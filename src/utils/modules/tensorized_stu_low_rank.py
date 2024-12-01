import torch
import torch.nn as nn

from tensorized_filters.utils.convolve import convolve, flash_convolve

try:
    from flashfftconv import FlashFFTConv

    flash_fft_available = True
except ImportError as e:
    print(
        f"Unable to import FlashFFTConv: {e}. Falling back to PyTorch implementation."
    )
    flash_fft_available = False


class STU(nn.Module):
    def __init__(self, config, phi, n) -> None:
        super(STU, self).__init__()
        self.config = config
        self.phi = phi
        self.n = n
        self.K = config.num_eigh ** 2
        self.d_in = config.n_embd
        self.d_out = config.n_embd
        self.factorize = config.factorize
        self.r = config.r
        self.use_hankel_L = config.use_hankel_L
        self.use_approx = config.use_approx
        self.flash_fft = (
            FlashFFTConv(self.n, dtype=torch.bfloat16)
            if config.use_flash_fft and flash_fft_available
            else None
        )

        if self.use_approx:
            self.M_inputs = nn.Parameter(torch.empty(self.d_in, self.d_out, dtype=config.torch_dtype))
            if self.factorize:
                self.M_i= nn.Parameter(torch.empty(self.K, self.r, dtype=config.torch_dtype))
                self.M_j = nn.Parameter(torch.empty(self.r, self.d_in, dtype=config.torch_dtype))
            else:
                self.M_filters = nn.Parameter(torch.empty(self.K, self.d_in, dtype=config.torch_dtype))
        else:
            if self.factorize:
                self.M_i_plus = nn.Parameter(torch.empty(self.K, self.d_out, self.r, dtype=config.torch_dtype))
                self.M_j_plus = nn.Parameter(torch.empty(self.K, self.r, self.d_in, dtype=config.torch_dtype))

                if not self.use_hankel_L:
                    self.M_i_minus = nn.Parameter(torch.empty(self.K, self.d_out, self.r, dtype=config.torch_dtype))
                    self.M_j_minus = nn.Parameter(torch.empty(self.K, self.r, self.d_in, dtype=config.torch_dtype))
            else:
                self.M_phi_plus = nn.Parameter(
                    torch.empty(self.K, self.d_out, self.d_in, dtype=config.torch_dtype)
                )
                if not self.use_hankel_L:
                    self.M_phi_minus = nn.Parameter(
                        torch.empty(self.K, self.d_out, self.d_in, dtype=config.torch_dtype)
                    )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.use_approx:
            # Contract inputs and filters over the K and d_in dimensions, then convolve
            x_proj = x @ self.M_inputs # (B, L, d_in) @ (d_in, d_out) -> (B, L, d_out)

            if self.factorize:
                phi_proj = torch.einsum("lk,kr,ri->li", self.phi, self.M_i, self.M_j)
            else:
                phi_proj = self.phi @ self.M_filters

            if self.flash_fft:
                spectral_plus, spectral_minus = flash_convolve(
                    x_proj, phi_proj, self.flash_fft, self.use_approx
                ) # conv(x_proj, phi_proj) -> (B, L, d_out)
            else:
                spectral_plus, spectral_minus = convolve(
                    x_proj, phi_proj, self.n, self.use_approx
                )
        else:
            # Convolve inputs and filters, (# conv(x, phi) -> (B, L, K, d_in)),
            if self.flash_fft:
                U_plus, U_minus = flash_convolve(
                    x, self.phi, self.flash_fft, self.use_approx
                )
            else:
                U_plus, U_minus = convolve(x, self.phi, self.n, self.use_approx)

            if self.factorize:
                spectral_plus = torch.einsum("kor,kri,blki->blo", self.M_i_plus, self.M_j_plus, U_plus)
                if not self.use_hankel_L:
                    spectral_minus = torch.einsum("kor,kri,blki->blo", self.M_i_minus, self.M_j_minus, U_minus)
            else:
                # Then, contract over the K and d_in dimensions
                spectral_plus = torch.tensordot(U_plus, self.M_phi_plus, dims=([2, 3], [0, 2]))
                if not self.use_hankel_L:
                    spectral_minus = torch.tensordot(U_minus, self.M_phi_minus, dims=([2, 3], [0, 2]))

        return spectral_plus if self.use_hankel_L else spectral_plus + spectral_minus
