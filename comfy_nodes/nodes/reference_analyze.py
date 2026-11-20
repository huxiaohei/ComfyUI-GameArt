"""参考图分析：视觉模型把参考图描述成文字，供提示词扩写参考。"""
from __future__ import annotations

from typing import Any

import torch
from PIL import Image

from game_art.steps import analyze_reference

from .. import common
from ..common import (
    CATEGORY,
    REFS_IN,
    TAG_IN,
    always_changed,
    collect_refs,
    ga_errors,
    model_input,
    resolve_tag,
)


class GAReferenceAnalyze:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("description",)
    DESCRIPTION = "参考图分析：让能看图的模型把参考图的风格、配色等写成一段文字，供「提示词扩写」参考。"
    OUTPUT_TOOLTIPS = ("对参考图的文字描述，接到「提示词扩写」的 ref_description。",)

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "focus": ("STRING", {"default": "风格", "tooltip": (
                    "让模型重点看参考图的哪方面，随意填写，例如：风格、配色、构图、造型。")}),
                "profile": model_input("vision", "看图分析"),
            },
            "optional": {
                "refs": ("GA_REFS", {"tooltip": REFS_IN[1]["tooltip"] + "refs 和 images 至少接一个。"}),
                "images": ("IMAGE", {"tooltip": "要分析的图片，接「加载图像」节点。"}),
                "tag": TAG_IN,
            },
        }

    @ga_errors
    def run(self, focus: str = "风格", profile: str = "vision", refs: list[Image.Image] | None = None,
            images: torch.Tensor | None = None, tag: str = "") -> tuple[str]:
        text = analyze_reference(common.get_router(), collect_refs(images, refs), focus=focus,
                                 tag=resolve_tag(tag), profile=profile)
        return (text,)
