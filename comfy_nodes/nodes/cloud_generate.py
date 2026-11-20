"""云端生图：提示词 → 云端出图模型；接了参考图时改走 edit。"""
from __future__ import annotations

from typing import Any

import torch
from PIL import Image

from game_art.providers.base import ImageRequest

from .. import common
from ..common import (
    CATEGORY,
    IMAGE_OUT_TOOLTIPS,
    META_IN,
    N_IN,
    NEGATIVE_IN,
    PROMPT_IN,
    REFS_IN,
    TAG_IN,
    TRANSPARENT_IN,
    changed_unless_seeded,
    ga_errors,
    image_meta,
    model_input,
    parse_size,
    resolve_tag,
    size_input,
    unify,
)
from ..tensors import pil_to_tensors


class GACloudGenerate:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK", "GA_META")
    RETURN_NAMES = ("image", "mask", "meta")
    DESCRIPTION = ("云端生图：把提示词发给云端出图模型，拿回图片。这是整条流程里主要花钱的一步，"
                   "费用 = 单张价格 × 张数。")
    OUTPUT_TOOLTIPS = IMAGE_OUT_TOOLTIPS

    @classmethod
    def IS_CHANGED(cls, seed: int = -1, **kw: Any) -> float:
        return changed_unless_seeded(cls, seed, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "prompt": PROMPT_IN,
                "negative": NEGATIVE_IN,
                "size": size_input(),
                "n": N_IN,
                "transparent": TRANSPARENT_IN,
                "seed": ("INT", {"default": -1, "min": -1, "max": 2**53 - 1, "tooltip": (
                    "随机种子，决定「同样的提示词会不会出同样的图」。\n"
                    "· -1：每次随机，每点一次运行都会重新出图并计费。\n"
                    "· 固定数字（如 42）：只要其他参数没变，再点运行会直接用上次的结果，不重新出图、不计费。\n"
                    "注意：有的出图模型（如 gpt-image-1）不保证相同种子得到完全相同的画面。")}),
                "profile": model_input("image", "出图"),
            },
            "optional": {
                "refs": ("GA_REFS", {"tooltip": (
                    REFS_IN[1]["tooltip"] + "接上且里面有图时，会把参考图一并发给模型（走「按参考图生图」，"
                    "用下面的 edit_profile），让画风更贴近；不接或参考图为空时就是普通的文字出图。")}),
                "meta": META_IN, "tag": TAG_IN,
                # 有参考图时改走 edit，用这个 profile
                "edit_profile": model_input("image_edit", "有参考图时（这时走的是「按参考图生图」）出图"),
            },
        }

    @ga_errors
    def run(self, prompt: str, negative: str, size: str = "1024x1024", n: int = 1, transparent: bool = False,
            seed: int = -1, profile: str = "image", refs: list[Image.Image] | None = None,
            meta: dict[str, Any] | None = None, tag: str = "",
            edit_profile: str = "image_edit") -> tuple[torch.Tensor, torch.Tensor, dict[str, Any]]:
        wh = parse_size(size)
        tag = resolve_tag(tag, meta)
        router = common.get_router()
        refs = list(refs or [])
        req = ImageRequest(prompt=prompt, negative=negative, size=f"{wh[0]}x{wh[1]}", n=n,
                           transparent=transparent, seed=None if seed < 0 else seed, refs=refs)
        if refs:
            result, target = router.edit(edit_profile, req, tag=tag)
        else:
            result, target = router.generate(profile, req, tag=tag)
        images = unify(result.images, wh)
        image, mask = pil_to_tensors(images)
        out_meta = image_meta(router, meta, result=result, target=target, prompt=prompt,
                              negative=negative, size=req.size, tag=tag)
        return (image, mask, out_meta)
