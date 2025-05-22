import functools
from typing import Any, List, Optional, Tuple, Union

import torch
import torch.nn as nn
from transformers.models.llava.modeling_llava import \
    LlavaCausalLMOutputWithPast

from llmc.utils.registry_factory import TOKEN_REDUCTION_REGISTRY

from .token_reduction_module import TokenReductionModule
from .utils import apply_info


def visionzip_forward(
    self,
    input_ids: torch.LongTensor = None,
    pixel_values: torch.FloatTensor = None,
    attention_mask: Optional[torch.Tensor] = None,
    position_ids: Optional[torch.LongTensor] = None,
    past_key_values: Optional[List[torch.FloatTensor]] = None,
    inputs_embeds: Optional[torch.FloatTensor] = None,
    vision_feature_layer: Optional[int] = None,
    vision_feature_select_strategy: Optional[str] = None,
    labels: Optional[torch.LongTensor] = None,
    use_cache: Optional[bool] = None,
    output_attentions: Optional[bool] = None,
    output_hidden_states: Optional[bool] = None,
    return_dict: Optional[bool] = None,
    cache_position: Optional[torch.LongTensor] = None,
    num_logits_to_keep: int = 0,
) -> Union[Tuple, LlavaCausalLMOutputWithPast]:

    output_attentions = (
        output_attentions
        if output_attentions is not None
        else self.config.output_attentions
    )
    output_hidden_states = (
        output_hidden_states
        if output_hidden_states is not None
        else self.config.output_hidden_states
    )
    return_dict = (
        return_dict if return_dict is not None else self.config.use_return_dict
    )
    vision_feature_layer = (
        vision_feature_layer
        if vision_feature_layer is not None
        else self.config.vision_feature_layer
    )
    vision_feature_select_strategy = (
        vision_feature_select_strategy
        if vision_feature_select_strategy is not None
        else self.config.vision_feature_select_strategy
    )

    if (input_ids is None) ^ (inputs_embeds is not None):
        raise ValueError(
            'You cannot specify both input_ids and '
            'inputs_embeds at the same time, and must specify either one'
        )

    if pixel_values is not None and inputs_embeds is not None:
        raise ValueError(
            'You cannot specify both pixel_values and '
            'inputs_embeds at the same time, and must specify either one'
        )

    legacy_processing = False
    if inputs_embeds is None:
        inputs_embeds = self.get_input_embeddings()(input_ids)

        legacy_processing = (
            (input_ids == self.config.image_token_index).sum(1).max()
            < self.config.image_seq_length
        ) or (input_ids.shape[-1] == 1 and pixel_values is not None)

    if pixel_values is not None:
        image_outputs = self.vision_tower(pixel_values, output_hidden_states=True)
        # this is not memory efficient at all
        # (output_hidden_states=True) will save all the hidden stated.
        selected_image_feature = image_outputs.hidden_states[vision_feature_layer]
        if vision_feature_select_strategy == 'default':
            selected_image_feature = selected_image_feature[:, 1:]
        elif vision_feature_select_strategy == 'full':
            selected_image_feature = selected_image_feature
        else:
            raise ValueError(
                f'Unexpected select \
                             feature strategy: {self.config.vision_feature_select_strategy}'
            )

        image_features = self.multi_modal_projector(selected_image_feature)

        image_token_idxs = (input_ids == self.config.image_token_index).nonzero(
            as_tuple=True
        )
        image_start_idx, image_end_idx = image_token_idxs[1][0], image_token_idxs[1][-1]
        image_token_num = image_features.shape[1]
        input_ids = torch.cat(
            [
                input_ids[:, :image_start_idx],
                input_ids[:, image_start_idx: image_start_idx + image_token_num],
                input_ids[:, image_end_idx + 1:],
            ],
            dim=1,
        )
        inputs_embeds = torch.cat(
            [
                inputs_embeds[:, :image_start_idx],
                inputs_embeds[:, image_start_idx: image_start_idx + image_token_num],
                inputs_embeds[:, image_end_idx + 1:],
            ],
            dim=1,
        )
        token_num = input_ids.shape[1]
        attention_mask = attention_mask[:, :token_num]
        position_ids = position_ids[:, :token_num]
        cache_position = cache_position[:token_num]

        if legacy_processing:
            # prefill stage vs decoding stage (legacy behavior copied)
            if input_ids.shape[1] != 1:
                inputs_embeds, attention_mask, labels, position_ids = (
                    self._merge_input_ids_with_image_features(
                        image_features, inputs_embeds, input_ids, attention_mask, labels
                    )
                )
                cache_position = torch.arange(
                    attention_mask.shape[1], device=attention_mask.device
                )
            else:
                # Retrieve the first layer to inspect the logits and mask out the hidden states
                # that are set to 0
                first_layer_past_key_value = past_key_values[0][0][:, :, :, 0]

                batch_index, non_attended_tokens = torch.where(
                    first_layer_past_key_value.float().sum(-2) == 0
                )

                # Get the target length
                target_length = input_ids.shape[1]
                past_length = first_layer_past_key_value.shape[-1]

                extended_attention_mask = torch.ones(
                    (attention_mask.shape[0], past_length),
                    dtype=attention_mask.dtype,
                    device=attention_mask.device,
                )

                # Filter out only the tokens that can be un-attended, this can happen
                # if one uses Llava + Fused modules where the cache on the
                # first iteration is already big enough, or if one passes custom cache
                valid_indices = non_attended_tokens < extended_attention_mask.size(-1)
                new_batch_index = batch_index[valid_indices]
                new_non_attended_tokens = non_attended_tokens[valid_indices]

                # Zero-out the places where we don't need to attend
                extended_attention_mask[new_batch_index, new_non_attended_tokens] = 0

                attention_mask = torch.cat(
                    (extended_attention_mask, attention_mask[:, -target_length:]), dim=1
                )
                position_ids = torch.sum(attention_mask, dim=1).unsqueeze(-1) - 1
                cache_position = torch.arange(
                    attention_mask.shape[1], device=attention_mask.device
                )[-target_length:]

        # TODO: @raushan retain only the new behavior after v4.47
        else:
            special_image_mask = (
                (input_ids == self.config.image_token_index)
                .unsqueeze(-1)
                .expand_as(inputs_embeds)
            )
            image_features = image_features.to(
                inputs_embeds.device, inputs_embeds.dtype
            )
            inputs_embeds = inputs_embeds.masked_scatter(
                special_image_mask, image_features
            )

    outputs = self.language_model(
        attention_mask=attention_mask,
        position_ids=position_ids,
        past_key_values=past_key_values,
        inputs_embeds=inputs_embeds,
        use_cache=use_cache,
        output_attentions=output_attentions,
        output_hidden_states=output_hidden_states,
        return_dict=return_dict,
        cache_position=cache_position,
        num_logits_to_keep=num_logits_to_keep,
    )

    logits = outputs[0]

    loss = None
    if labels is not None:
        # Shift so that tokens < n predict n
        if attention_mask is not None:
            shift_attention_mask = attention_mask[..., 1:]
            shift_logits = logits[..., :-1, :][
                shift_attention_mask.to(logits.device) != 0
            ].contiguous()
            shift_labels = labels[..., 1:][
                shift_attention_mask.to(labels.device) != 0
            ].contiguous()
        else:
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
        # Flatten the tokens
        loss_fct = nn.CrossEntropyLoss()
        loss = loss_fct(
            shift_logits.view(-1, shift_logits.size(-1)),
            shift_labels.view(-1).to(shift_logits.device),
        )

    if not return_dict:
        output = (logits,) + outputs[1:]
        return (loss,) + output if loss is not None else output

    return LlavaCausalLMOutputWithPast(
        loss=loss,
        logits=logits,
        past_key_values=outputs.past_key_values,
        hidden_states=outputs.hidden_states,
        attentions=outputs.attentions,
        image_hidden_states=image_features if pixel_values is not None else None,
    )


class CLIPVisionTower_VisionZip(nn.Module):

    @torch.no_grad()
    def forward(self, images):

        if type(images) is list:
            image_features = []
            for image in images:
                image_forward_out = self.vision_tower(
                    image.to(device=self.device, dtype=self.dtype).unsqueeze(0),
                    output_hidden_states=True,
                    output_attentions=True,
                )
                image_feature = self.feature_select(image_forward_out).to(image.dtype)
                image_features.append(image_feature)
        else:

            image_forward_outs = self.vision_tower(
                images.to(device=self.device, dtype=self.dtype),
                output_hidden_states=True,
                output_attentions=True,
            )
            attn_weights = image_forward_outs.attentions[-2]
            hidden_states = image_forward_outs.hidden_states[-2]
            metric = self.vision_tower.vision_model.encoder.layers[-2].metric
            dominant_num = self.vision_tower._info['dominant']
            contextual_num = self.vision_tower._info['contextual']

            # Dominant Visual Tokens
            cls_idx = 0
            cls_attention = attn_weights[:, :, cls_idx, cls_idx + 1:]
            cls_attention_sum = cls_attention.sum(dim=1)
            topk_indices = cls_attention_sum.topk(dominant_num, dim=1).indices + 1
            all_indices = torch.cat(
                [
                    torch.zeros(
                        (hidden_states.shape[0], 1),
                        dtype=topk_indices.dtype,
                        device=topk_indices.device,
                    ),
                    topk_indices,
                ],
                dim=1,
            )

            mask = torch.ones_like(
                hidden_states[:, :, 0], dtype=torch.bool, device=metric.device
            ).scatter_(1, all_indices, False)
            dominant_tokens = hidden_states.masked_select(~mask.unsqueeze(-1)).view(
                hidden_states.shape[0], dominant_num + 1, hidden_states.shape[2]
            )

            # Filter
            metric_filtered = metric[mask].view(
                hidden_states.shape[0],
                hidden_states.shape[1] - (dominant_num + 1),
                metric.shape[2],
            )

            hidden_states_filtered = hidden_states.masked_select(
                mask.unsqueeze(-1)
            ).view(
                hidden_states.shape[0],
                hidden_states.shape[1] - (dominant_num + 1),
                hidden_states.shape[2],
            )

            metric_normalized = metric_filtered / metric_filtered.norm(
                dim=-1, keepdim=True
            )

            # Contextual Visual Tokens
            step = max(1, metric_normalized.shape[1] // contextual_num)
            target_indices = torch.arange(
                0, metric_normalized.shape[1], step, device=metric_normalized.device
            )[:contextual_num]
            target_tokens = metric_normalized[:, target_indices, :]

            tokens_to_merge = metric_normalized[
                :,
                ~torch.isin(
                    torch.arange(
                        metric_normalized.shape[1], device=metric_normalized.device
                    ),
                    target_indices,
                ),
                :,
            ]
            similarity = torch.bmm(tokens_to_merge, target_tokens.transpose(1, 2))
            assign_one_hot = torch.zeros(
                tokens_to_merge.shape[0],
                tokens_to_merge.shape[1],
                contextual_num,
                dtype=hidden_states_filtered.dtype,
                device=metric_normalized.device,
            )
            assign_one_hot.scatter_(2, similarity.argmax(dim=2).unsqueeze(-1), 1)
            counts = assign_one_hot.sum(dim=1).clamp(min=1).unsqueeze(-1)
            hidden_to_merge = hidden_states_filtered[
                :,
                ~torch.isin(
                    torch.arange(
                        hidden_states_filtered.shape[1],
                        device=hidden_states_filtered.device,
                    ),
                    target_indices,
                ),
                :,
            ]
            aggregated_hidden = (
                torch.bmm(assign_one_hot.transpose(1, 2), hidden_to_merge) / counts
            )
            target_hidden = hidden_states_filtered[:, target_indices, :]

            contextual_tokens = target_hidden + aggregated_hidden

            # Merge with target hidden states and concatenate
            hidden_states_save = torch.cat(
                [dominant_tokens, contextual_tokens], dim=1
            ).to(images.dtype)

        return hidden_states_save, all_indices


@TOKEN_REDUCTION_REGISTRY.register('VisionZip')
class VisionZip(TokenReductionModule):
    def __init__(self, config, model, blocks):
        super().__init__(config, model, blocks)
        self.add_sparse_config()
        self.register_reduction_modules()

    def add_sparse_config(self):
        special_config = self.config.get('special', {})
        self.dominant = special_config.get('dominant', 192)
        self.contextual = special_config.get('contextual', 30)

    def register_reduction_modules(self):

        def visionzip_hook(m, images, image_forward_outs):
            attn_weights = image_forward_outs.attentions[-2]
            hidden_states = image_forward_outs.hidden_states[-2]
            metric = self.blocks[-2].self_attn.k_proj.metric
            dominant_num = m._info['dominant']
            contextual_num = m._info['contextual']

            # Dominant Visual Tokens
            cls_idx = 0
            cls_attention = attn_weights[:, :, cls_idx, cls_idx + 1:]
            cls_attention_sum = cls_attention.sum(dim=1)
            topk_indices = cls_attention_sum.topk(dominant_num, dim=1).indices + 1
            all_indices = torch.cat(
                [
                    torch.zeros(
                        (hidden_states.shape[0], 1),
                        dtype=topk_indices.dtype,
                        device=topk_indices.device,
                    ),
                    topk_indices,
                ],
                dim=1,
            )

            mask = torch.ones_like(
                hidden_states[:, :, 0], dtype=torch.bool, device=metric.device
            ).scatter_(1, all_indices, False)
            dominant_tokens = hidden_states.masked_select(~mask.unsqueeze(-1)).view(
                hidden_states.shape[0], dominant_num + 1, hidden_states.shape[2]
            )

            # Filter
            metric_filtered = metric[mask].view(
                hidden_states.shape[0],
                hidden_states.shape[1] - (dominant_num + 1),
                metric.shape[2],
            )

            hidden_states_filtered = hidden_states.masked_select(
                mask.unsqueeze(-1)
            ).view(
                hidden_states.shape[0],
                hidden_states.shape[1] - (dominant_num + 1),
                hidden_states.shape[2],
            )

            metric_normalized = metric_filtered / metric_filtered.norm(
                dim=-1, keepdim=True
            )

            # Contextual Visual Tokens
            step = max(1, metric_normalized.shape[1] // contextual_num)
            target_indices = torch.arange(
                0, metric_normalized.shape[1], step, device=metric_normalized.device
            )[:contextual_num]
            target_tokens = metric_normalized[:, target_indices, :]

            tokens_to_merge = metric_normalized[
                :,
                ~torch.isin(
                    torch.arange(
                        metric_normalized.shape[1], device=metric_normalized.device
                    ),
                    target_indices,
                ),
                :,
            ]
            similarity = torch.bmm(tokens_to_merge, target_tokens.transpose(1, 2))
            assign_one_hot = torch.zeros(
                tokens_to_merge.shape[0],
                tokens_to_merge.shape[1],
                contextual_num,
                dtype=hidden_states_filtered.dtype,
                device=metric_normalized.device,
            )
            assign_one_hot.scatter_(2, similarity.argmax(dim=2).unsqueeze(-1), 1)
            counts = assign_one_hot.sum(dim=1).clamp(min=1).unsqueeze(-1)
            hidden_to_merge = hidden_states_filtered[
                :,
                ~torch.isin(
                    torch.arange(
                        hidden_states_filtered.shape[1],
                        device=hidden_states_filtered.device,
                    ),
                    target_indices,
                ),
                :,
            ]
            aggregated_hidden = (
                torch.bmm(assign_one_hot.transpose(1, 2), hidden_to_merge) / counts
            )
            target_hidden = hidden_states_filtered[:, target_indices, :]

            contextual_tokens = target_hidden + aggregated_hidden

            # Merge with target hidden states and concatenate
            hidden_states_save = torch.cat(
                [dominant_tokens, contextual_tokens], dim=1
            ).to(images[0].dtype)

            res = list(image_forward_outs.hidden_states)
            res[-2] = hidden_states_save.contiguous()
            image_forward_outs.hidden_states = tuple(res)

        def store_key_hook(m, x, outputs):
            bsz = x[0].shape[0]
            raw_outputs = (
                outputs.view(bsz, -1, m.num_heads, m.head_dim)
                .transpose(1, 2)
                .contiguous()
            )
            m.metric = raw_outputs.clone().mean(1)

        # output_attentions
        def update_output_attentions_hook(module, args, kwargs):
            kwargs['output_attentions'] = True
            return args, kwargs

        apply_info(
            self.model.vlm_model.vision_tower,
            dominant_num=self.dominant,
            contextual_num=self.contextual,
        )

        self.model.vlm_model.__class__.forward = visionzip_forward
        self.model.vlm_model.vision_tower.register_forward_pre_hook(
            update_output_attentions_hook, with_kwargs=True
        )

        self.blocks = self.model.vlm_model.vision_tower.vision_model.encoder.layers
        r = self.model.vlm_model.vision_tower.r
        for idx, block in enumerate(self.blocks):
            if r[idx]:
                block.self_attn.k_proj.num_heads = block.self_attn.num_heads
                block.self_attn.k_proj.head_dim = block.self_attn.head_dim
                block.self_attn.k_proj.register_forward_hook(store_key_hook)

        self.model.vlm_model.vision_tower.register_forward_hook(visionzip_hook)
