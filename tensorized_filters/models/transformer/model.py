import torch
import torch.nn as nn

from transformers import PreTrainedModel

from tensorized_filters.utils.modules.attn import Attention
from tensorized_filters.utils.numerics import nearest_power_of_two
from tensorized_filters.models.transformer.config import TransformerConfig
from tensorized_filters.layers.attn import AttentionLayer

try:
    from liger_kernel.transformers.rms_norm import LigerRMSNorm as TritonNorm
    triton_norm = True
except ImportError as e:
    print(
        f"Unable to import Triton-based RMSNorm: {e}. Falling back to PyTorch implementation."
    )
    from torch.nn import RMSNorm

    triton_norm = False


class Transformer(PreTrainedModel):
    config_class = TransformerConfig

    def __init__(self, config) -> None:
        super(Transformer, self).__init__(config)
        self.n_layers = config.n_layers
        self.n = nearest_power_of_two(config.seq_len * 2 - 1, round_up=True)

        # TODO: Add support for Liger-Kernel Embedding once no longer experimental
        self.tok_emb = nn.Embedding(
            config.vocab_size, config.n_embd, dtype=config.torch_dtype
        )
        self.dropout = nn.Dropout(config.dropout)

        self.layers = nn.ModuleList()
        for _ in range(self.n_layers):
            self.layers.append(AttentionLayer(config))

        self.norm = (
            TritonNorm(config.n_embd)
            if triton_norm
            else RMSNorm(config.n_embd, dtype=config.torch_dtype)
        )
        # TODO: Write Issue in Liger-Kernel repo to support user-defined dtype for RMS Norm
        self.norm = self.norm.to(dtype=config.torch_dtype)
        self.lm_head = nn.Linear(
            config.n_embd, config.vocab_size, bias=config.bias, dtype=config.torch_dtype
        )
        self.tok_emb.weight = self.lm_head.weight

        self.std = (config.n_embd) ** -0.5
        self.apply(self._init_weights)
        print("Model Parameter Count: %.2fM\n" % (self._get_num_params() / 1e6,))

    def forward(self, x: torch.Tensor) -> torch.tensor:
        tok_emb = self.tok_emb(x)
        x = self.dropout(tok_emb)

        for layer in self.layers:
            x = layer(x)

        x = self.norm(x)
        y_hat = self.lm_head(x)

        return y_hat

    def _get_num_params(self):
        n_params = sum(p.numel() for p in self.parameters())
        if hasattr(self, "pos_emb") and self.pos_emb is not None:
            n_params -= self.pos_emb.weight.numel()
        if self.tok_emb.weight is not self.lm_head.weight:
            n_params -= self.tok_emb.weight.numel()
        return n_params

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            if hasattr(module, "SCALE_INIT"):
                self.std *= (2 * self.n_layers) ** -0.5
            torch.nn.init.normal_(module.weight, mean=0.0, std=self.std)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=self.std)
        elif isinstance(module, Attention):
            torch.nn.init.xavier_normal_(module.c_attn.weight)
            torch.nn.init.xavier_normal_(module.c_proj.weight)
            if module.c_attn.bias is not None:
                torch.nn.init.zeros_(module.c_attn.bias)
            if module.c_proj.bias is not None:
                torch.nn.init.zeros_(module.c_proj.bias)
