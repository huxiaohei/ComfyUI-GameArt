"""无缝贴图：把一张材质贴图处理成能首尾相接平铺的，并给出一张铺开后的预览。"""
from __future__ import annotations

from typing import Any

import torch

from game_art import imaging

from ..common import CATEGORY, ga_errors
from ..tensors import pil_to_tensors, tensors_to_pils


class GAMakeSeamless:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK", "IMAGE")
    RETURN_NAMES = ("image", "mask", "preview")
    DESCRIPTION = ("无缝贴图：把草地、土地、石板、水面这类材质图处理成能上下左右首尾相接平铺的贴图，"
                   "在本机处理，不花钱。"
                   "用来做瓦片地图的地面和路面。图上有单独的物体（一棵树、一块大石头）时不适用，过渡的地方会出现重影。")
    OUTPUT_TOOLTIPS = ("处理好的无缝贴图，尺寸不变，接到「保存资产」。",
                       "透明度蒙版，和 image 一起接到下游；没接 mask 时是全不透明。",
                       "预览：把处理好的贴图横竖各铺两遍拼成的图，接「预览图像」节点，看接缝的地方是否自然。")

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要处理的贴图。一次给多张时每张都处理。"}),
                "blend": ("FLOAT", {"default": 0.25, "min": 0.05, "max": 0.5, "step": 0.05, "tooltip": (
                    "过渡带的宽度，是图片边长的几分之几，默认 0.25。\n"
                    "调大：接缝更自然，但图片四周更大一圈会被改动，纹理大的贴图容易出现重影。\n"
                    "调小：改动的范围小，但过渡生硬，可能看得出一条线。")}),
            },
            "optional": {"mask": ("MASK", {"tooltip": "上游的透明度蒙版。贴图一般是不透明的，可以不接。"})},
        }

    @ga_errors
    def run(self, image: torch.Tensor, blend: float = 0.25,
            mask: torch.Tensor | None = None) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        tiles = [imaging.make_seamless(im, blend) for im in tensors_to_pils(image, mask)]
        out_image, out_mask = pil_to_tensors(tiles)
        preview, _ = pil_to_tensors([imaging.flatten_for_review(imaging.tile_preview(t)) for t in tiles])
        return (out_image, out_mask, preview)
