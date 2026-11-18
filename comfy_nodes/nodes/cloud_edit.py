"""云端改图：参考图/原图 + 提示词（+ 可选蒙版）→ 云端 edit。"""
from __future__ import annotations

from typing import Any

import torch

from game_art.providers.base import ImageRequest

from .. import common
from ..common import (
    CATEGORY,
    IMAGE_OUT_TOOLTIPS,
    META_IN,
    N_IN,
    NEGATIVE_IN,
    PROMPT_IN,
    TAG_IN,
    TRANSPARENT_IN,
    always_changed,
    edit_mask,
    ga_errors,
    image_meta,
    model_input,
    parse_size,
    resolve_tag,
    size_input,
    unify,
)
from ..tensors import pil_to_tensors, tensors_to_pils


class GACloudEdit:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK", "GA_META")
    RETURN_NAMES = ("image", "mask", "meta")
    DESCRIPTION = "云端改图：把参考图和提示词一起发给云端模型，按参考图生成新图，或修改原图。按张计费。"
    OUTPUT_TOOLTIPS = IMAGE_OUT_TOOLTIPS

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "参考图或要修改的原图，接「加载图像」节点。"}),
                "prompt": PROMPT_IN,
                "negative": NEGATIVE_IN,
                "size": size_input(),
                "n": N_IN,
                "transparent": TRANSPARENT_IN,
                "profile": model_input("image_edit", "按参考图生图 / 改图"),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": (
                    "可选。指定只重画原图的哪一块：蒙版里涂白的区域会被重画，其余保持不变。不接则整张图都可以改。")}),
                "meta": META_IN, "tag": TAG_IN,
            },
        }

    @ga_errors
    def run(self, images: torch.Tensor, prompt: str, negative: str = "", size: str = "1024x1024", n: int = 1,
            transparent: bool = False, profile: str = "image_edit", mask: torch.Tensor | None = None,
            meta: dict[str, Any] | None = None,
            tag: str = "") -> tuple[torch.Tensor, torch.Tensor, dict[str, Any]]:
        wh = parse_size(size)
        tag = resolve_tag(tag, meta)
        router = common.get_router()
        refs = tensors_to_pils(images)
        req = ImageRequest(prompt=prompt, negative=negative, size=f"{wh[0]}x{wh[1]}", n=n,
                           transparent=transparent, refs=refs,
                           mask=edit_mask(mask, refs[0].size) if mask is not None else None)
        result, target = router.edit(profile, req, tag=tag)
        image, out_mask = pil_to_tensors(unify(result.images, wh))
        out_meta = image_meta(router, meta, result=result, target=target, prompt=prompt,
                              negative=negative, size=req.size, tag=tag)
        return (image, out_mask, out_meta)
