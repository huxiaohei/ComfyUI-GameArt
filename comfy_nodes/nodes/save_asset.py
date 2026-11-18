"""保存资产：PNG + 同名 .json 侧车文件，存到 ComfyUI 输出目录。"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch

from game_art.errors import InputError

from ..common import CATEGORY, META_IN, ga_errors
from ..tensors import tensors_to_pils

_BAD_DRIVE = re.compile(r"^[A-Za-z]:")


def _safe_subfolder(sub: str) -> str:
    sub = (sub or "").strip().replace("\\", "/")
    if sub.startswith("/") or _BAD_DRIVE.match(sub) or ".." in sub.split("/"):
        raise InputError("bad_subfolder", f"子目录不合法：{sub!r}（不能是绝对路径，也不能含 ..）")
    return "/".join(p for p in sub.split("/") if p not in ("", "."))


def _safe_name(name: str) -> str:
    name = (name or "").strip() or "asset"
    if "/" in name or "\\" in name or ".." in name:
        raise InputError("bad_name", f"名称不合法：{name!r}（不能含路径分隔符或 ..）")
    return name


class GASaveAsset:
    CATEGORY = CATEGORY
    FUNCTION = "save"
    RETURN_TYPES = ()
    RETURN_NAMES = ()
    OUTPUT_NODE = True
    DESCRIPTION = ("保存资产：把图片存成 PNG，并在旁边存一个同名的 .json，记录这张图的提示词、模型、种子和估算费用。")

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要保存的图片。"}),
                "subfolder": ("STRING", {"default": "game_art", "tooltip": (
                    "存到 ComfyUI 输出目录下的哪个子文件夹，例如 ga/text_to_asset。文件夹不存在会自动创建。")}),
                "name": ("STRING", {"default": "asset", "tooltip": (
                    "文件名前缀。实际文件名会自动加编号，如 asset_00001.png，不会覆盖已有文件。")}),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": "透明度蒙版。接上才会存成透明背景；不接则存成不透明图片。"}),
                "meta": META_IN,
            },
        }

    @ga_errors
    def save(self, image: torch.Tensor, subfolder: str = "", name: str = "asset",
             mask: torch.Tensor | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        import folder_paths  # ComfyUI 之外不存在，惰性导入

        sub, name = _safe_subfolder(subfolder), _safe_name(name)
        root = Path(folder_paths.get_output_directory()).resolve()
        target_dir = (root / sub).resolve()
        if root != target_dir and root not in target_dir.parents:
            raise InputError("bad_subfolder", f"子目录不合法：{subfolder!r}（越出了输出目录）")
        target_dir.mkdir(parents=True, exist_ok=True)

        # 无 mask 时也写 RGBA（alpha 全不透明），保持产物格式一致
        pils = [im.convert("RGBA") for im in tensors_to_pils(image, mask)]
        saved: list[dict[str, str]] = []
        counter = 1
        for im in pils:
            while (target_dir / f"{name}_{counter:05d}.png").exists() or \
                    (target_dir / f"{name}_{counter:05d}.json").exists():
                counter += 1
            fname = f"{name}_{counter:05d}.png"
            im.save(target_dir / fname, format="PNG")
            side = dict(meta or {})
            side.update(file=fname, saved_at=datetime.now(UTC).isoformat())
            (target_dir / f"{name}_{counter:05d}.json").write_text(
                json.dumps(side, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            saved.append({"filename": fname, "subfolder": sub, "type": "output"})
            counter += 1
        return {"ui": {"images": saved}}
