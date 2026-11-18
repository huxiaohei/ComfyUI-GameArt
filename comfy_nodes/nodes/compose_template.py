"""模板合成：按版式 yaml 把底框、图案、文字拼成成品。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import torch
from PIL import Image

from game_art.compose import compose, load_layout
from game_art.errors import InputError
from game_art.paths import resolve_repo_path

from ..common import CATEGORY, ga_errors
from ..tensors import pil_to_tensors, tensors_to_pils


class GAComposeTemplate:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    DESCRIPTION = ("模板合成：按版式文件把底框、图案、文字拼成一张成品（如一张麻将牌）。"
                   "文字由程序用字体画上去，不经过 AI，所以不会写错字。在本机完成，不花钱。")
    OUTPUT_TOOLTIPS = ("合成好的成品图。", "成品的透明度蒙版，和 image 一起接到下游。")

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "layout_path": ("STRING", {"default": "", "tooltip": (
                    "版式文件（.yaml）的路径，规定成品多大、每一层（底框、图案、文字）放在哪。"
                    "以 @repo/ 开头表示本项目目录下的文件，例如 @repo/examples/layouts/demo_tile.yaml。")}),
                "vars_json": ("STRING", {"default": "{}", "multiline": True, "tooltip": (
                    "填进版式里的文字，写成 {\"变量名\": \"内容\"}。例如版式里的文字层写的是 {name}，"
                    "这里写 {\"name\": \"萬\"}，成品上就印「萬」。没有变量时保持 {}。")}),
            },
            "optional": {
                "art": ("IMAGE", {"tooltip": "要放进版式里的图案（对应版式中的 slot:art）。"}),
                "art_mask": ("MASK", {"tooltip": "图案的透明度蒙版。接上才能保留图案的透明背景。"}),
            },
        }

    @ga_errors
    def run(self, layout_path: str, vars_json: str = "{}", art: torch.Tensor | None = None,
            art_mask: torch.Tensor | None = None) -> tuple[torch.Tensor, torch.Tensor]:
        try:
            vars_ = json.loads(vars_json or "{}")
            if not isinstance(vars_, dict):
                raise ValueError("顶层必须是对象")
        except ValueError as e:
            raise InputError("bad_vars_json", f"vars_json 不是合法的 JSON 对象：{e}") from e
        path = Path(resolve_repo_path(layout_path)).expanduser()
        layout = load_layout(path)
        slots: dict[str, Image.Image] = {}
        if art is not None:
            slots["art"] = tensors_to_pils(art[:1], art_mask[:1] if art_mask is not None else None)[0]
        out = compose(layout, slots, vars_, path.resolve().parent)
        return pil_to_tensors([out])
