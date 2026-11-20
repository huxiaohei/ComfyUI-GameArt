"""质检打分：视觉模型给图片打 0~10 分并列出问题。"""
from __future__ import annotations

from typing import Any

import torch

from game_art.steps import qc_score
from game_art.style import StyleCard

from .. import common
from ..common import CATEGORY, TAG_IN, always_changed, ga_errors, model_input, resolve_tag
from ..tensors import tensors_to_pils


class GAQCScore:
    """批量输入时只给第一张图打分。可选 mask 让视觉模型看到透明区域（棋盘格）。"""
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("FLOAT", "STRING")
    RETURN_NAMES = ("score", "report")
    DESCRIPTION = "质检打分：让能看图的模型给图片打分（0 到 10），并列出问题。批量流程用它自动筛掉不合格的图。"
    OUTPUT_TOOLTIPS = ("得分，0 到 10。", "文字报告：得分、是否通过、发现的问题。")

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要打分的图片。一次给了多张时只给第一张打分。"}),
                "brief": ("STRING", {"multiline": True, "default": "", "tooltip": (
                    "这张图原本想要什么（和「提示词扩写」里写的描述一致），模型据此判断画得对不对。")}),
                "threshold": ("FLOAT", {"default": 7.0, "min": 0.0, "max": 10.0, "step": 0.5, "tooltip": (
                    "及格线，0 到 10 分。得分不低于它算通过。调高更严格，会有更多图被判为不合格。")}),
                "profile": model_input("vision", "看图质检"),
            },
            "optional": {
                "style": ("GA_STYLE", {"tooltip": "接「风格卡」，让模型同时检查是否符合风格。可不接。"}),
                "tag": TAG_IN,
                "mask": ("MASK", {"tooltip": "图片的透明度蒙版。接上后模型能看出哪里是透明的。可不接。"}),
            },
        }

    @ga_errors
    def run(self, image: torch.Tensor, brief: str, threshold: float = 7.0, profile: str = "vision",
            style: StyleCard | None = None, tag: str = "",
            mask: torch.Tensor | None = None) -> tuple[float, str]:
        img = tensors_to_pils(image[:1], mask[:1] if mask is not None else None)[0]
        r = qc_score(common.get_router(), img, brief=brief, style=style, threshold=threshold,
                     tag=resolve_tag(tag), profile=profile)
        verdict = "通过" if r.passed else "未通过"
        lines = [f"得分 {r.score:.1f}/10（{verdict}，阈值 {threshold:g}）"]
        lines += [f"- {i}" for i in r.issues]
        return (r.score, "\n".join(lines))
