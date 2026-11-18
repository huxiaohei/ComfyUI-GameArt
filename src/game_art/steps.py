"""AI 步骤：提示词扩写 / 参考图分析 / 质检打分。"""
from __future__ import annotations

import json
import math
import re
from importlib import resources
from string import Template
from typing import Any, Protocol

from PIL import Image
from pydantic import BaseModel

from .config import Target
from .errors import InputError, UpstreamError
from .imaging import flatten_for_review
from .providers.base import ChatResult, Message
from .style import StyleCard


class ChatRouter(Protocol):
    """steps 只依赖 router 的 chat 接口（便于测试替身）。"""

    def chat(self, profile: str, messages: list[Message], *, tag: str,
             json_mode: bool = False) -> tuple[ChatResult, Target]: ...


class ExpandedPrompt(BaseModel):
    prompt: str
    negative: str


class QCResult(BaseModel):
    score: float
    issues: list[str]
    passed: bool


def _template(name: str) -> Template:
    text = resources.files("game_art").joinpath("prompts", name).read_text(encoding="utf-8")
    return Template(text)


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S | re.I)


def _parse_json(text: str) -> dict[str, Any]:
    s = text.strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1)
    obj = json.loads(s)
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层不是对象")
    return obj


def _style_text(style: StyleCard | None) -> str:
    return style.to_prompt_text() if style else "（无风格卡）"


def _merge(base: str, extra: str) -> str:
    extra = extra.strip()
    if not extra or extra in base:
        return base
    return f"{base}, {extra}" if base else extra


# 资产类型 → 给扩写模型看的画面要求。节点上是下拉选项；不在表里的文本原样交给模型。
ASSET_TYPES: dict[str, str] = {
    "icon": "图标：单个主体居中，轮廓清晰，背景干净",
    "prop": "道具：单个物件，完整无裁切，背景干净",
    "character": "角色：单个角色，全身完整无裁切，背景干净",
    "ui_element": "界面元素：按钮、边框、徽章等，形状规整，边缘干净",
    "background": "背景/场景：铺满整个画面，不留边，不需要透明底",
    "illustration": "插画：完整构图的宣传图或活动图，铺满画面",
}


def describe_asset_type(asset_type: str) -> str:
    desc = ASSET_TYPES.get(asset_type)
    return f"{asset_type}（{desc}）" if desc else asset_type


def expand_prompt(router: ChatRouter, brief: str, *, asset_type: str, style: StyleCard | None,
                  ref_description: str = "", tag: str, profile: str = "text") -> ExpandedPrompt:
    system = _template("expand.md").safe_substitute(
        brief=brief, asset_type=describe_asset_type(asset_type), style=_style_text(style),
        ref_description=ref_description or "（无）")
    messages = [Message(role="system", text=system),
                Message(role="user", text=brief)]
    last_err = ""
    for _ in range(2):
        res, _target = router.chat(profile, messages, tag=tag, json_mode=True)
        try:
            obj = _parse_json(res.text)
            raw_prompt, raw_neg = obj["prompt"], obj.get("negative")
            if not isinstance(raw_prompt, str):
                raise ValueError("prompt 不是字符串")
            if raw_neg is not None and not isinstance(raw_neg, str):
                raise ValueError("negative 不是字符串")
            prompt = raw_prompt.strip()
            negative = (raw_neg or "").strip()
            if not prompt:
                raise ValueError("prompt 为空")
            break
        except (ValueError, KeyError) as e:
            last_err = f"{e}; 原始输出: {res.text[:200]!r}"
    else:
        raise UpstreamError("bad_llm_json", f"模型两次都没有返回合法 JSON: {last_err}")
    if style:
        prompt = _merge(prompt, style.positive)
        negative = _merge(negative, style.negative)
    return ExpandedPrompt(prompt=prompt, negative=negative)


def analyze_reference(router: ChatRouter, images: list[Image.Image], *, focus: str = "风格",
                      tag: str, profile: str = "vision") -> str:
    if not images:
        raise InputError("no_reference_images", "参考图分析至少需要一张图片")
    system = _template("analyze.md").safe_substitute(focus=focus, count=len(images))
    messages = [Message(role="system", text=system),
                Message(role="user", text=f"请分析这些参考图的「{focus}」。", images=list(images))]
    res, _target = router.chat(profile, messages, tag=tag)
    return res.text.strip()


def qc_score(router: ChatRouter, img: Image.Image, *, brief: str, style: StyleCard | None,
             threshold: float = 6.0, tag: str, profile: str = "vision") -> QCResult:
    system = _template("qc.md").safe_substitute(brief=brief, style=_style_text(style))
    messages = [Message(role="system", text=system),
                Message(role="user", text="请按要求给这张图打分。", images=[flatten_for_review(img)])]
    res, _target = router.chat(profile, messages, tag=tag, json_mode=True)
    try:
        obj = _parse_json(res.text)
        raw_score, raw_issues = obj["score"], obj.get("issues")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise ValueError("score 不是数字")
        score = float(raw_score)
        if not math.isfinite(score):
            raise ValueError("score 不是有限数")
        if raw_issues is None:
            raw_issues = []
        if not isinstance(raw_issues, list) or not all(isinstance(i, str) for i in raw_issues):
            raise ValueError("issues 不是字符串列表")
        issues = raw_issues
    except (ValueError, KeyError, TypeError) as e:
        raise UpstreamError("bad_llm_json",
                            f"质检返回的不是合法 JSON: {e}; 原始输出: {res.text[:200]!r}") from e
    score = max(0.0, min(10.0, score))
    return QCResult(score=score, issues=issues, passed=score >= threshold)
