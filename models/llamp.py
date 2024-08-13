import torch
import torch.nn as nn
import torch.nn.functional as F

from peft import LoraConfig, TaskType, LoraModel
from typing import Any, Optional, Tuple, Union

import os
import numpy as np

from flags import DATA_FOLDER

import math

from transformers import CLIPProcessor, CLIPModel
from transformers.modeling_attn_mask_utils import _prepare_4d_causal_attention_mask

import copy

def _make_causal_mask(
    input_ids_shape: torch.Size, dtype: torch.dtype, device: torch.device, past_key_values_length: int = 0
):
    """
    Make causal mask used for bi-directional self-attention.
    """
    bsz, tgt_len = input_ids_shape
    mask = torch.full((tgt_len, tgt_len), torch.finfo(dtype).min, device=device)
    mask_cond = torch.arange(mask.size(-1), device=device)
    mask.masked_fill_(mask_cond < (mask_cond + 1).view(mask.size(-1), 1), 0)
    mask = mask.to(dtype)

    if past_key_values_length > 0:
        mask = torch.cat([torch.zeros(tgt_len, past_key_values_length, dtype=dtype, device=device), mask], dim=-1)
    return mask[None, None, :, :].expand(bsz, 1, tgt_len, tgt_len + past_key_values_length)

def _expand_mask(mask: torch.Tensor, dtype: torch.dtype, tgt_len: Optional[int] = None):
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    """
    bsz, src_len = mask.size()
    tgt_len = tgt_len if tgt_len is not None else src_len

    expanded_mask = mask[:, None, None, :].expand(bsz, 1, tgt_len, src_len).to(dtype)

    inverted_mask = 1.0 - expanded_mask

    return inverted_mask.masked_fill(inverted_mask.to(torch.bool), torch.finfo(dtype).min)

class LLaMP(nn.Module):

    def __init__(self, dset, classnames, args, model, tokenizer, few_shot=False, indices=None):
        super(LLaMP, self).__init__()
        self.args = args
        self.dset = dset
        
        self.naive_decoding = args.naive_decoding
        self.debug = args.debug

        vision_peft_config = LoraConfig(
            task_type=TaskType.FEATURE_EXTRACTION, inference_mode=False, r=args.lora_rank, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout,
            target_modules=["layers.{}.self_attn.q_proj".format(i) for i in range(args.v_lora_start, args.v_lora_end)] + ["layers.{}.self_attn.v_proj".format(i) for i in range(args.v_lora_start, args.v_lora_end)]
        )

        language_peft_config = LoraConfig(
            task_type=TaskType.FEATURE_EXTRACTION, inference_mode=False, r=args.lora_rank, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout,
            target_modules=["v_proj", "q_proj"]
        )

        self.clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch16")
        self.clip_model.requires_grad_(False)

        self.processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch16")

        self.text_inputs = {}
        self.prompt_offset_indices = {}
        self.eos_offset = {}

        self.few_shot = few_shot

        self.num_prior_tokens = args.num_prior_tokens 
        self.num_llm_prompts = args.num_llm_prompts
        self.num_text_template = args.num_text_template

        self.num_text_ctx = args.num_text_ctx
        self.llm_prompt_depth = args.llm_prompt_depth

        self.decoder_skip_connection = args.decoder_skip_connection
        self.concat_fixed_prompts = args.concat_fixed_prompts

        if self.concat_fixed_prompts:
            self.num_special_tokens = 4 + self.num_llm_prompts
        else: # False
            self.num_special_tokens = self.num_llm_prompts

        self.prompt_type = args.prompt_type

        target_list = ['base', 'new'] if not self.few_shot else ['all']

        for target in target_list:
            self.text_inputs[target] = self.processor(
                ["a photo of a {}".format(c.replace("_", " ")) for c in classnames[target]], 
                return_tensors="pt", padding=True
            )

            if self.prompt_type == 'prefix':
                self.text_inputs[target]['input_ids'] = torch.cat((torch.ones((len(classnames[target]), self.num_special_tokens), dtype=torch.long) * self.processor.tokenizer.bos_token_id, self.text_inputs[target].input_ids), dim=1)
                self.text_inputs[target]['attention_mask'] = torch.cat((torch.ones((len(classnames[target]), self.num_special_tokens), dtype=torch.long), self.text_inputs[target].attention_mask), dim=1)
            elif self.prompt_type == "suffix":
                # Suffix
                eos_loc = self.text_inputs[target]['input_ids'].argmax(dim=-1)
                idx = eos_loc != (self.text_inputs[target]['input_ids'].shape[1] - 1)

                self.text_inputs[target]['attention_mask'][:, -1] = 1
                self.text_inputs[target]['input_ids'] = torch.cat((self.text_inputs[target].input_ids, torch.ones((len(classnames[target]), self.num_special_tokens), dtype=torch.long) * self.processor.tokenizer.pad_token_id), dim=1)
                self.text_inputs[target]['attention_mask'] = torch.cat((self.text_inputs[target].attention_mask, torch.ones((len(classnames[target]), self.num_special_tokens), dtype=torch.long)), dim=1)

                eos_loc = self.text_inputs[target]['input_ids'].argmax(dim=-1)
                self.text_inputs[target]['attention_mask'][torch.arange(len(classnames[target]))[idx], eos_loc[idx]] = 0

                self.eos_offset[target] = (torch.arange(len(classnames[target])), eos_loc)


        self.eos_token_id = self.clip_model.text_model.eos_token_id 

        if self.naive_decoding:
            if args.freeze_vit: # False
                self.lora_model = nn.ModuleDict({'default': self.clip_model.vision_model})
                self.lora_model.requires_grad_(False)
            else:
                self.lora_model = nn.ModuleDict({'default': LoraModel(self.clip_model.vision_model, {'default': vision_peft_config}, 'default')})

        self.text_hidden_size = self.clip_model.text_model.config.hidden_size

        print("Loading CLIP text embeddings from {}".format(args.clip_text_embed_file)) # release_clip_text_embeddings.pt
        text_embeddings = torch.load(os.path.join(self.dset.data_dir, args.clip_text_embed_file))

        if type(text_embeddings['base']) == dict:
            self.base_embeddings = nn.Parameter(text_embeddings['base']['avg'], requires_grad=False)
            self.new_embeddings = nn.Parameter(text_embeddings['new']['avg'], requires_grad=False)
        else:
            self.base_embeddings = nn.Parameter(text_embeddings['base'], requires_grad=False)
            self.new_embeddings = nn.Parameter(text_embeddings['new'], requires_grad=False)

        if self.few_shot:
            if indices is not None:
                self.text_embeddings = nn.ParameterDict({
                    'all': torch.cat((self.base_embeddings, self.new_embeddings), dim=0)[indices]
                }) 
            else:
                self.text_embeddings = nn.ParameterDict({
                    'all': torch.cat((self.base_embeddings, self.new_embeddings), dim=0)
                })
        else:
            self.text_embeddings = nn.ParameterDict({
                'base': self.base_embeddings,
                'new': self.new_embeddings,
            })

        self.distillation_type = args.distillation_type
        self.base_loss = nn.CrossEntropyLoss(label_smoothing=args.label_smoothing)
        self.token_bias = args.token_bias

        self.visual_prompting = args.visual_prompting

        self.prompt_depth = max(self.llm_prompt_depth, args.text_prompt_depth)

        self.visual_prompt_depth = args.visual_prompt_depth
        self.text_prompt_depth = self.prompt_depth

        if self.visual_prompting:
            self.visual_prompts = nn.Parameter(torch.empty((self.visual_prompt_depth, args.num_vis_ctx, self.clip_model.vision_model.config.hidden_size)).normal_(0, 1))
            nn.init.kaiming_uniform_(self.visual_prompts, a=math.sqrt(5))

        self.text_prompts = nn.ParameterList()
            
        ctx_init = "a photo of a"
        n_ctx = 4
        prompt = self.processor([ctx_init], return_tensors="pt")
        with torch.no_grad():
            embedding = self.clip_model.text_model.embeddings(input_ids=prompt.input_ids)
        init_prompt = nn.Parameter(embedding[0, 1: 1 + n_ctx, :], requires_grad=True)
        self.text_prompts.extend(nn.ParameterList([init_prompt]))

        self.in_layer_prompts = nn.ParameterList([
            nn.Parameter(torch.empty(self.num_text_ctx, 512).normal_(0, 1), requires_grad=True) for _ in range(self.text_prompt_depth-1)])
        for i in range(len(self.in_layer_prompts)):
            nn.init.kaiming_uniform_(self.in_layer_prompts[i], a=math.sqrt(5))

        self.text_prompts.extend(self.in_layer_prompts)

        self.num_decoder_layers = args.num_decoder_layers

        print("Loading past key values from {}".format(args.past_key_value_file))
        content_dict = torch.load(os.path.join(DATA_FOLDER, dset.data_dir, args.past_key_value_file))

        if self.few_shot:
            if indices is not None:
                self.all_class_key_values = nn.ParameterList(
                    [nn.Parameter(x['past_key_values'][-self.num_decoder_layers:, : , indices], requires_grad=False) for x in content_dict['all']]
                )
                self.all_class_attn_mask = [x['attn_mask'][indices] for x in content_dict['all']]
            else:
                self.all_class_key_values = nn.ParameterList(
                    [nn.Parameter(x['past_key_values'][-self.num_decoder_layers:], requires_grad=False) for x in content_dict['all']]
                )
                self.all_class_attn_mask = [x['attn_mask'] for x in content_dict['all']]

            self.past_key_values = nn.ParameterDict({
                'all': self.all_class_key_values,
            })

            self.attention_mask = {
                'all': self.all_class_attn_mask,
            }
        else:
            self.base_class_key_values = nn.ParameterList(
                [nn.Parameter(x['past_key_values'][-self.num_decoder_layers:], requires_grad=False) for x in content_dict['base']]
            )
            self.base_class_attn_mask = [x['attn_mask'] for x in content_dict['base']]

            self.new_class_key_values = nn.ParameterList(
                [nn.Parameter(x['past_key_values'][-self.num_decoder_layers:], requires_grad=False) for x in content_dict['new']]
            )
            self.new_class_attn_mask = [x['attn_mask'] for x in content_dict['new']]

            self.past_key_values = nn.ParameterDict({
                'base': self.base_class_key_values,
                'new': self.new_class_key_values,
            })

            self.attention_mask = {
                'base': self.base_class_attn_mask,
                'new': self.new_class_attn_mask,
            }
            
            if self.token_bias:

                self.base_token_bias = nn.ParameterList(
                    [nn.Parameter(x['next_token_embeds'][-self.num_decoder_layers, :, :self.num_prior_tokens, :], requires_grad=False) for x in content_dict['base']] if self.token_bias else [torch.zeros(1)]
                )

                self.new_token_bias = nn.ParameterList(
                    [nn.Parameter(x['next_token_embeds'][-self.num_decoder_layers, :, :self.num_prior_tokens, :], requires_grad=False) for x in content_dict['new']] if self.token_bias else [torch.zeros(1)]
                )   

                self.base_token_bias_attn_mask = [x['next_token_attn_mask'] for x in content_dict['base']]
                self.new_token_bias_attn_mask = [x['next_token_attn_mask'] for x in content_dict['new']]

                self.next_token_bias = nn.ParameterDict({
                    'base': self.base_token_bias,
                    'new': self.new_token_bias,
                })

                self.next_token_attn_mask = {
                    'base': self.base_token_bias_attn_mask,
                    'new': self.new_token_bias_attn_mask,
                }

        self.class_token = nn.ParameterList(
            [nn.Parameter(torch.empty((self.num_llm_prompts, model.config.hidden_size)).normal_(0, 1)) for _ in range(1)]
        ) 
        for i in range(len(self.class_token)):
            nn.init.kaiming_uniform_(self.class_token[i], a=math.sqrt(5)) 

        self.class_proj = nn.Identity()
        self.class_norm = copy.deepcopy(model.model.norm)

        if args.lora_decoding:
            self.class_decoder = nn.ModuleList([
                LoraModel(copy.deepcopy(model.model.layers[i]), {'default': language_peft_config}, 'default')  for i in range(-self.num_decoder_layers, 0)])
            self.class_norm.requires_grad_(False)
        else: # decoder 마지막 레이어만 # 여기서 model은 llama -> 그러니까 llama 디코더의 마지막 레이어만 학습
            self.class_decoder = nn.ModuleList([copy.deepcopy(model.model.layers[i]) for i in range(-self.num_decoder_layers, 0)])
            self.class_decoder.requires_grad_(True)
            self.class_norm.requires_grad_(True)


        self.text_proj = nn.ModuleList(
            [nn.Linear(model.config.hidden_size, self.text_hidden_size, bias=False) for _ in range(self.llm_prompt_depth)]
        )

        self.llm_prompt_bias = nn.ParameterList([
            nn.Parameter(torch.empty(self.num_special_tokens, 512).normal_(0,1)) for _ in range(self.llm_prompt_depth)
        ])

        for i in range(len(self.llm_prompt_bias)):
            nn.init.kaiming_uniform_(self.llm_prompt_bias[i], a=math.sqrt(5))

        self.class_embed_weight = nn.Parameter(torch.zeros(1), requires_grad=False)

        if args.learn_class_embed_weight: # False
            self.class_embed_weight.requires_grad_(True)

        if args.prompt_learning: # False
            self.class_decoder.requires_grad_(False)
            self.class_norm.requires_grad_(False)

        if args.freeze_decoder_kv_proj: # True
            for decoder in self.class_decoder:
                decoder.self_attn.k_proj.requires_grad_(False)
                decoder.self_attn.v_proj.requires_grad_(False)
            
        if args.freeze_decoder_q_proj: # False
            for decoder in self.class_decoder:
                decoder.self_attn.q_proj.requires_grad_(False)
        
        if args.freeze_decoder_o_proj: # False
            for decoder in self.class_decoder:
                decoder.self_attn.o_proj.requires_grad_(False)
        
        if args.freeze_decoder_attn: # False
            for decoder in self.class_decoder:
                decoder.self_attn.requires_grad_(False) 
        
        if args.freeze_decoder_ffn: # True
            for decoder in self.class_decoder:
                decoder.mlp.requires_grad_(False)
        
        self.class_fn = self.decode_class # CLIP Text Encoder 부분 정의하는 것
                    
        self.logit_scale = nn.Parameter(torch.tensor([np.log(1/0.01)]), requires_grad=True)

        self.dropout = nn.Dropout(args.prompt_dropout)
        self.image_dropout = nn.Dropout(args.img_dropout)
        self.lambda_dist = args.lambda_dist

    def decode_class(self, subset='base', bias=None):
        pkv = self.past_key_values[subset]
        attention_mask = self.attention_mask[subset]

        if self.training:
            template_idx = torch.randint(self.num_text_template, (1,)).item()
            if self.token_bias: # False
                selected_embeddings = self.next_token_bias[subset][template_idx]
                selected_attn_mask = self.next_token_attn_mask[subset][template_idx]
            else:
                selected_embeddings = None
                selected_attn_mask = None

            encoded_prompt = self.generate_text_features_from_prompt( # 이건 논문 figure에서 h_l을 depth 9개 만큼 text encoder에 넣은 것 
                pkv[template_idx], 
                attention_mask[template_idx], 
                self.class_token[0],
                selected_embeddings, 
                selected_attn_mask, 
                subset=subset
            ) # 그래서 이게 논문 figure에서 g_p에 해당함
        else:
            encoded_prompts = []
            for template_idx in range(self.num_text_template):

                if self.token_bias:
                    selected_embeddings = self.next_token_bias[subset][template_idx]
                    selected_attn_mask = self.next_token_attn_mask[subset][template_idx]
                else:
                    selected_embeddings = None
                    selected_attn_mask = None


                encoded_prompt = self.generate_text_features_from_prompt(
                        pkv[template_idx], 
                        attention_mask[template_idx], 
                        self.class_token[0],
                        selected_embeddings,
                        selected_attn_mask,
                        subset=subset
                    )
                encoded_prompts.append(encoded_prompt)

            encoded_prompt = torch.stack(encoded_prompts, dim=0) # g_p가 11개 담김

        outputs = ((encoded_prompt, self.text_embeddings[subset]), ) # 논문 figure에서 g_p, text clip embedding을 의미함
        return outputs
        

    def generate_text_features_from_prompt(self, pkv, attention_mask, class_token, selected_embeddings=None, selected_attn_mask=None, subset='base'):

        all_embeds = []

        num_classes = self.text_embeddings[subset].shape[0] # self.text_embeddings['base'] : (19, 512) , self.text_embeddings['new'] : (18, 512)
        tokens = self.class_proj(class_token) # self.class_proj : Identity() , class_token : (16, 4096)
        tokens = tokens.unsqueeze(0).expand(num_classes, -1, -1) # tokens : (19, 16, 4096)

        device = tokens.device

        if selected_embeddings is not None: # None
            attention_mask = torch.cat((attention_mask.to(device), selected_attn_mask.to(device), torch.ones((attention_mask.shape[0], tokens.shape[1])).to(device)), dim=1)

            tokens = torch.cat([
                selected_embeddings,
                tokens,
            ], dim=1)
        else:
            attention_mask = torch.cat((attention_mask.to(device), torch.ones((attention_mask.shape[0], tokens.shape[1])).to(device)), dim=1)
            # attention_mask : (19, 124), tokens.shape[1] : 16 -> (19, 124)와 (19, 16)을 concat -> (19, 140) => 이건 llm prompt에 대한 attention mask와 class token을 합친 것
            # 여기서 class_token이 16짜리인 이유는 llm prompt 수도 16개여서

        position_ids = torch.clamp(torch.cumsum(attention_mask, dim=-1).long() - 1, min=0)[:, -tokens.shape[1]:] # attention_mask : (19, 140)인데 여기서 (19, 124)만큼의 index가 담김

        attention_mask = _prepare_4d_causal_attention_mask(
            attention_mask, (num_classes, tokens.shape[1]), tokens, pkv.shape[-2]
        )
        # attention_mask : (19, 1, 16, 140)

        hidden_states = tokens  # hidden_states : (19, 16, 4096)

        past_key_values_length = pkv[0][0].shape[2]

        for idx, decoder_layer in enumerate(self.class_decoder): # self.class_decoder : llama 디코더 마지막 레이어
            layer_outputs = decoder_layer(
                hidden_states, # class_token # TODO : 이게 논문 figure에서 p_l에 해당하는 것으로 추정
                attention_mask=attention_mask, # llm prompt에 대한 attention mask와 class token을 합친 것 # TODO : 디버그 시 모델에서 required_grad가 True인 것들을 찾고 이름을 봐서 p_l에 해당하는 것을 찾아보자
                position_ids=position_ids, # llm prompt에 대한 attention mask의 index
                past_key_value=pkv[idx], 
                use_cache=False,
                output_attentions=True
            )
            hidden_states = layer_outputs[0]

        hidden_states = self.class_norm(hidden_states) # hidden_states가 논문 figure에서 h_l에 해당함 (19, 16, 4096)

        class_embed = hidden_states[:, -self.num_special_tokens:, :] # num_special_tokens : 16 -> 원본 그대로 출력
        class_embed = self.dropout(class_embed) * self.class_embed_weight.exp() # dropout prob : 0, class_embed_weight : tensor([0.]) (freeze됨) => 그냥 그대로임

        for i in range(self.llm_prompt_depth):
            all_embeds.append(self.text_proj[i](class_embed) + self.llm_prompt_bias[i]) # -> (19, 16, 512)짜리 9개가 all_embeds에 담김
            # self.text_proj : Linear(in_features=4096, out_features=512, bias=False) * 9개
            # self.llm_prompt_bias : tensor (16, 512) * 9개
                
        encoded_prompt = self.encode_LLM_prompt(torch.stack(all_embeds, dim=0), subset=subset) # torch.stack(all_embeds, dim=0) : (9, 19, 16, 512)
        return encoded_prompt # 이게 논문 figure에서 g_p에 해당함

    def encode_LLM_prompt(self, prompts, subset):

        device = prompts.device
        input_ids = self.text_inputs[subset].input_ids.to(device) # (19, 26), "a photo of a" + classnames을 token화한 것
        attention_mask = self.text_inputs[subset].attention_mask.to(device) # (19, 26)
        position_ids = None 

        input_shape = input_ids.size() # (19, 26)
        input_ids = input_ids.view(-1, input_shape[-1]) # (19, 26)

        if self.prompt_type == 'prefix':
            hidden_states = self.clip_model.text_model.embeddings(input_ids=input_ids[:, self.num_special_tokens:], position_ids=position_ids)
            hidden_states = torch.cat([
                hidden_states[:, :1, :],
                torch.cat([prompts[0], self.text_prompts[0].unsqueeze(0).expand(hidden_states.shape[0], -1, -1)], dim=1),
                hidden_states[:, 1+self.num_text_ctx:, :]
            ], dim=1)
        elif self.prompt_type == 'suffix':
            hidden_states = self.clip_model.text_model.embeddings(input_ids=input_ids, position_ids=position_ids) # clip text encoder에 (19, 26)짜리 text token을 넣어서 hidden state를 만듦 => (19, 26, 512)
            hidden_states = torch.cat([
                hidden_states[:, :1, :], # bos token으로 추정 (19, 1, 512)
                self.text_prompts[0].unsqueeze(0).expand(hidden_states.shape[0], -1, -1), # self.text_prompts는 "a photo of a"의 embedding(4, 512)과 정규화된 8개의 embedding(4, 512)이다 => 그래서 "a photo of a"의 embedding으로 사이즈에 맞게 확장시킴 (19, 4, 512) # 이게 논문 figure에서 p_t에 해당하는 것으로 추정
                hidden_states[:, 1+self.num_text_ctx:-self.num_special_tokens-1, :], # class name에 대한 clip text embedding (19, 4, 512)
                prompts[0], # h_l의 9개 중 첫 번째 (19, 16, 512)
                hidden_states[self.eos_offset[subset]].unsqueeze(1) # eos token에 대한 clip text embedding (19, 1, 512)
            ], dim=1) # => 그래서 이 hidden_states는 논문 figure에서 h_l에 해당함 (19, 26, 512)

        causal_attention_mask = _make_causal_mask(input_shape, hidden_states.dtype, device=hidden_states.device) # (19, 1, 26, 26) => 대각선 아래는 0, 나머지는 -inf로 채워진 mask
        if attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            attention_mask = _expand_mask(attention_mask, hidden_states.dtype) # => (19, 1, 26, 26)로 확장

        for idx, encoder_layer in enumerate(self.clip_model.text_model.encoder.layers): # len : 12
            if idx > 0 and idx < self.text_prompt_depth: # 9
                if self.prompt_type == 'prefix':
                    if idx < self.llm_prompt_depth:
                        next_prompts = torch.cat([prompts[idx], self.text_prompts[idx].unsqueeze(0).expand(hidden_states.shape[0], -1, -1)], dim=1)
                    else:
                        next_prompts = torch.cat([
                            hidden_states[:, 1:1+self.num_special_tokens, :],
                            self.text_prompts[idx].unsqueeze(0).expand(hidden_states.shape[0], -1, -1),
                        ], dim=1)

                    hidden_states = torch.cat([
                        hidden_states[:, :1, :],
                        next_prompts,
                        hidden_states[:, 1+self.num_text_ctx+self.num_special_tokens:, :]
                    ], dim=1)
                    
                elif self.prompt_type == 'suffix':
                    if idx < self.llm_prompt_depth: # 9 # 여기에서 idx가 text encoder의 layer index를 의미함 
                        hidden_states = torch.cat([ # 앞선 9개 레이어에 대해서는 hidden_states에 llm prompt를 넣어줌
                            hidden_states[:, :1, :],
                            self.text_prompts[idx].unsqueeze(0).expand(hidden_states.shape[0], -1, -1),
                            hidden_states[:, 1 + self.num_text_ctx:-self.num_special_tokens-1, :],
                            prompts[idx],
                            hidden_states[:, -1:, :]
                        ], dim=1)
                    else:
                        hidden_states = torch.cat([ # 이후 3개의 레이어에 대해서는 "a photo of a classname"에 대한 hidden state만 넣어줌
                            hidden_states[:, :1, :],
                            self.text_prompts[idx].unsqueeze(0).expand(hidden_states.shape[0], -1, -1),
                            hidden_states[:, 1 + self.num_text_ctx:, :]
                        ], dim=1)

            layer_outputs = encoder_layer(  # for문을 돌면서 text encoder를 12번 통과함
                hidden_states, # (19, 26, 512)
                attention_mask=attention_mask,
                causal_attention_mask=causal_attention_mask,
            )

            hidden_states = layer_outputs[0]

        last_hidden_state = hidden_states # (19, 26, 512)
        last_hidden_state = self.clip_model.text_model.final_layer_norm(last_hidden_state)

        if self.prompt_type == 'prefix':
            pooled_output = last_hidden_state[
                torch.arange(last_hidden_state.shape[0], device=last_hidden_state.device),
                input_ids.to(dtype=torch.int, device=last_hidden_state.device).argmax(dim=-1),
            ]
        else:
            pooled_output = last_hidden_state[:, -1, :]

        text_features = self.clip_model.text_projection(pooled_output)

        return text_features # 이게 논문 figure에서 g_p에 해당함 (a photo of class + llm prompt)

    def forward(self, x, subset=None):
        if self.training:
            loss, pred = self.run(x)
            return loss, pred
        else:
            scores = self.run(x, subset)
            return None, scores
    
    def compute_all_class_embeddings(self, subset):

        outputs = self.class_fn(subset=subset)
        class_embed = outputs[0] # g_p가 11개 담김

        self.all_class_embed = class_embed
    
    def extract_image_features(self, img, target="default", dropout=False):
        if self.visual_prompting: # True
            image_features = self.extract_prompt_image_features(img, model=self.lora_model[target]) # self.lora_model['default'] : CLIP image encoder, 뒤 6개 레이어는 lora 붙음
        else:
            image_features = self.lora_model[target](img)[1]
            if dropout:
                image_features = self.image_dropout(image_features)
            image_features = self.clip_model.visual_projection(image_features)
        return image_features # 이게 논문 figure에서 f_p에 해당함
    
    def extract_prompt_image_features(self, img, model, dropout=False):
        hidden_states = model.embeddings(img)
        hidden_states = torch.cat([hidden_states, self.visual_prompts[0].unsqueeze(0).expand(hidden_states.shape[0], -1, -1)], dim=1) # self.visual_prompts가 논문 figure에서 p_v에 해당함
        hidden_states = model.pre_layrnorm(hidden_states) # hidden_states : (4, 201, 768) , => 아직 이건 encoder layer에는 통과 안 함

        len_vpt = self.visual_prompts.shape[1] # self.visual_prompts : (6, 4, 768)

        for idx, encoder_layer in enumerate(model.encoder.layers): # 12개
            if idx > 0 and idx < self.visual_prompt_depth: # visual_prompt_depth : 6
                hidden_states = torch.cat([hidden_states[:, :-len_vpt], self.visual_prompts[idx].unsqueeze(0).expand(hidden_states.shape[0], -1, -1)], dim=1)
                # hidden_states[:, :-len_vpt] : (4, 197, 768) , self.visual_prompts[idx]가 idx번째 layer에 입력되는 p_v를 의미함 (4, 768) -> (4, 4, 768)
                
            layer_outputs = encoder_layer(
                hidden_states,
                attention_mask=None,
                causal_attention_mask=None,
            )

            hidden_states = layer_outputs[0]

        last_hidden_states = hidden_states # (4, 201, 768)
        pooled_output = last_hidden_states[:, 0, :]
        pooled_output = model.post_layernorm(pooled_output)

        visual_features = self.clip_model.visual_projection(pooled_output)

        return visual_features
    
    def run(self, x, subset=None):
        if self.training:
            img, img_1, labels = x
        else:
            img = x[0] # img : (4, 3, 224, 224)

        self.logit_scale.data = torch.clamp(self.logit_scale.data, 0, 4.605)

        normalize_fn = lambda x: F.normalize(x, dim=-1)
        logit_scale = self.logit_scale.exp()

        embeds = {}


        if self.training:
            if self.few_shot:
                embeds['llm'], embeds['clip'] = self.class_fn(subset='all')[0]
            else:
                embeds['llm'], embeds['clip'] = self.class_fn(subset='base')[0] # embeds['llm'] : 논문 figure에서 g_p에 해당함 (a photo of class + llm prompt)
        else:
            embeds['llm'], embeds['clip'] = self.all_class_embed # self.all_class_embed : g_p가 11개 담긴 것, clip text embedding

        embeds['all'] = embeds['llm']

        raw_clip_embeds = embeds['clip'] # clip embedding (?)
        raw_llm_embeds = embeds['llm'] # g_p (a photo of class + llm prompt)

        for k, v in embeds.items(): # llm, clip, all
            if embeds[k].ndim == 3: # 2
                embeds[k] = normalize_fn(v).permute(0, 2, 1)
            else: # (19, 512) -> (512, 19)
                embeds[k] = normalize_fn(v).permute(1, 0)

        if self.training:
            with torch.inference_mode():
                orig_image_features = self.clip_model.vision_model(img_1)[1] # img_1 : (4, 3, 224, 224)
                orig_image_features = self.clip_model.visual_projection(orig_image_features)
                # orig_image_features : (4, 512)

        target_pred = {}
        if self.training:
            class_features = self.extract_image_features(img) # img : (4, 3, 224, 224) # class_features : 이게 논문 figure에서 f_p에 해당함
            image_features = class_features # image_features : 이게 논문 figure에서 f_p에 해당함
            target_pred['clip'] = normalize_fn(image_features) @ embeds['clip']
            if image_features.ndim != embeds['llm'].ndim:
                image_features = image_features.unsqueeze(0)
            target_pred['llm'] = torch.matmul(normalize_fn(image_features), embeds['llm'])
            target_pred['all'] = torch.matmul(normalize_fn(image_features), embeds['all'])
            clip_pred = normalize_fn(orig_image_features) @ embeds['clip'] # 논문 수식에서 f^*g^에 해당함 # TODO : 여기서 embeds['clip']은 어떤 텍스트의 임베딩인가?

                
            target_pred['clip'] = target_pred['clip'].float() # clip text embedding과 image feature(p_v 포함)의 내적값
            target_pred['llm'] = target_pred['llm'].float() # (a photo of class + llm prompt)와 image feature의 내적값
            target_pred['all'] = target_pred['all'].float() # (a photo of class + llm prompt)와 image feature의 내적값
            clip_pred = clip_pred.float() # clip text embedding과 원본 image feature의 내적값 # 논문 수식에서 f^*g^에 해당함
            raw_clip_embeds = raw_clip_embeds.float()
            raw_llm_embeds = raw_llm_embeds.float()
            image_features = image_features.float()
            orig_image_features = orig_image_features.float()
        else:
            class_features = self.extract_image_features(img) # class_features : 이게 논문 figure에서 f_p에 해당함
            image_features = class_features
            if image_features.ndim != embeds['llm'].ndim:
                image_features = image_features.unsqueeze(0)
            target_pred['all'] = torch.matmul(normalize_fn(image_features), embeds['all'])
            target_pred['all'] = target_pred['all'].float()
                    
        if self.training:
            base_loss = self.base_loss(target_pred['all'] * logit_scale, labels) # CE Loss : 논문 figure에서 g_p와 f_p
            feature_l1_loss = F.l1_loss(normalize_fn(raw_clip_embeds), normalize_fn(raw_llm_embeds)) * 25
            feature_l1_loss += F.l1_loss(normalize_fn(image_features), normalize_fn(orig_image_features)) * 10
            # clip text embedding과 g_p(a photo of class + llm prompt)의 l1 loss + image feature(p_v포함)와 원본 image feature의 l1 loss
            
            if self.distillation_type == 'soft': # soft
                dist_loss = F.kl_div(F.log_softmax(target_pred['all'] * logit_scale, dim=-1), F.log_softmax(clip_pred * logit_scale, dim=-1),reduction='sum', log_target=True) / target_pred['all'].numel() * self.lambda_dist
                # 논문 수식에서 f_p*g_p와 f^*g^의 KL Divergence # self.lambda_dist : 2.5
            elif self.distillation_type == 'hard':
                dist_loss = F.cross_entropy(target_pred['all'] * logit_scale, clip_pred.argmax(dim=-1), reduction='mean') * self.lambda_dist
        
            loss = base_loss + feature_l1_loss + dist_loss
            losses = {
                'loss_ce': base_loss,
                'loss_dist': dist_loss,
                'loss_l1': feature_l1_loss,
                'loss_total': loss
            }
            return losses, target_pred['all']
        else:
            if target_pred['all'].ndim == 2:
                return target_pred['all']
            else:
                return F.softmax(target_pred['all'].float(), dim=-1).mean(dim=0)

