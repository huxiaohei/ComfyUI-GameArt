"""按配置里的单价估算一次运行要花多少钱（--max-cost 用），提交之前算，不联网。

只是估算：出图按"这个模型组里最贵的那个模型 × 张数"算（失败换下一个时最多只有一个成功计费）；
文字和看图调用按固定的 token 数估（EST_INPUT_TOKENS / EST_OUTPUT_TOKENS）。没写单价的模型记为 0。
"""
from __future__ import annotations

import json
from typing import Any

from .config import Pricing, Settings, Target

EST_INPUT_TOKENS = 3000  # 一次文字/看图调用大约的输入 token（含一张图）
EST_OUTPUT_TOKENS = 1000
LAZY_QC_INPUTS = ("image", "mask", "meta")  # 「质检结果」在修图/补质检时不向上游要的输入


def _is_link(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)


def _targets(settings: Settings, profile: str) -> list[Target]:
    if profile in settings.profiles:
        return settings.profiles[profile]
    if ":" in profile:
        try:
            return [Target.model_validate(profile)]
        except ValueError:
            return []
    return []


def _price(settings: Settings, profile: str) -> list[Pricing]:
    return [settings.pricing[f"{t.provider}:{t.model}"] for t in _targets(settings, str(profile))
            if f"{t.provider}:{t.model}" in settings.pricing]


def image_cost(settings: Settings, profile: str, n: int = 1) -> float:
    return max((p.per_image for p in _price(settings, profile)), default=0.0) * n


def chat_cost(settings: Settings, profile: str, calls: int = 1) -> float:
    each = max((EST_INPUT_TOKENS / 1000 * p.per_1k_input_tokens + EST_OUTPUT_TOKENS / 1000 * p.per_1k_output_tokens
                for p in _price(settings, profile)), default=0.0)
    return each * calls


def _fix_job(node: dict[str, Any]) -> dict[str, Any] | None:
    raw = node["inputs"].get("fix")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _executed(wf: dict[str, Any]) -> set[str]:
    """会被执行的节点：从没有下游的节点（输出节点）往上找。「质检结果」在修图时不向上游要图，那条线上的不算。"""
    used = {v[0] for node in wf.values() for v in node["inputs"].values() if _is_link(v)}
    stack = [nid for nid in wf if nid not in used]
    seen: set[str] = set()
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in wf:
            continue
        seen.add(nid)
        node = wf[nid]
        skip = LAZY_QC_INPUTS if node["class_type"] == "GAQCReview" and _fix_job(node) else ()
        stack += [v[0] for k, v in node["inputs"].items() if _is_link(v) and k not in skip]
    return seen


def estimate_cost(wf: dict[str, Any], settings: Settings) -> float:
    """估算这个（API 格式）工作流运行一次的费用，美元。"""
    run = _executed(wf)
    images = sum(int(n["inputs"].get("n") or 1) for nid, n in wf.items()
                 if nid in run and n["class_type"] in ("GACloudGenerate", "GACloudEdit")) or 1
    total = 0.0
    for nid in run:
        node = wf[nid]
        ins, cls = node["inputs"], node["class_type"]
        n = int(ins.get("n") or 1)
        if cls == "GAPromptExpand":
            total += chat_cost(settings, ins.get("profile", "text"))
        elif cls == "GAReferenceAnalyze":
            total += chat_cost(settings, ins.get("profile", "vision"))
        elif cls == "GACloudGenerate":
            total += max(image_cost(settings, ins.get("profile", "image"), n),
                         image_cost(settings, ins.get("edit_profile", "image_edit"), n))
        elif cls == "GACloudEdit":
            total += image_cost(settings, ins.get("profile", "image_edit"), n)
        elif cls == "GAQCScore":
            total += chat_cost(settings, ins.get("profile", "vision"), images)
        elif cls == "GAQCReview":
            total += _qc_review_cost(settings, ins, _fix_job(node), images)
    return round(total, 4)


def _qc_review_cost(settings: Settings, ins: dict[str, Any], job: dict[str, Any] | None, images: int) -> float:
    vision = ins.get("profile", "vision")
    if job is None:  # 正常质检：每张图一次；多张时再归并一次共性问题
        if ins.get("auto_qc") is False:
            return 0.0
        return chat_cost(settings, vision, images) + (chat_cost(settings, "text") if images > 1 else 0.0)
    if job.get("resize") is True:
        return 0.0
    if job.get("qc") is True:
        return chat_cost(settings, vision)
    edit = str(ins.get("edit_model") or "").strip() or ins.get("edit_profile", "image_edit")
    qc = chat_cost(settings, vision) if ins.get("auto_qc", True) is not False else 0.0
    return chat_cost(settings, vision) + image_cost(settings, edit) + qc
