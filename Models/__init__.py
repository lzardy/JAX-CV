from .SimMIM import simmim_swinv2_base_window8_256, simmim_swinv2_tiny_window8_256
from .SwinV2 import swinv2_base_window8_256, swinv2_tiny_window8_256

model_registry = {
    "swinv2_tiny": swinv2_tiny_window8_256,
    "swinv2_base": swinv2_base_window8_256,
    "simmim_swinv2_tiny": simmim_swinv2_tiny_window8_256,
    "simmim_swinv2_base": simmim_swinv2_base_window8_256,
}
