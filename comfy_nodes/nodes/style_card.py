"""风格卡：读 yaml 文件 / 编辑框内容，输出 GA_STYLE。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from game_art.errors import InputError
from game_art.paths import resolve_repo_path
from game_art.style import StyleCard, load_style_card

from ..common import CATEGORY, ga_errors

# 把 .yaml 拖到节点上、又拿不到文件完整路径时，编辑器在 path 里填 "拖入:<文件名>"，只用来显示这张卡是哪来的；
# 内容在 edit 里。要和 web/style_card.js 里的 DROPPED_PREFIX 保持一致。
DROPPED_PREFIX = "拖入:"


class GAStyleCard:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("GA_STYLE",)
    RETURN_NAMES = ("style",)

    DESCRIPTION = ("风格卡：规定一整套素材共用的画风（色板、关键词、规则），保证批量出的图风格一致。"
                   "可以读文件，也可以直接在编辑框里写。")
    OUTPUT_TOOLTIPS = ("风格卡，接到「提示词扩写」「云端生图」等节点的 style。",)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {"path": ("STRING", {"default": "", "tooltip": (
                "风格卡文件（.yaml）的路径。以 @repo/ 开头表示本项目目录下的文件，"
                "例如 @repo/examples/style_cards/sanguo_chibi.yaml；也可以写电脑上的完整路径。"
                "如果下面的编辑框里已经写了完整的风格内容，这里可以留空。\n"
                "也可以把 .yaml 文件直接拖到本节点上。拿不到文件的完整路径时这里会显示「拖入:文件名」，"
                "表示内容已经在下面的编辑框里、不再读文件；之后文件有改动，需要重新拖一次。")})},
            "optional": {"edit": ("STRING", {"default": "", "multiline": True, "placeholder": (
                "在这里直接编辑风格（可留空）。写了的字段会覆盖文件里的同名字段，例如：\n"
                "positive: 日系Q版卡通, 两层明暗的平涂, 深褐色线稿\n"
                "negative: 写实光影, 3D渲染\n"
                "palette: [\"#C8372D\", \"#D4A93C\"]\n"
                "rules:\n  - 色块之间用细金线勾边"), "tooltip": (
                "直接编辑风格内容，格式是「字段: 值」，一行一个。这里写了的字段覆盖文件里的同名字段，"
                "没写的沿用文件。可用字段：name（名称）、description（一句话描述）、palette（色板，颜色列表）、"
                "positive（希望出现的风格关键词）、negative（不希望出现的）、rules（规则列表）。"
                "参考图不写在这里，用「参考图」节点。只有 name 和 description 必填，其余都可以不写；"
                "不想设置的字段整行删掉，或只留字段名不写值（如单独一行 palette:），都表示不设置。")})},
        }

    @ga_errors
    def run(self, path: str = "", edit: str = "") -> tuple[StyleCard]:
        path = path.strip()
        if path.startswith(DROPPED_PREFIX):
            if not edit.strip():
                raise InputError("bad_style_card", (
                    f"这张风格卡（{path[len(DROPPED_PREFIX):].strip()}）是拖进来的，内容应该在编辑框里，但编辑框是空的。"
                    "请把文件重新拖到节点上，或者在 path 里填它的完整路径。"))
            path = ""
        file = Path(resolve_repo_path(path)) if path else None
        return (load_style_card(file, edit),)
