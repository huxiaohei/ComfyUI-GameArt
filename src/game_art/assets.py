"""已保存的资产：PNG 和旁边同名的 .json 记录。命令行（ga fix / regen / qc / final）和「保存资产」节点共用。"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import InputError

FINAL_BACKUP = "_final_backup"
_NUMBERED = re.compile(r"(.+)_\d{5}")
MAX_FIX_ISSUES = 3  # 和编辑器（web/qc_review.js 的 MAX_FIX_ISSUES）一致：一次最多修 3 条细节问题


def record_path(png: Path) -> Path:
    return png.with_suffix(".json")


def checked_asset(path: Path) -> Path:
    """必须是 .png，旁边要有同名 .json 记录。返回完整路径。"""
    p = Path(path).expanduser().resolve()
    if p.suffix.lower() != ".png" or not p.is_file():
        raise InputError("bad_asset", f"找不到这张图（要写 .png 文件的路径）：{p}")
    if not record_path(p).is_file():
        raise InputError("bad_asset", f"这张图旁边没有同名的 .json 记录：{record_path(p)}")
    return p


def read_record(png: Path) -> dict[str, Any]:
    side = record_path(png)
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InputError("bad_record", f"读不了记录文件 {side}：{e}") from e
    if not isinstance(data, dict):
        raise InputError("bad_record", f"记录文件的内容不是一个对象：{side}")
    return data


def write_record(png: Path, record: dict[str, Any]) -> None:
    record_path(png).write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def base_name(png: Path) -> str:
    """候选图的名字去掉编号：archer_lv1_00002.png → archer_lv1；没有编号时就是文件名本身。"""
    m = _NUMBERED.fullmatch(png.stem)
    return m.group(1) if m else png.stem


def copy_with_record(source: Path, side: Path, dest: Path) -> None:
    """把图片和记录写到 dest（.png）和它旁边的 .json，记录里的文件名跟着改。"""
    try:
        data = source.read_bytes()
        record = json.loads(side.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            record.update(file=dest.name, copied_from=str(source))
        dest.write_bytes(data)
        record_path(dest).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError) as e:
        raise InputError("copy_failed", f"另存失败：{e}") from e


def qc_of(record: dict[str, Any]) -> dict[str, Any] | None:
    """记录里这张图的质检结果。保存节点给每张图只留它自己那一条；旧记录可能是只有一条的列表。"""
    qc = record.get("qc")
    if isinstance(qc, list) and len(qc) == 1:
        qc = qc[0]
    return qc if isinstance(qc, dict) else None


def issues_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    """质检问题，每条带序号 n（从 1 开始，就是 qc.details 的顺序）、text、level、kind。"""
    qc = qc_of(record) or {}
    details = qc.get("details")
    if not isinstance(details, list):  # 很旧的记录只有文字
        details = [{"text": t} for t in qc.get("issues") or [] if isinstance(t, str)]
    out: list[dict[str, Any]] = []
    for n, d in enumerate(details, 1):
        if isinstance(d, dict) and isinstance(d.get("text"), str):
            out.append({"n": n, "text": d["text"], "level": "suggest" if d.get("level") == "suggest" else "must",
                        "kind": "layout" if d.get("kind") == "layout" else "detail"})
    return out


def pick_issues(record: dict[str, Any], numbers: list[int], *, details_only: bool) -> list[dict[str, Any]]:
    """按序号取质检问题。details_only：只许选细节类，最多 MAX_FIX_ISSUES 条（「修这张」的规矩）。"""
    if not numbers:
        return []
    issues = issues_of(record)
    if not issues:
        raise InputError("no_qc", "这张图的记录里没有质检问题：先用 ga qc 质检一次，或者只用 --note 写要改什么")
    by_n = {i["n"]: i for i in issues}
    picked: list[dict[str, Any]] = []
    for n in dict.fromkeys(numbers):
        if n not in by_n:
            raise InputError("bad_issue", f"没有第 {n} 条问题，这张图一共 {len(issues)} 条",
                             {"issues": issues})
        picked.append(by_n[n])
    if details_only:
        layout = [i["n"] for i in picked if i["kind"] == "layout"]
        if layout:
            raise InputError("layout_issue", (
                f"第 {'、'.join(map(str, layout))} 条是构图类问题，在原图上改不好，请用 ga regen 整张重出"),
                {"issues": layout})
        if len(picked) > MAX_FIX_ISSUES:
            raise InputError("too_many_issues", (
                f"一次最多修 {MAX_FIX_ISSUES} 条细节问题：改得越多越像整张重画，满意的部分越保不住。"
                "请先修几条，再对新图修下一轮"))
    return picked


def finalize(source: Path, name: str, *, force: bool) -> tuple[Path, Path | None]:
    """定稿：把候选图和记录拷成同目录下的 <name>.png / <name>.json，候选图不动。
    目标已存在时不覆盖；force 时先把旧定稿挪到 _final_backup/<name>_<时间>.png（连同记录）。
    返回（定稿路径，旧定稿的备份路径或 None）。"""
    source = checked_asset(source)
    name = name.strip().removesuffix(".png")
    if not name or "/" in name or "\\" in name or ".." in name:
        raise InputError("bad_name", f"定稿名不合法：{name!r}（不能为空，不能含路径分隔符或 ..）")
    dest = source.parent / f"{name}.png"
    if dest == source:
        raise InputError("bad_asset", f"这张图已经是定稿了：{dest}")
    exists = [p for p in (dest, record_path(dest)) if p.exists()]
    backup: Path | None = None
    if exists:
        if not force:
            raise InputError("final_exists", f"定稿已存在：{dest}。确认要替换就加 --force（旧定稿会先备份）",
                             {"path": str(dest)})
        keep = source.parent / FINAL_BACKUP
        keep.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = keep / f"{name}_{stamp}.png"
        for p in exists:
            shutil.move(str(p), str(keep / f"{name}_{stamp}{p.suffix}"))
    copy_with_record(source, record_path(source), dest)
    return dest, backup
