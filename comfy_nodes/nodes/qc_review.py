"""质检结果：放在「保存资产」之前，给这一批的每张图打分、列出问题，图片原样往下传。

编辑器里（web/qc_review.js）节点上会显示每张图的缩略图、得分和问题，每条问题一个开关。两种改法：
- 「采纳并重新生成」：勾选的问题被写进上游「提示词扩写」的 feedback，整条流程再运行一次，整张重出。
- 「修这张」：在选中的这张图上直接修改。页面脚本把"修哪张、改什么"写进本节点的 fix，再运行一次；
  这一次本节点不向上游要图（image / mask / meta 是按需读取的输入），上游不执行、不花出图的钱，
  而是取出上次存下的那张原图，让看图模型对着它写一段完整的修改指令（保持什么、改什么，末尾拼上风格卡），
  再把原图（排第一）和标准风格图（refs，只管画风）一起交给改图模型，最后对结果质检。
- 「调整大小」：主体在画面里占几成是程序按透明度量出来的（不问模型）。点了以后同样走 fix，
  在本机把选中那张图的主体等比例缩放到目标占比并居中，不调任何模型、不花钱。

问题不通过连线回传（ComfyUI 的图不能成环），由页面脚本直接改节点上的输入框。
"""
from __future__ import annotations

import importlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, NamedTuple

import torch
from PIL import Image

from game_art import imaging
from game_art.errors import GAError, InputError, UpstreamError
from game_art.providers.base import ImageRequest
from game_art.steps import (
    DEFAULT_QC_THRESHOLD,
    CommonIssue,
    QCResult,
    common_issues,
    fix_instruction,
    qc_score,
)
from game_art.style import StyleCard

from .. import common
from ..common import (
    CATEGORY,
    META_IN,
    META_OUT,
    TAG_IN,
    always_changed,
    ga_errors,
    image_meta,
    model_input,
    resolve_tag,
)
from ..tensors import pil_to_tensors, tensors_to_pils

# 修图的画质和保真度可以直接在节点上选；选"跟随配置"就不发这个参数，用配置文件里给模型写的
FIX_LEVELS = ("high", "medium", "low")
FOLLOW_CONFIG = "跟随配置"
STORE_SUBFOLDER = "game_art_qc"
THUMB_SIZE = 256
LAZY_INPUTS = ("image", "mask", "meta")
_STORE_NAME = re.compile(r"[0-9a-f]{12}\.png")


def _store_dir() -> Path:
    import folder_paths  # ComfyUI 之外不存在，惰性导入

    return Path(folder_paths.get_temp_directory()) / STORE_SUBFOLDER


def _store(pils: list[Image.Image], meta: dict[str, Any]) -> list[dict[str, Any]]:
    """每张图在 ComfyUI 的临时目录里存三样东西：原图（之后「修这张」拿它当底图）、同名 .json（这张图的记录）、
    缩略图（页面用 /view 显示）。返回页面要用的文件信息。"""
    folder = _store_dir()
    folder.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for im in pils:
        key = uuid.uuid4().hex[:12]
        im.save(folder / f"{key}.png", format="PNG")
        (folder / f"{key}.json").write_text(json.dumps(meta, ensure_ascii=False, default=str), encoding="utf-8")
        thumb = im.copy()
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        thumb.save(folder / f"{key}_t.png", format="PNG")
        # path：这张图（质检时的原样，还没经过去背景和清理）在硬盘上的位置；下游没有保存节点时「另存这张」用它
        out.append({"full": f"{key}.png", "path": str(folder / f"{key}.png"),
                    "thumb": {"filename": f"{key}_t.png", "subfolder": STORE_SUBFOLDER, "type": "temp"}})
    return out


def _load_stored(name: str) -> tuple[Image.Image, dict[str, Any]]:
    if not _STORE_NAME.fullmatch(name):
        raise InputError("bad_fix", f"要修改的图片名字不对：{name!r}")
    file = _store_dir() / name
    try:
        with Image.open(file) as im:
            base = im.convert("RGBA")
        meta = json.loads(file.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InputError("fix_base_missing", (
            "找不到要修改的那张原图（ComfyUI 重启后临时文件会被清掉）。"
            "请先正常运行一次重新出图，再点「修这张」。")) from e
    return base, meta if isinstance(meta, dict) else {}


class FixJob(NamedTuple):
    file: str  # 要处理的那张图在临时目录里的名字
    notes: list[str]  # 修改意见
    only_qc: bool  # 只补一次质检
    resize: bool = False  # 只在本机调整主体大小


def parse_fix(fix: str) -> FixJob | None:
    """fix 是页面脚本写进来的 JSON，空 = 正常运行。三种写法：
    {"file": 要修的那张图, "notes": [修改意见]} = 在这张图上修改；
    {"file": 那张图, "qc": true} = 只给这张图补一次质检（平时关着自动质检时用）；
    {"file": 那张图, "resize": true} = 在本机把这张图的主体缩放到目标占比，不调模型。"""
    if not (fix or "").strip():
        return None
    try:
        obj = json.loads(fix)
        file, only_qc, resize = obj["file"], obj.get("qc") is True, obj.get("resize") is True
        notes = obj.get("notes", []) if only_qc or resize else obj["notes"]
        if not isinstance(file, str) or not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
            raise ValueError("字段类型不对")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise InputError("bad_fix", f"fix 的内容不对（它由节点上的按钮自动填写，平时应该留空）：{e}") from e
    return FixJob(file, [n.strip() for n in notes if n.strip()], only_qc, resize)


def _blocked() -> Any:
    """只补质检的那次运行不该再往下游送图（否则「保存资产」会把同一张图再存一遍）：
    用 ComfyUI 的 ExecutionBlocker 让下游这一次什么都不做。不在 ComfyUI 里（如单元测试）时返回 None。"""
    try:
        graph: Any = importlib.import_module("comfy_execution.graph")
    except ImportError:
        return None
    return graph.ExecutionBlocker(None)


def _report(results: list[QCResult | None], shared: list[CommonIssue], threshold: float, note: str) -> str:
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        if r is None:
            lines.append(f"第 {i} 张：未质检")
            continue
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
                   "节点上每条问题有一个开关。勾选后有两种改法：「采纳并重新生成」把问题带回「提示词扩写」整张重出；"
                   "「修这张」在选中的图上直接修改，其余部分基本保留。每张图调用一次看图模型（费用很低），"
                   "修图另外按张计费。")
    OUTPUT_TOOLTIPS = ("图片，原样往下传；「修这张」时是修改后的那一张。", "透明度蒙版；没接 mask 时是全不透明。",
                       META_OUT + "这里额外写入每张图的质检结果（qc）。", "文字报告：每张图的得分和问题。")

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"lazy": True, "tooltip": (
                    "要质检的图片，一般接「云端生图」的 image。一次给多张时每张都打分。")}),
                "threshold": ("FLOAT", {"default": DEFAULT_QC_THRESHOLD, "min": 0.0, "max": 10.0, "step": 0.5,
                                        "tooltip": (
                    "及格线，0 到 10 分。得分不低于它算通过。这里只影响显示（通过/未通过），"
                    "不会拦住图片，不管得分多少图片都会往下传并保存。")}),
                "profile": model_input("vision", "看图质检"),
            },
            "optional": {
                "mask": ("MASK", {"lazy": True, "tooltip": (
                    "图片的透明度蒙版，接上后模型能看出哪里是透明的，并原样往下传。")}),
                "meta": ("GA_META", {"lazy": True, "tooltip": META_IN[1]["tooltip"]}),
                "style": ("GA_STYLE", {"tooltip": "接「风格卡」，让模型同时检查是否符合风格。可不接。"}),
                "brief": ("STRING", {"default": "", "multiline": True,
                                     "placeholder": "这张图原本想要什么（留空 = 用 meta 里记录的描述）", "tooltip": (
                    "这张图原本想要什么，模型据此判断画得对不对。接了 meta 时可以留空，"
                    "会自动用「提示词扩写」里写的那段描述。")}),
                "edit_profile": model_input("image_edit", "在原图上修改（「修这张」）"),
                "fix": ("STRING", {"default": "", "tooltip": (
                    "由「修这张」按钮自动填写，平时留空。里面记着要修哪一张图、按哪些意见改。")}),
                "tag": TAG_IN,
                "refs": ("GA_REFS", {"tooltip": (
                    "接「参考图」节点的 refs，可不接。只在「修这张」时用："
                    "作为画风的对照图和要修改的原图一起发给改图模型，"
                    "防止连修几轮以后画风越改越偏。正常质检时不使用。")}),
                "fix_with_refs": ("BOOLEAN", {"default": True, "tooltip": (
                    "「修这张」时要不要带上 refs 里的参考图作画风对照。默认打开。"
                    "参考图和要修的图内容差别很大、修出来反而被参考图带偏时关掉。")}),
                "fix_quality": ([*FIX_LEVELS, FOLLOW_CONFIG], {"default": "high", "tooltip": (
                    "「修这张」的画质档。high 最清晰、细节最好，也最贵；medium、low 依次便宜。"
                    "选「跟随配置」则用配置文件里给这个模型写的画质。"
                    "Gemini 图片模型不分画质档，这一项对它们不起作用。")}),
                "fix_fidelity": (["high", "low", FOLLOW_CONFIG], {"default": "high", "tooltip": (
                    "「修这张」时对原图的保真度。high：模型更努力地保持原图的画风、脸和细节，只动要改的地方，费用更高；"
                    "low：改动更自由，原图保留得少。修图一般用 high。\n"
                    "不是每个模型都支持这个参数：模型不认时会自动去掉它再试一次，并在记录里注明。")}),
                "edit_model": ("STRING", {"default": "", "tooltip": (
                    "手动指定「修这张」用的模型，一般留空。写法是 服务商:模型名，例如 aihubmix:gpt-image-2。"
                    "填了就用它，上面 edit_profile 选的不再生效；留空则用 edit_profile。\n"
                    "用来试下拉里没有的模型：有些服务商的模型列表不全，实际能用的模型不一定列出来。"
                    "名字写错或服务商没有这个模型时，运行会报错并带上服务商返回的原因。")}),
                "auto_qc": ("BOOLEAN", {"default": True, "tooltip": (
                    "打开（默认）：每张图出来都自动质检一次。\n"
                    "关闭：不自动质检，节省看图模型的费用，适合一次出好几张先挑一挑的阶段。"
                    "图片照常显示和保存；想看哪一张的问题，选中它再点「质检这张」，只给这一张质检。"
                    "没质检的图也可以直接「修这张」，把要改的地方写在「我的补充」里即可。")}),
                "subject_ratio": ("FLOAT", {"default": imaging.DEFAULT_SUBJECT_RATIO, "min": 0.3, "max": 0.95,
                                            "step": 0.05, "tooltip": (
                    "主体应该占画面高度的几成，默认 0.7。节点上会显示每张图实测的占比（按透明度量，不问模型）；"
                    "点「调整大小」时，在本机把选中那张图的主体等比例缩放到这个占比并居中，不花钱。"
                    "只对带透明底的图有效。放大超过三成左右线条会发虚，那种情况重新生成更好。")}),
            },
        }

    def check_lazy_status(self, fix: str = "", **kw: Any) -> list[str]:
        """正常质检时向上游要图；「修这张」时不要，上游的扩写和出图就不会执行。
        kw 里只有连了线的输入，还没算出来的值是 None。"""
        if (fix or "").strip():
            return []
        return [name for name in LAZY_INPUTS if name in kw and kw[name] is None]

    @ga_errors
    def run(self, image: torch.Tensor | None = None, threshold: float = DEFAULT_QC_THRESHOLD,
            profile: str = "vision", mask: torch.Tensor | None = None, meta: dict[str, Any] | None = None,
            style: StyleCard | None = None, brief: str = "", edit_profile: str = "image_edit", fix: str = "",
            tag: str = "", refs: list[Image.Image] | None = None, fix_with_refs: bool = True,
            fix_quality: str = "high", fix_fidelity: str = "high", edit_model: str = "",
            auto_qc: bool = True, subject_ratio: float = imaging.DEFAULT_SUBJECT_RATIO) -> dict[str, Any]:
        job = parse_fix(fix)
        router = common.get_router()
        fixed_from = ""
        used_model = ""
        if job is not None and job.resize:
            # 只在本机调整主体大小：不出图、不调模型。新的一张照常往下游送（去背景、保存）
            base, stored = _load_stored(job.file)
            before = imaging.subject_ratio(base)
            fitted = imaging.fit_subject(base, subject_ratio)
            after = imaging.subject_ratio(fitted)
            out_image, out_mask = pil_to_tensors([fitted])
            out_meta = dict(stored)
            out_meta.pop("qc", None)
            out_meta["resize"] = {"from": round(before or 0, 3), "to": round(after or 0, 3)}
            files = _store([fitted], out_meta)
            note = f"主体占画面高度从 {before:.0%} 调到了 {after:.0%}（本机处理，没有调用模型）"
            ui = {"threshold": threshold, "note": "", "mode": "fix", "base": job.file, "model": "", "common": [],
                  "resized": True, "target": subject_ratio,
                  "results": [{"score": None, "passed": None, **files[0], "subject": after, "issues": []}]}
            return {"ui": {"ga_qc": [ui]}, "result": (out_image, out_mask, out_meta, note)}
        if job is not None and job.only_qc:
            # 只给已经出好的某一张图补一次质检：不出图、不改图，也不往下游送东西
            base, stored = _load_stored(job[0])
            want = brief.strip() or str(stored.get("brief") or "").strip()
            if not want:
                raise InputError("no_brief", "质检需要知道这张图原本想要什么：请在 brief 里写上描述")
            r = qc_score(router, base, brief=want, style=style, threshold=threshold,
                         tag=resolve_tag(tag, stored), profile=profile)
            ui = {"threshold": threshold, "note": "", "mode": "qc", "base": job[0], "model": "", "common": [],
                  "results": [{"score": r.score, "passed": r.passed,
                               "issues": [d.model_dump() for d in r.details]}]}
            stop = _blocked()
            return {"ui": {"ga_qc": [ui]}, "result": (stop, stop, stop, _report([r], [], threshold, ""))}
        if job is None:
            if image is None:
                raise InputError("no_image", "质检结果节点没有收到图片：请把上游的 image 接到本节点")
            out_meta = dict(meta or {})
            want = brief.strip() or str(out_meta.get("brief") or "").strip()
            if not want:
                raise InputError("no_brief", (
                    "质检需要知道这张图原本想要什么：请把上游的 meta 接到本节点，或者在 brief 里写上描述"))
            tag = resolve_tag(tag, meta)
            pils = tensors_to_pils(image, mask)
            out_image = image
            out_mask = mask if mask is not None else image.new_zeros(image.shape[:3])
        else:
            fixed_from, notes = job.file, job.notes
            manual = edit_model.strip()
            if manual and ":" not in manual:
                raise InputError("bad_edit_model", (
                    f"edit_model 的写法不对：{manual!r}。要写成 服务商:模型名，例如 aihubmix:{manual}"))
            edit_profile = manual or edit_profile
            base, stored = _load_stored(fixed_from)
            want = brief.strip() or str(stored.get("brief") or "").strip()
            tag = resolve_tag(tag, stored)
            # 要修改的原图排第一（模型对第一张图还原得最用心），标准风格图跟在后面只管画风
            anchors = list(refs or []) if fix_with_refs else []
            plan = fix_instruction(router, base, notes, brief=want, style=style, tag=tag,
                                   style_refs=len(anchors), profile=profile)
            size = f"{base.width}x{base.height}"
            transparent = base.getchannel("A").getextrema() != (255, 255)  # 原图带透明底：修改后也要透明底
            params: dict[str, Any] = {}
            if fix_quality in FIX_LEVELS:
                params["quality"] = fix_quality
            if fix_fidelity in ("high", "low"):
                params["input_fidelity"] = fix_fidelity
            req = ImageRequest(prompt=plan.prompt, negative=plan.negative, size=size, n=1,
                               transparent=transparent, refs=[base, *anchors], params=params)
            fidelity_dropped = False
            try:
                result, target = router.edit(edit_profile, req, tag=tag)
            except UpstreamError:
                if "input_fidelity" not in params:
                    raise
                # 有的模型不认保真度这个参数：去掉它再试一次；还是失败就照实报第二次的错
                req = req.model_copy(update={"params": {k: v for k, v in params.items() if k != "input_fidelity"}})
                result, target = router.edit(edit_profile, req, tag=tag)
                fidelity_dropped = True
            pils = common.unify(result.images, base.size)
            out_image, out_mask = pil_to_tensors(pils)
            out_meta = image_meta(router, stored, result=result, target=target, prompt=plan.prompt,
                                  negative=plan.negative, size=size, tag=tag, refs=len(req.refs))
            # 这张图是在上一版的基础上按这些意见修改出来的；style_refs = 附带了几张画风对照图
            out_meta["fix"] = {"notes": notes, "style_refs": len(anchors), **req.params}
            if fidelity_dropped:
                out_meta["fix"]["input_fidelity"] = "模型不支持，已去掉"
            used_model = f"{target.provider}:{target.model}"
            if not want:
                want = plan.prompt

        out_meta.pop("qc", None)
        results: list[QCResult | None] = [
            qc_score(router, im, brief=want, style=style, threshold=threshold, tag=tag, profile=profile)
            if auto_qc else None for im in pils]
        checked = [r for r in results if r is not None]
        shared: list[CommonIssue] = []
        note = ""
        if job is None and len(checked) > 1:
            try:
                shared = common_issues(router, [r.issues for r in checked], tag=tag)
            except GAError as e:  # 归并只是锦上添花：失败了每张图自己的问题照样可用，在报告里说明
                note = f"（没能归并出共性问题：{e.message}）"
        files = _store(pils, out_meta)
        if len(checked) == len(results):
            out_meta["qc"] = [r.model_dump() for r in checked]
        ui = {"threshold": threshold, "note": note, "mode": "fix" if job else "review", "base": fixed_from,
              "model": used_model, "target": subject_ratio,
              # 没质检的图：score 是 null，界面上显示"未质检"
              # subject：主体占画面高度的几成，程序量的；不带透明底的图量不出来，是 null
              "results": [{"score": r.score if r else None, "passed": r.passed if r else None, **f,
                           "subject": imaging.subject_ratio(im),
                           "issues": [d.model_dump() for d in r.details] if r else []}
                          for r, f, im in zip(results, files, pils, strict=True)],
              "common": [c.model_dump() for c in shared]}
        return {"ui": {"ga_qc": [ui]},
                "result": (out_image, out_mask, out_meta, _report(results, shared, threshold, note))}
