"""提示词扩写：大白话描述 + 风格卡 → 出图用的正/反向提示词。"""
from __future__ import annotations

from typing import Any

from game_art.steps import ASSET_TYPES, expand_prompt
from game_art.style import StyleCard

from .. import common
from ..common import CATEGORY, META_OUT, STYLE_IN, TAG_IN, Recorder, always_changed, ga_errors, model_input, resolve_tag


class GAPromptExpand:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "GA_META")
    RETURN_NAMES = ("prompt", "negative", "meta")
    DESCRIPTION = ("提示词扩写：把一句大白话描述，结合风格卡，交给文字模型改写成出图模型能用好的专业提示词。"
                   "每运行一次调用一次文字模型（费用很低）。")
    OUTPUT_TOOLTIPS = ("扩写后的正向提示词：希望画面里出现的内容。", "反向提示词：不希望画面里出现的内容。",
                       META_OUT)

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "brief": ("STRING", {"multiline": True, "default": "", "placeholder": "用一两句话描述想要的素材",
                                     "tooltip": (
                    "你想要什么素材，用大白话写一两句即可，例如「一枚国潮风铜钱图标」。"
                    "不用写专业提示词，这个节点会替你扩写。不要要求画文字，文字由模板合成。")}),
                "asset_type": (list(ASSET_TYPES), {"default": "icon", "tooltip": (
                    "素材的种类，决定构图方式：\n" + "\n".join(f"· {k}：{v}" for k, v in ASSET_TYPES.items()))}),
                "profile": model_input("text", "扩写提示词"),
            },
            "optional": {
                "style": STYLE_IN,
                "ref_description": ("STRING", {"default": "", "multiline": True,
                                               "placeholder": "参考图的文字描述（可留空）", "tooltip": (
                    "对参考图的文字描述，可留空。在「参考图 → 资源」流程里由「参考图分析」节点自动填入，"
                    "让扩写出的提示词贴近参考图。")}),
                "tag": TAG_IN,
            },
        }

    @ga_errors
    def run(self, brief: str, asset_type: str, profile: str = "text", style: StyleCard | None = None,
            ref_description: str = "", tag: str = "") -> tuple[str, str, dict[str, Any]]:
        tag = resolve_tag(tag)
        rec = Recorder(common.get_router())
        res = expand_prompt(rec, brief, asset_type=asset_type, style=style,
                            ref_description=ref_description, tag=tag, profile=profile)
        t = rec.target
        meta = {"brief": brief, "prompt": res.prompt, "negative": res.negative, "tag": tag,
                "text_model": f"{t.provider}:{t.model}" if t else None}
        return (res.prompt, res.negative, meta)
