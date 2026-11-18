"""去背景：本机 rembg 抠图，或 none 原样透传。"""
from __future__ import annotations

from typing import Any, Literal

import torch

from game_art import imaging

from ..common import CATEGORY, ga_errors
from ..tensors import pil_to_tensors, tensors_to_pils


class GARemoveBackground:
    """可选 mask（= 上游 1 - alpha）：none 时原样透传透明度；rembg 在合成后的 RGBA 上运行。"""
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    DESCRIPTION = "去背景：在本机把图片背景抠掉变成透明。method 选 none 时什么都不做，原样通过。"
    OUTPUT_TOOLTIPS = ("处理后的图片。", "处理后的透明度蒙版，和 image 一起接到下游。")

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要处理的图片。"}),
                "method": (["rembg", "none"], {"tooltip": (
                    "去背景的方式：\n"
                    "· none：不处理，原样通过。模型已经直接出了透明底（云端生图的 transparent 打开）时用它。\n"
                    "· rembg：在本机用抠图模型把主体抠出来，背景变透明，不花钱。"
                    "需要先给 ComfyUI 装 rembg（见 README「ComfyUI 自带 Python」）。")}),
            },
            "optional": {"mask": ("MASK", {"tooltip": "上游的透明度蒙版。接上才能保留上游已有的透明背景。"})},
        }

    @ga_errors
    def run(self, image: torch.Tensor, method: Literal["rembg", "none"] = "rembg",
            mask: torch.Tensor | None = None) -> tuple[torch.Tensor, torch.Tensor]:
        outs = [imaging.remove_background(im, method) for im in tensors_to_pils(image, mask)]
        return pil_to_tensors(outs)
