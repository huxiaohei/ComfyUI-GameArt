"""参考图：一次载入多张参考图（各自保留原始尺寸），输出给「云端生图」「云端改图」「参考图分析」。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import torch
from PIL import Image

from game_art import imaging
from game_art.refs import fingerprint, load_ref_images, parse_ref_paths

from ..common import CATEGORY, ga_errors
from ..tensors import pil_to_tensors, tensors_to_pils

_PREVIEW_EMPTY = (64, 64)


def _input_dir() -> Path | None:
    try:
        import folder_paths  # ComfyUI 之外不存在，惰性导入
    except ImportError:
        return None
    return Path(folder_paths.get_input_directory())


class GAReferenceImages:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("GA_REFS", "IMAGE")
    RETURN_NAMES = ("refs", "preview")
    DESCRIPTION = ("参考图：一次载入一张或多张参考图，出图时一并发给模型，让画风、比例贴近参考图。"
                   "多放几张内容不同、画风相同的图，模型更容易只学画风而不是照抄某一张。不填路径就是不用参考图。")
    OUTPUT_TOOLTIPS = (
        "参考图列表，接到「云端生图」「云端改图」「参考图分析」的 refs。",
        "预览用：把全部参考图缩放到同一尺寸后的一批图片，想看载入了哪些图时接「预览图像」节点。可以不接。",
    )

    @classmethod
    def IS_CHANGED(cls, paths: str = "", **_kw: Any) -> str:
        return fingerprint(parse_ref_paths(paths, _input_dir()))

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {"paths": ("STRING", {"default": "", "multiline": True, "placeholder": (
                "一行一个图片路径（可留空 = 不用参考图），例如：\n"
                "/Users/me/art/refs/warrior.png\n"
                "/Users/me/art/refs/lady.png\n"
                "# 以 # 开头的行会被忽略"), "tooltip": (
                "参考图的文件路径，一行一张，可以写多张；留空表示不用参考图。\n"
                "· 写电脑上的完整路径；以 @repo/ 开头表示本项目目录下的文件；"
                "只写文件名表示 ComfyUI 的 input 目录里的文件。\n"
                "· 空行和以 # 开头的行会被忽略，临时不想用某张图时在行首加 # 即可。\n"
                "· 参考图里最显眼的东西（人物、兵器、姿势）容易被模型照搬。只想统一画风时，"
                "放几张内容不同、画风相同的图，并在描述里写明哪些地方按文字重新设计。")})},
            "optional": {"images": ("IMAGE", {"tooltip": (
                "可选。接「加载图像」等节点输出的图片，会排在 paths 里的图后面一起作为参考图。"
                "想用编辑器的选图按钮挑文件、或者把上一步生成的图直接当参考图时用它。")})},
        }

    @ga_errors
    def run(self, paths: str = "", images: torch.Tensor | None = None) -> tuple[list[Image.Image], torch.Tensor]:
        refs = load_ref_images(parse_ref_paths(paths, _input_dir()))
        if images is not None:
            refs += [im.convert("RGBA") for im in tensors_to_pils(images)]
        if not refs:
            return (refs, torch.zeros((1, _PREVIEW_EMPTY[1], _PREVIEW_EMPTY[0], 3)))
        size = refs[0].size
        preview, _mask = pil_to_tensors([imaging.flatten_for_review(imaging.fit_canvas(im, size)) for im in refs])
        return (refs, preview)
