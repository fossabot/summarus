from typing import Dict, Tuple, List
import numpy as np

import torch
import torch.nn.functional as F
from torch.nn.modules.linear import Linear
from torch.nn.modules.rnn import LSTMCell

from allennlp.common.util import START_SYMBOL, END_SYMBOL
from allennlp.data.vocabulary import Vocabulary, DEFAULT_OOV_TOKEN
from allennlp.modules import TextFieldEmbedder, Seq2SeqEncoder
from allennlp.models.model import Model
from allennlp.modules.token_embedders import Embedding
from allennlp.modules import Attention
from allennlp.nn.beam_search import BeamSearch
from allennlp.nn import util


@Model.register("pgn")
class PointerGeneratorNetwork(Model):
    def __init__(self,
                 vocab: Vocabulary,
                 source_embedder: TextFieldEmbedder,
                 encoder: Seq2SeqEncoder,
                 attention: Attention,
                 max_decoding_steps: int,
                 beam_size: int = None,
                 target_namespace: str = "tokens",
                 target_embedding_dim: int = None,
                 scheduled_sampling_ratio: float = 0.,
                 projection_dim: int = None,
                 use_coverage: bool = False,
                 coverage_loss_weight: float = None) -> None:
        super(PointerGeneratorNetwork, self).__init__(vocab)

        self._target_namespace = target_namespace
        self._start_index = self.vocab.get_token_index(START_SYMBOL, self._target_namespace)
        self._end_index = self.vocab.get_token_index(END_SYMBOL, self._target_namespace)
        self._source_unk_index = self.vocab.get_token_index(DEFAULT_OOV_TOKEN)
        self._target_unk_index = self.vocab.get_token_index(DEFAULT_OOV_TOKEN, self._target_namespace)
        self._source_vocab_size = self.vocab.get_vocab_size()
        self._target_vocab_size = self.vocab.get_vocab_size(self._target_namespace)

        # Encoder
        self._source_embedder = source_embedder
        self._encoder = encoder
        self._encoder_output_dim = self._encoder.get_output_dim()

        # Decoder
        self._target_embedding_dim = target_embedding_dim or source_embedder.get_output_dim()
        self._num_classes = self.vocab.get_vocab_size(self._target_namespace)
        self._target_embedder = Embedding(self._num_classes, self._target_embedding_dim)
        self._decoder_input_dim = self._encoder_output_dim + self._target_embedding_dim
        self._decoder_output_dim = self._encoder_output_dim
        self._decoder_cell = LSTMCell(self._decoder_input_dim, self._decoder_output_dim)
        self._projection_dim = projection_dim or self._source_embedder.get_output_dim()
        self._hidden_projection_layer = Linear(self._decoder_output_dim, self._projection_dim)
        self._output_projection_layer = Linear(self._projection_dim, self._num_classes)
        self._p_gen_layer = Linear(self._decoder_output_dim * 3 + self._decoder_input_dim, 1)
        self._attention = attention
        self._use_coverage = use_coverage
        self._coverage_loss_weight = coverage_loss_weight
        self._eps = 1e-31

        # Decoding
        self._scheduled_sampling_ratio = scheduled_sampling_ratio
        self._max_decoding_steps = max_decoding_steps
        self._beam_search = BeamSearch(self._end_index, max_steps=max_decoding_steps, beam_size=beam_size or 1)

    def forward(self,
                source_tokens: Dict[str, torch.LongTensor],
                source_token_ids: torch.Tensor,
                source_to_target: torch.Tensor,
                target_tokens: Dict[str, torch.LongTensor] = None,
                target_token_ids: torch.Tensor = None,
                metadata=None) -> Dict[str, torch.Tensor]:
        state = self._encode(source_tokens)
        target_tokens_tensor = target_tokens["tokens"].long() if target_tokens else None
        extra_zeros, modified_source_tokens, modified_target_tokens = self._prepare(
            source_to_target, source_token_ids, target_tokens_tensor, target_token_ids)

        state["tokens"] = modified_source_tokens
        state["extra_zeros"] = extra_zeros

        output_dict = {}
        if target_tokens:
            state["target_tokens"] = modified_target_tokens
            state = self._init_decoder_state(state)
            output_dict = self._forward_loop(state, target_tokens)
        output_dict["metadata"] = metadata
        output_dict["source_to_target"] = source_to_target

        if not self.training:
            state = self._init_decoder_state(state)
            predictions = self._forward_beam_search(state)
            output_dict.update(predictions)

        return output_dict

    def _prepare(self,
                 source_tokens: torch.LongTensor,
                 source_token_ids: torch.Tensor,
                 target_tokens: torch.LongTensor = None,
                 target_token_ids: torch.Tensor = None):
        batch_size = source_tokens.size(0)
        source_max_length = source_tokens.size(1)

        tokens = source_tokens
        token_ids = source_token_ids.long()
        if target_tokens is not None:
            tokens = torch.cat((tokens, target_tokens), 1)
            token_ids = torch.cat((token_ids, target_token_ids.long()), 1)

        is_unk = torch.eq(tokens, self._target_unk_index).long()
        unk_only = token_ids * is_unk

        unk_token_nums = token_ids.new_zeros((batch_size, token_ids.size(1)))
        for i in range(batch_size):
            unique = torch.unique(unk_only[i, :], return_inverse=True, sorted=True)[1]
            unk_token_nums[i, :] = unique

        tokens = tokens - tokens * is_unk + (self._target_vocab_size - 1) * is_unk + unk_token_nums

        modified_target_tokens = None
        modified_source_tokens = tokens
        if target_tokens is not None:
            for i in range(batch_size):
                max_source_num = torch.max(tokens[i, :source_max_length])
                max_source_num = max(self._target_vocab_size - 1, max_source_num)
                unk_target_tokens_mask = torch.gt(tokens[i, :], max_source_num).long()
                zero_target_unk = tokens[i, :] - tokens[i, :] * unk_target_tokens_mask
                tokens[i, :] = zero_target_unk + self._target_unk_index * unk_target_tokens_mask
            modified_target_tokens = tokens[:, source_max_length:]
            modified_source_tokens = tokens[:, :source_max_length]

        source_unk_count = torch.max(unk_token_nums[:, :source_max_length])
        extra_zeros = tokens.new_zeros((batch_size, source_unk_count), dtype=torch.float32)
        return extra_zeros, modified_source_tokens, modified_target_tokens

    def _encode(self, source_tokens: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        # shape: (batch_size, max_input_sequence_length, encoder_input_dim)
        embedded_input = self._source_embedder.forward(source_tokens)
        # shape: (batch_size, max_input_sequence_length)
        source_mask = util.get_text_field_mask(source_tokens)
        # shape: (batch_size, max_input_sequence_length, encoder_output_dim)
        encoder_outputs = self._encoder.forward(embedded_input, source_mask)

        return {
                "source_mask": source_mask,
                "encoder_outputs": encoder_outputs,
        }

    def _init_decoder_state(self, state: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        batch_size = state["source_mask"].size(0)
        # shape: (batch_size, encoder_output_dim)
        final_encoder_output = util.get_final_encoder_states(
                state["encoder_outputs"],
                state["source_mask"],
                self._encoder.is_bidirectional())
        # Initialize the decoder hidden state with the final output of the encoder.
        # shape: (batch_size, decoder_output_dim)
        state["decoder_hidden"] = final_encoder_output

        encoder_outputs = state["encoder_outputs"]
        state["decoder_context"] = encoder_outputs.new_zeros(batch_size, self._decoder_output_dim)
        if self._use_coverage:
            state["coverage"] = encoder_outputs.new_zeros(batch_size, encoder_outputs.size(1))
        return state

    def _prepare_output_projections(self,
                                    last_predictions: torch.Tensor,
                                    state: Dict[str, torch.Tensor]) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        # shape: (group_size, max_input_sequence_length, encoder_output_dim)
        encoder_outputs = state["encoder_outputs"]
        # shape: (group_size, max_input_sequence_length)
        source_mask = state["source_mask"]
        # shape: (group_size, decoder_output_dim)
        decoder_hidden = state["decoder_hidden"]
        # shape: (group_size, decoder_output_dim)
        decoder_context = state["decoder_context"]

        is_unk = (last_predictions >= self._target_vocab_size).long()
        last_predictions_fixed = last_predictions - last_predictions * is_unk + self._target_unk_index * is_unk
        embedded_input = self._target_embedder.forward(last_predictions_fixed)

        if not self._use_coverage:
            attn_scores = self._attention.forward(decoder_hidden, encoder_outputs, source_mask)
        else:
            coverage = state["coverage"]
            attn_scores = self._attention.forward(decoder_hidden, encoder_outputs, source_mask, coverage)
            coverage = coverage + attn_scores
            state["coverage"] = coverage
        attn_context = util.weighted_sum(encoder_outputs, attn_scores)
        decoder_input = torch.cat((attn_context, embedded_input), -1)

        decoder_hidden, decoder_context = self._decoder_cell(
            decoder_input,
            (decoder_hidden, decoder_context))

        output_projections = self._output_projection_layer(self._hidden_projection_layer(decoder_hidden))

        state["decoder_input"] = decoder_input
        state["decoder_hidden"] = decoder_hidden
        state["decoder_context"] = decoder_context
        state["attn_scores"] = attn_scores
        state["attn_context"] = attn_context

        return output_projections, state

    def _get_final_dist(self, state: Dict[str, torch.Tensor], output_projections):
        attn_dist = state["attn_scores"]
        tokens = state["tokens"]
        extra_zeros = state["extra_zeros"]
        attn_context = state["attn_context"]
        decoder_input = state["decoder_input"]
        decoder_hidden = state["decoder_hidden"]
        decoder_context = state["decoder_context"]

        decoder_state = torch.cat((decoder_hidden, decoder_context), 1)
        p_gen = self._p_gen_layer(torch.cat((attn_context, decoder_state, decoder_input), 1))
        p_gen = torch.sigmoid(p_gen)

        vocab_dist = F.softmax(output_projections, dim=-1)

        vocab_dist = vocab_dist * p_gen
        attn_dist = attn_dist * (1.0 - p_gen)
        if extra_zeros.size(1) != 0:
            vocab_dist = torch.cat((vocab_dist, extra_zeros), 1)
        final_dist = vocab_dist.scatter_add(1, tokens, attn_dist)
        normalization_factor = final_dist.sum(1, keepdim=True)
        final_dist = final_dist / normalization_factor

        return final_dist

    def _forward_loop(self,
                      state: Dict[str, torch.Tensor],
                      target_tokens: Dict[str, torch.LongTensor] = None) -> Dict[str, torch.Tensor]:
        # shape: (batch_size, max_input_sequence_length)
        source_mask = state["source_mask"]
        batch_size = source_mask.size(0)

        num_decoding_steps = self._max_decoding_steps
        if target_tokens:
            # shape: (batch_size, max_target_sequence_length)
            targets = target_tokens["tokens"]
            _, target_sequence_length = targets.size()
            num_decoding_steps = target_sequence_length - 1

        last_predictions = source_mask.new_full((batch_size,), fill_value=self._start_index)

        step_proba: List[torch.Tensor] = []
        step_predictions: List[torch.Tensor] = []
        if self._use_coverage:
            coverage_loss = None
        for timestep in range(num_decoding_steps):
            if self.training and torch.rand(1).item() < self._scheduled_sampling_ratio:
                input_choices = last_predictions
            elif not target_tokens:
                input_choices = last_predictions
            else:
                input_choices = targets[:, timestep]

            if self._use_coverage:
                coverage = state["coverage"]

            output_projections, state = self._prepare_output_projections(input_choices, state)
            final_dist = self._get_final_dist(state, output_projections)
            step_proba.append(final_dist)

            if self._use_coverage:
                step_coverage_loss = torch.sum(torch.min(state["attn_scores"], coverage), 1)
                coverage_loss = coverage_loss + step_coverage_loss if coverage_loss is not None else step_coverage_loss

            _, predicted_classes = torch.max(final_dist, 1)
            last_predictions = predicted_classes
            step_predictions.append(last_predictions.unsqueeze(1))

        # shape: (batch_size, num_decoding_steps)
        predictions = torch.cat(step_predictions, 1)

        output_dict = {"predictions": predictions}

        if target_tokens:
            # shape: (batch_size, num_decoding_steps, num_classes)
            num_classes = step_proba[0].size(1)
            proba = step_proba[0].new_zeros((batch_size, num_classes, len(step_proba)))
            for i, p in enumerate(step_proba):
                proba[:, :, i] = p

            loss = self._get_loss(proba, state["target_tokens"], self._eps)
            if self._use_coverage:
                coverage_loss = torch.mean(coverage_loss / num_decoding_steps)
                loss = loss + self._coverage_loss_weight * coverage_loss
            output_dict["loss"] = loss

        return output_dict

    @staticmethod
    def _get_loss(proba: torch.LongTensor,
                  targets: torch.LongTensor,
                  eps: float) -> torch.Tensor:
        targets = targets[:, 1:]
        proba = torch.log(proba + eps)
        loss = torch.nn.NLLLoss(ignore_index=0)(proba, targets)
        return loss

    def _forward_beam_search(self, state: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        batch_size = state["source_mask"].size()[0]
        start_predictions = state["source_mask"].new_full((batch_size,), fill_value=self._start_index)

        # shape (all_top_k_predictions): (batch_size, beam_size, num_decoding_steps)
        # shape (log_probabilities): (batch_size, beam_size)
        all_top_k_predictions, log_probabilities = self._beam_search.search(
            start_predictions, state, self.take_step)

        output_dict = {
            "class_log_probabilities": log_probabilities,
            "predictions": all_top_k_predictions,
        }
        return output_dict

    def take_step(self,
                  last_predictions: torch.Tensor,
                  state: Dict[str, torch.Tensor]) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        # shape: (group_size, num_classes)
        output_projections, state = self._prepare_output_projections(last_predictions, state)
        final_dist = self._get_final_dist(state, output_projections)
        log_probabilities = torch.log(final_dist + self._eps)
        return log_probabilities, state

    def decode(self, output_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        predicted_indices = output_dict["predictions"]
        if not isinstance(predicted_indices, np.ndarray):
            predicted_indices = predicted_indices.detach().cpu().numpy()
        all_predicted_tokens = []
        for (indices, metadata), source_to_target in zip(zip(predicted_indices, output_dict["metadata"]), output_dict["source_to_target"]):
            # Beam search gives us the top k results for each source sentence in the batch
            # but we just want the single best.
            if len(indices.shape) > 1:
                indices = indices[0]
            indices = list(indices)
            # Collect indices till the first end_symbol
            if self._end_index in indices:
                indices = indices[:indices.index(self._end_index)]
            predicted_tokens = []

            unk_tokens = list()
            for i, t in enumerate(source_to_target):
                if t == self._target_unk_index:
                    token = metadata["source_tokens"][i]
                    if token not in unk_tokens:
                        unk_tokens.append(token)

            for x in indices:
                if x < self._target_vocab_size:
                    token = self.vocab.get_token_from_index(x, namespace=self._target_namespace)
                else:
                    unk_number = x - self._target_vocab_size
                    token = unk_tokens[unk_number]
                predicted_tokens.append(token)
            all_predicted_tokens.append(predicted_tokens)
        output_dict["predicted_tokens"] = all_predicted_tokens
        return output_dict
