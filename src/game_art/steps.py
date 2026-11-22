"""AI 步骤：提示词扩写 / 参考图分析 / 质检打分。"""
from __future__ import annotations

import hashlib
import json
import math
import re
from importlib import resources
from string import Template
from typing import Any, Literal, Protocol

from PIL import Image
from pydantic import BaseModel, Field

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


class QCIssue(BaseModel):
    """一条质检问题。level：must = 不改不能用；suggest = 改了更好的建议。"""
    text: str
    level: Literal["must", "suggest"] = "must"
    # layout = 构图类，要整张重画（留白、贴边、比例、姿势）；detail = 细节类，可以在原图上局部修改
    kind: Literal["layout", "detail"] = "detail"


class QCResult(BaseModel):
    score: float
    issues: list[str]  # 只有文字，和 details 一一对应（批量流程和旧记录用它）
    passed: bool
    details: list[QCIssue] = Field(default_factory=list)


class CommonIssue(BaseModel):
    """多张图共有的问题：count = 有几张图出现了它。"""
    text: str
    count: int


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


def feedback_lines(feedback: str) -> list[str]:
    """修订意见拆成一条条：去掉空行、行首的列表符号和重复项，保持先后顺序。"""
    out: list[str] = []
    for raw in (feedback or "").splitlines():
        line = raw.strip().lstrip("-•*·").strip()
        if line and line not in out:
            out.append(line)
    return out


def _feedback_text(feedback: str) -> str:
    lines = feedback_lines(feedback)
    return "（无）" if not lines else "".join(f"\n  {i}. {line}" for i, line in enumerate(lines, 1))


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
    "texture": "无缝贴图：从正上方垂直俯视的一整片材质（草地、土地、石板、水面等），均匀地铺满画面，"
               "没有单独的物体、没有明显的焦点和明暗方向，四边不留边框，便于首尾相接地平铺",
}


def describe_asset_type(asset_type: str) -> str:
    desc = ASSET_TYPES.get(asset_type)
    return f"{asset_type}（{desc}）" if desc else asset_type


def expand_prompt(router: ChatRouter, brief: str, *, asset_type: str, style: StyleCard | None,
                  ref_description: str = "", feedback: str = "", tag: str,
                  profile: str = "text") -> ExpandedPrompt:
    """feedback：上一版图片的修订意见（质检问题 + 使用者补充），一行一条；空 = 第一次出图。"""
    system = _template("expand.md").safe_substitute(
        brief=brief, asset_type=describe_asset_type(asset_type), style=_style_text(style),
        ref_description=ref_description or "（无）", feedback=_feedback_text(feedback))
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


def expand_key(brief: str, *, asset_type: str, style: StyleCard | None, ref_description: str = "",
               feedback: str = "", profile: str = "text") -> str:
    """扩写的"指纹"：这些输入都没变，扩写出来的提示词就可以沿用上次的。修订意见按整理后的条目算。"""
    payload = json.dumps({"brief": brief.strip(), "asset_type": asset_type, "profile": profile,
                          "style": style.model_dump() if style else None,
                          "ref_description": ref_description.strip(), "feedback": feedback_lines(feedback)},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def analyze_reference(router: ChatRouter, images: list[Image.Image], *, focus: str = "风格",
                      tag: str, profile: str = "vision") -> str:
    if not images:
        raise InputError("no_reference_images", "参考图分析至少需要一张图片")
    system = _template("analyze.md").safe_substitute(focus=focus, count=len(images))
    messages = [Message(role="system", text=system),
                Message(role="user", text=f"请分析这些参考图的「{focus}」。", images=list(images))]
    res, _target = router.chat(profile, messages, tag=tag)
    return res.text.strip()


DEFAULT_QC_THRESHOLD = 7.0  # 和 qc.md 的评分标准一致：7 分起才算"可用"


def _parse_issues(raw: Any) -> list[QCIssue]:
    """issues 的每一项可以是 {"text", "level"}，也可以是一句话（旧格式，按 must 处理）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("issues 不是列表")
    out: list[QCIssue] = []
    for item in raw:
        if isinstance(item, str):
            text, level, kind = item, "must", "detail"
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            text, level, kind = item["text"], item.get("level", "must"), item.get("kind", "detail")
        else:
            raise ValueError("issues 里有一项既不是字符串也不是带 text 的对象")
        text = text.strip()
        if text:
            out.append(QCIssue(text=text, level="suggest" if level == "suggest" else "must",
                               kind="layout" if kind == "layout" else "detail"))
    return out


def qc_score(router: ChatRouter, img: Image.Image, *, brief: str, style: StyleCard | None,
             threshold: float = DEFAULT_QC_THRESHOLD, tag: str, profile: str = "vision") -> QCResult:
    system = _template("qc.md").safe_substitute(brief=brief, style=_style_text(style))
    messages = [Message(role="system", text=system),
                Message(role="user", text="请按要求给这张图打分。", images=[flatten_for_review(img)])]
    res, _target = router.chat(profile, messages, tag=tag, json_mode=True)
    try:
        obj = _parse_json(res.text)
        raw_score = obj["score"]
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise ValueError("score 不是数字")
        score = float(raw_score)
        if not math.isfinite(score):
            raise ValueError("score 不是有限数")
        details = _parse_issues(obj.get("issues"))
    except (ValueError, KeyError, TypeError) as e:
        raise UpstreamError("bad_llm_json",
                            f"质检返回的不是合法 JSON: {e}; 原始输出: {res.text[:200]!r}") from e
    score = max(0.0, min(10.0, score))
    return QCResult(score=score, issues=[d.text for d in details], passed=score >= threshold, details=details)


def common_issues(router: ChatRouter, per_image: list[list[str]], *, tag: str,
                  profile: str = "text") -> list[CommonIssue]:
    """把多张图各自的问题归并成"共性问题"：意思相同的算同一条，统计有几张图出现；只返回至少两张图都有的。

    不足两张图有问题时不调用模型，直接返回空列表。
    """
    if sum(1 for issues in per_image if issues) < 2:
        return []
    listing = "\n".join(f"第 {i} 张：" + ("；".join(issues) if issues else "（无问题）")
                        for i, issues in enumerate(per_image, 1))
    system = _template("common_issues.md").safe_substitute(count=len(per_image))
    messages = [Message(role="system", text=system), Message(role="user", text=listing)]
    res, _target = router.chat(profile, messages, tag=tag, json_mode=True)
    try:
        raw = _parse_json(res.text).get("common")
        if not isinstance(raw, list):
            raise ValueError("common 不是列表")
        out: list[CommonIssue] = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ValueError("common 里有一项不是带 text 的对象")
            images = item.get("images")
            if not isinstance(images, list):
                raise ValueError("common 里有一项缺少 images 列表")
            hit = {n for n in images if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(per_image)}
            if len(hit) >= 2 and item["text"].strip():
                out.append(CommonIssue(text=item["text"].strip(), count=len(hit)))
    except (ValueError, KeyError, TypeError) as e:
        raise UpstreamError("bad_llm_json",
                            f"归并共性问题时返回的不是合法 JSON: {e}; 原始输出: {res.text[:200]!r}") from e
    return sorted(out, key=lambda c: -c.count)


# 修图时接在指令后面，说明几张图各自的角色（第一张是要改的原图，其余只管画风）
FIX_STYLE_REFS_NOTE = ("第一张图是要修改的原图。其余几张图只用来对照画风、线条和上色方式，"
                       "它们里面的人物、服装和道具都不要搬过来。")


def fix_instruction(router: ChatRouter, img: Image.Image, notes: list[str], *, brief: str,
                    style: StyleCard | None, tag: str, style_refs: int = 0,
                    profile: str = "vision") -> ExpandedPrompt:
    """看着要修改的这张图，把修改意见（质检问题 + 使用者补充）写成交给图片编辑模型的完整指令。

    指令里既有"保持什么"（对当前画面的如实描述）也有"改什么"，末尾原样拼上风格卡的 positive，
    这样画风不会因为修改而跑偏。style_refs > 0 表示改图时还会附带几张只管画风的参考图。
    """
    notes = [n.strip() for n in notes if n.strip()]
    if not notes:
        raise InputError("no_fix_notes", "没有修改意见：请至少勾选一条问题，或在「我的补充」里写上要改什么")
    listing = "".join(f"\n{i}. {n}" for i, n in enumerate(notes, 1))
    system = _template("fix.md").safe_substitute(brief=brief or "（无）", style=_style_text(style), notes=listing)
    messages = [Message(role="system", text=system),
                Message(role="user", text="这是要修改的图，请写出修改指令。", images=[flatten_for_review(img)])]
    res, _target = router.chat(profile, messages, tag=tag, json_mode=True)
    try:
        prompt = _parse_json(res.text)["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt 为空或不是字符串")
    except (ValueError, KeyError, TypeError) as e:
        raise UpstreamError("bad_llm_json",
                            f"生成修改指令时返回的不是合法 JSON: {e}; 原始输出: {res.text[:200]!r}") from e
    prompt = prompt.strip()
    if style_refs > 0:
        prompt = f"{prompt}{FIX_STYLE_REFS_NOTE}"
    negative = ""
    if style:
        prompt = _merge(prompt, style.positive)
        negative = style.negative.strip()
    return ExpandedPrompt(prompt=prompt, negative=negative)
