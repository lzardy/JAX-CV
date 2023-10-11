from functools import partial

import einops
import jax.numpy as jnp
from flax import linen

from .SwinV2 import SwinTransformerV2


class SwinTransformerForSimMIM(SwinTransformerV2):
    def setup(self):
        super().setup()

        token_init = linen.initializers.normal(0.02)
        self.mask_token = self.param("mask_token", token_init, (1, 1, self.embed_dim))

    def __call__(self, x, mask, train: bool = False):
        x = self.patch_embed(x)

        B, L, _ = x.shape

        mask_tokens = jnp.broadcast_to(self.mask_token, (B, L, self.embed_dim))
        mask = jnp.reshape(mask, (B, L, 1)).astype(mask_tokens.dtype)
        x = x * (1.0 - mask) + mask_tokens * mask

        for layer in self.swin_body:
            x = layer(x, train=train)

        x = self.norm(x)

        B, L, C = x.shape
        H = W = int(L**0.5)
        x = jnp.reshape(x, (B, H, W, C))
        return x


class SimMIM(linen.Module):
    encoder: linen.Module
    encoder_stride: int

    patch_size: int
    in_chans: int = 3

    def setup(self):
        self.decoder = linen.Sequential(
            [
                linen.Conv(features=self.encoder_stride**2 * 3, kernel_size=(1, 1)),
                partial(
                    einops.rearrange,
                    pattern="... h w (c b1 b2) -> ... (h b1) (w b2) c",
                    b1=self.encoder_stride,
                    b2=self.encoder_stride,
                ),
            ]
        )

    def __call__(self, x, mask, train: bool = False):
        z = self.encoder(x, mask, train)
        x_rec = self.decoder(z)

        mask = jnp.expand_dims(
            jnp.repeat(
                jnp.repeat(mask, self.patch_size, axis=1),
                self.patch_size,
                axis=2,
            ),
            axis=-1,
        )
        loss_recon = jnp.abs(x - x_rec)
        loss = jnp.sum(loss_recon * mask) / (jnp.sum(mask) + 1e-5) / self.in_chans

        return loss, x_rec


def simmim_swinv2_tiny_window8_256(**kwargs):
    encoder = partial(
        SwinTransformerForSimMIM,
        embed_dim=96,
        window_size=8,
        depths=(2, 2, 6, 2),
        num_heads=(3, 6, 12, 24),
    )
    encoder = encoder(**kwargs)
    model = SimMIM(encoder, 32, encoder.patch_size)
    return model


def simmim_swinv2_base_window8_256(**kwargs):
    encoder = partial(
        SwinTransformerForSimMIM,
        embed_dim=128,
        window_size=8,
        depths=(2, 2, 18, 2),
        num_heads=(4, 8, 16, 32),
    )
    encoder = encoder(**kwargs)
    model = SimMIM(encoder, 32, encoder.patch_size)
    return model
