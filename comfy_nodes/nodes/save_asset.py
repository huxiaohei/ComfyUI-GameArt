"""保存资产：PNG + 同名 .json 侧车文件。

保存位置（subfolder）有两种写法：
- 相对路径：ComfyUI 输出目录下的子文件夹，如 ga/text_to_asset；
- 完整路径：电脑上的任意文件夹，如 /Users/me/project/art。这时文件直接存到那里，
  另外在 ComfyUI 的临时目录放一份只给编辑器预览用的图（ComfyUI 的预览只能显示它自己目录下的文件）。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch

from game_art.errors import InputError
from game_art.imaging import clean_faint_alpha

from ..common import CATEGORY, META_IN, ga_errors
from ..tensors import tensors_to_pils

_BAD_DRIVE = re.compile(r"^[A-Za-z]:")


PREVIEW_SUBFOLDER = "game_art_preview"


def _clean(sub: str) -> str:
    return (sub or "").strip().strip("\"'").strip()


def _absolute_dir(sub: str) -> Path | None:
    """写的是完整路径（含 ~ 开头）时返回它，否则返回 None。"""
    text = _clean(sub)
    if not text:
        return None
    p = Path(text).expanduser()
    if not p.is_absolute():
        return None
    if p.exists() and not p.is_dir():
        raise InputError("bad_subfolder", f"保存位置不是一个文件夹：{p}")
    return p


def _safe_subfolder(sub: str) -> str:
    """相对路径：只能落在输出目录里面。"""
    sub = _clean(sub).replace("\\", "/")
    if sub.startswith("/") or _BAD_DRIVE.match(sub) or ".." in sub.split("/"):
        raise InputError("bad_subfolder", (
            f"保存位置不合法：{sub!r}（相对路径不能含 ..；要存到输出目录以外，请写完整路径，"
            "或者点一下 subfolder 来选文件夹）"))
    return "/".join(p for p in sub.split("/") if p not in ("", "."))


def check_location(subfolder: str, name: str) -> None:
    """不碰文件系统的写法检查，供运行前校验用。"""
    if _absolute_dir(subfolder) is None:
        _safe_subfolder(subfolder)
    _safe_name(name)


def _safe_name(name: str) -> str:
    name = (name or "").strip() or "asset"
    if "/" in name or "\\" in name or ".." in name:
        raise InputError("bad_name", f"名称不合法：{name!r}（不能含路径分隔符或 ..）")
    return name


def _checked_source(src: str) -> tuple[Path, Path]:
    """只认本节点保存出来的图：必须是 .png，旁边要有同名 .json。返回（图片，记录）。"""
    source = Path(src)
    side = source.with_suffix(".json")
    if source.suffix.lower() != ".png" or not source.is_file() or not side.is_file():
        raise InputError("bad_asset", (
            f"找不到要另存的图片或它的记录文件：{source}（图片可能已被移走或删除，请重新运行一次再另存）"))
    return source, side


def _stem(name: str) -> str:
    name = _safe_name(name)
    return (name[:-4] or "asset") if name.lower().endswith(".png") else name


def _copy_to(source: Path, side: Path, dest: Path) -> None:
    """把图片和记录写到 dest（.png）和它旁边的 .json，记录里的文件名跟着改。"""
    try:
        data = source.read_bytes()
        record = json.loads(side.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            record.update(file=dest.name, copied_from=str(source))
        dest.write_bytes(data)
        dest.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError) as e:
        raise InputError("copy_failed", f"另存失败：{e}") from e


def copy_asset(src: str, dest_dir: str, name: str) -> Path:
    """把一张已保存的资产另存到 dest_dir，文件名是 <name>.png，同名的 .json 记录一起带过去。

    只认本节点保存出来的图：必须是 .png，旁边要有同名 .json。目标位置已有同名文件时不覆盖，
    改用 <name>_2.png、<name>_3.png……返回实际写入的 PNG 路径。
    """
    source, side = _checked_source(src)
    name = _stem(name)
    target_dir = Path(dest_dir).expanduser()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise InputError("bad_subfolder", f"无法使用这个文件夹 {target_dir}：{e}") from e
    stem, n = name, 1
    while (target_dir / f"{stem}.png").exists() or (target_dir / f"{stem}.json").exists():
        n += 1
        stem = f"{name}_{n}"
    dest = target_dir / f"{stem}.png"
    _copy_to(source, side, dest)
    return dest


def save_dir(subfolder: str, output_dir: str | Path) -> Path:
    """subfolder 这一栏指的是硬盘上的哪个文件夹：完整路径就是它本身，相对路径在输出目录下面。"""
    outside = _absolute_dir(subfolder)
    if outside is not None:
        return outside.resolve()
    root = Path(output_dir).resolve()
    target = (root / _safe_subfolder(subfolder)).resolve()
    if root != target and root not in target.parents:
        raise InputError("bad_subfolder", f"保存位置不合法：{subfolder!r}（越出了输出目录）")
    return target


def finalize_asset(src: str, subfolder: str, name: str, output_dir: str | Path) -> tuple[Path, bool]:
    """定稿：把一张已保存的资产存成 subfolder 里的 <name>.png（不带编号），记录文件一起带过去。

    和 copy_asset 的区别：位置就是节点上填的 subfolder，不用再选；已有同名的定稿时直接替换，
    因为定稿的文件名要固定，游戏项目才好引用。返回（写入的 PNG 路径，是否替换了旧的）。
    """
    source, side = _checked_source(src)
    target_dir = save_dir(subfolder, output_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise InputError("bad_subfolder", f"无法使用这个文件夹 {target_dir}：{e}") from e
    dest = target_dir / f"{_stem(name)}.png"
    if dest.resolve() == source.resolve():
        raise InputError("bad_asset", f"这张图已经是定稿了：{dest}")
    replaced = dest.exists()
    _copy_to(source, side, dest)
    return dest, replaced


class GASaveAsset:
    CATEGORY = CATEGORY
    FUNCTION = "save"
    RETURN_TYPES = ()
    RETURN_NAMES = ()
    OUTPUT_NODE = True
    DESCRIPTION = ("保存资产：把图片存成 PNG，并在旁边存一个同名的 .json，记录这张图的提示词、模型、种子和估算费用。"
                   "可以存到 ComfyUI 的输出目录，也可以直接存到电脑上的任意文件夹。")

    @classmethod
    def VALIDATE_INPUTS(cls, subfolder: str = "", name: str = "asset") -> bool | str:
        """点运行时、任何节点开始执行之前检查保存位置；等图出完了才发现存不了，图和钱就都白费了。"""
        try:
            check_location(subfolder if isinstance(subfolder, str) else "", name if isinstance(name, str) else "")
        except InputError as e:
            return f"「保存资产」节点：{e.message}"
        return True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要保存的图片。"}),
                "subfolder": ("STRING", {"default": "game_art", "tooltip": (
                    "保存位置，文件夹不存在会自动创建。在编辑器里点一下这一栏，会弹出系统的文件夹选择窗口；"
                    "想手动输入时按住 Shift 再点。两种写法：\n"
                    "· 相对路径（如 ga/text_to_asset）：ComfyUI 输出目录下的子文件夹。\n"
                    "· 完整路径（如 /Users/me/project/art）：直接存到电脑上的这个文件夹，比如游戏项目的美术目录。")}),
                "name": ("STRING", {"default": "asset", "tooltip": (
                    "文件名前缀。实际文件名会自动加编号，如 asset_00001.png，不会覆盖已有文件。")}),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": "透明度蒙版。接上才会存成透明背景；不接则存成不透明图片。"}),
                "meta": META_IN,
                "clean_alpha": ("INT", {"default": 0, "min": 0, "max": 255, "tooltip": (
                    "清理半透明残留，用来去掉模型画在主体脚下的淡阴影、抠图留下的雾边。\n"
                    "· 0：不清理（默认）。\n"
                    "· 大于 0：透明度（0 全透明～255 不透明）低于这个值、又不紧贴主体的像素，会被擦成全透明；"
                    "紧贴主体的抗锯齿边缘保留。淡阴影一般用 64～128。\n"
                    "只对带透明底的图有效（要接上 mask）。数值越大擦得越多，"
                    "画面里本来就该半透明的东西（薄纱、烟雾、光晕）也会被擦掉，这类素材保持 0。")}),
            },
        }

    @ga_errors
    def save(self, image: torch.Tensor, subfolder: str = "", name: str = "asset",
             mask: torch.Tensor | None = None, meta: dict[str, Any] | None = None,
             clean_alpha: int = 0) -> dict[str, Any]:
        import folder_paths  # ComfyUI 之外不存在，惰性导入

        name = _safe_name(name)
        root = Path(folder_paths.get_output_directory()).resolve()
        outside = _absolute_dir(subfolder)
        if outside is not None and (outside.resolve() == root or root in outside.resolve().parents):
            # 完整路径其实就在输出目录里：按相对路径处理，预览直接用原文件
            subfolder, outside = outside.resolve().relative_to(root).as_posix(), None
        if outside is None:
            sub = _safe_subfolder(subfolder)
            target_dir = (root / sub).resolve()
            if root != target_dir and root not in target_dir.parents:
                raise InputError("bad_subfolder", f"保存位置不合法：{subfolder!r}（越出了输出目录）")
            preview_dir = None
        else:
            sub, target_dir = PREVIEW_SUBFOLDER, outside.resolve()
            preview_dir = Path(folder_paths.get_temp_directory()) / PREVIEW_SUBFOLDER
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if preview_dir is not None:
                preview_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise InputError("bad_subfolder", f"无法创建保存位置 {target_dir}：{e}") from e

        # 无 mask 时也写 RGBA（alpha 全不透明），保持产物格式一致
        pils = [clean_faint_alpha(im, clean_alpha) for im in tensors_to_pils(image, mask)]
        saved: list[dict[str, str]] = []
        files: list[str] = []  # 每张图在硬盘上的完整路径，和 saved 一一对应，「另存选中的图」用它找原文件
        counter = 1
        # 上游「质检结果」给的是整批的质检列表：每张图的记录里只留它自己那一条
        qc_list = (meta or {}).get("qc")
        per_image_qc = qc_list if isinstance(qc_list, list) and len(qc_list) == len(pils) else None
        for index, im in enumerate(pils):
            while (target_dir / f"{name}_{counter:05d}.png").exists() or \
                    (target_dir / f"{name}_{counter:05d}.json").exists():
                counter += 1
            fname = f"{name}_{counter:05d}.png"
            im.save(target_dir / fname, format="PNG")
            files.append(str(target_dir / fname))
            side = dict(meta or {})
            if per_image_qc is not None:
                side["qc"] = per_image_qc[index]
            if clean_alpha > 0:
                side["clean_alpha"] = clean_alpha
            side.update(file=fname, saved_at=datetime.now(UTC).isoformat())
            (target_dir / f"{name}_{counter:05d}.json").write_text(
                json.dumps(side, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            if preview_dir is None:
                saved.append({"filename": fname, "subfolder": sub, "type": "output"})
            else:
                # 文件名加随机前缀：同名图片重复生成时，编辑器不会显示成缓存里的旧图
                shown = f"{uuid.uuid4().hex[:8]}_{fname}"
                im.save(preview_dir / shown, format="PNG")
                saved.append({"filename": shown, "subfolder": sub, "type": "temp"})
            counter += 1
        return {"ui": {"images": saved, "ga_files": files}}
