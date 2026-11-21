"""gifenc：最小 GIF 编码器（调色板 + LZW + 容器拼装）。

只依赖标准库，对外暴露 :class:`GifError` 与 8 个核心函数。
"""

from .palette import (
    MAX_COLORS,
    MIN_DELAY_CS,
    GifError,
    assemble,
    dither,
    encode_batch,
    lzw_encode,
    map_to_palette,
    median_cut,
    quantize_delay,
    validate_frames,
)

__all__ = [
    "MAX_COLORS",
    "MIN_DELAY_CS",
    "GifError",
    "assemble",
    "dither",
    "encode_batch",
    "lzw_encode",
    "map_to_palette",
    "median_cut",
    "quantize_delay",
    "validate_frames",
]
