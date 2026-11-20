"""质检结果：放在「保存资产」之前，给这一批的每张图打分、列出问题，图片原样往下传。

编辑器里（web/qc_review.js）节点上会显示每张图的缩略图、得分和问题，每条问题一个开关；
点「采纳并重新生成」时，勾选的问题被写进上游「提示词扩写」的 feedback，再运行一次。
问题不通过连线回传（ComfyUI 的图不能成环），由页面脚本直接改上游节点的输入框。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import torch
from PIL import Image

from game_art.errors import GAError, InputError
from game_art.steps import DEFAULT_QC_THRESHOLD, CommonIssue, QCResult, common_issues, qc_score
from game_art.style import StyleCard

from .. import common
from ..common import CATEGORY, META_IN, META_OUT, TAG_IN, always_changed, ga_errors, model_input, resolve_tag
from ..tensors import tensors_to_pils

THUMB_SUBFOLDER = "game_art_qc"
THUMB_SIZE = 256


def _save_thumbs(pils: list[Image.Image]) -> list[dict[str, str]]:
    """缩略图放进 ComfyUI 的临时目录，页面用 /view 读取。"""
    import folder_paths  # ComfyUI 之外不存在，惰性导入

    folder = Path(folder_paths.get_temp_directory()) / THUMB_SUBFOLDER
    folder.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, str]] = []
    for im in pils:
        thumb = im.copy()
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        name = f"{uuid.uuid4().hex[:12]}.png"
        thumb.save(folder / name, format="PNG")
        out.append({"filename": name, "subfolder": THUMB_SUBFOLDER, "type": "temp"})
    return out


def _report(results: list[QCResult], shared: list[CommonIssue], threshold: float, note: str) -> str:
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        verdict = "通过" if r.passed else "未通过"
        lines.append(f"第 {i} 张：{r.score:.1f}/10（{verdict}，及格线 {threshold:g}）")
        lines += [f"  - [{'必须改' if d.level == 'must' else '建议'}] {d.text}" for d in r.details]
    if shared:
        lines.append("共性问题：")
        lines += [f"  - {c.text}（{c.count}/{len(results)} 张）" for c in shared]
    if note:
        lines.append(note)
    return "\n".join(lines)


class GAQCReview:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK", "GA_META", "STRING")
    RETURN_NAMES = ("image", "mask", "meta", "report")
    DESCRIPTION = ("质检结果：放在「保存资产」前面。让能看图的模型给每张图打分（0 到 10）并列出问题，图片原样往下传。"
                   "节点上每条问题有一个开关，勾选后点「采纳并重新生成」，这些问题会作为修订意见带回「提示词扩写」再出一版。"
                   "每张图调用一次看图模型（费用很低）。")
    OUTPUT_TOOLTIPS = ("和输入相同的图片，原样往下传。", "和输入相同的透明度蒙版；没接 mask 时是全不透明。",
                       META_OUT + "这里额外写入每张图的质检结果（qc）。", "文字报告：每张图的得分和问题。")

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要质检的图片，一般接「云端生图」的 image。一次给多张时每张都打分。"}),
                "threshold": ("FLOAT", {"default": DEFAULT_QC_THRESHOLD, "min": 0.0, "max": 10.0, "step": 0.5,
                                        "tooltip": (
                    "及格线，0 到 10 分。得分不低于它算通过。这里只影响显示（通过/未通过），"
                    "不会拦住图片，不管得分多少图片都会往下传并保存。")}),
                "profile": model_input("vision", "看图质检"),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": "图片的透明度蒙版，接上后模型能看出哪里是透明的，并原样往下传。"}),
                "meta": META_IN,
                "style": ("GA_STYLE", {"tooltip": "接「风格卡」，让模型同时检查是否符合风格。可不接。"}),
                "brief": ("STRING", {"default": "", "multiline": True,
                                     "placeholder": "这张图原本想要什么（留空 = 用 meta 里记录的描述）", "tooltip": (
                    "这张图原本想要什么，模型据此判断画得对不对。接了 meta 时可以留空，"
                    "会自动用「提示词扩写」里写的那段描述。")}),
                "tag": TAG_IN,
            },
        }

    @ga_errors
    def run(self, image: torch.Tensor, threshold: float = DEFAULT_QC_THRESHOLD, profile: str = "vision",
            mask: torch.Tensor | None = None, meta: dict[str, Any] | None = None,
            style: StyleCard | None = None, brief: str = "", tag: str = "") -> dict[str, Any]:
        want = brief.strip() or str((meta or {}).get("brief") or "").strip()
        if not want:
            raise InputError("no_brief", (
                "质检需要知道这张图原本想要什么：请把上游的 meta 接到本节点，或者在 brief 里写上描述"))
        tag = resolve_tag(tag, meta)
        router = common.get_router()
        pils = tensors_to_pils(image, mask)
        results = [qc_score(router, im, brief=want, style=style, threshold=threshold, tag=tag, profile=profile)
                   for im in pils]
        shared: list[CommonIssue] = []
        note = ""
        if len(results) > 1:
            try:
                shared = common_issues(router, [r.issues for r in results], tag=tag)
            except GAError as e:  # 归并只是锦上添花：失败了每张图自己的问题照样可用，在报告里说明
                note = f"（没能归并出共性问题：{e.message}）"
        out_meta = dict(meta or {})
        out_meta["qc"] = [r.model_dump() for r in results]
        out_mask = mask if mask is not None else image.new_zeros(image.shape[:3])
        thumbs = _save_thumbs(pils)
        ui = {"threshold": threshold, "note": note,
              "results": [{"score": r.score, "passed": r.passed, "thumb": t,
                           "issues": [d.model_dump() for d in r.details]}
                          for r, t in zip(results, thumbs, strict=True)],
              "common": [c.model_dump() for c in shared]}
        return {"ui": {"ga_qc": [ui]},
                "result": (image, out_mask, out_meta, _report(results, shared, threshold, note))}
