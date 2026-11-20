"""PIL / base64 互转。"""
from __future__ import annotations

import base64
import io
import math
from typing import Literal, cast

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from game_art.errors import InputError


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def from_bytes(b: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(b))
    img.load()
    return img


def to_data_url(img: Image.Image) -> str:
    return "data:image/png;base64," + base64.b64encode(to_png_bytes(img)).decode("ascii")


# ---- Task 5: alpha / 去背景 / 拼图预览 ----
_DEFAULT_COMFY_ROOT = "~/Documents/ComfyUI"


def _rembg_hint() -> str:
    """ComfyUI 实际运行在 <comfy_root>/.venv；尽量用配置里的 comfy_root 拼出安装命令。"""
    root = _DEFAULT_COMFY_ROOT
    try:
        from .config import load_settings
        root = str(load_settings().comfy_root)
    except Exception:
        pass
    return (f"未安装 rembg。ComfyUI 环境安装：{root}/.venv/bin/python3 -m pip install rembg ；"
            "开发环境：uv sync --extra rembg")


def remove_background(img: Image.Image, method: Literal["rembg", "none"] = "rembg") -> Image.Image:
    if method == "none":
        return img.convert("RGBA")
    if method != "rembg":
        raise InputError("bad_bg_method", f"未知去背景方法：{method}（可用：rembg, none）")
    try:
        from rembg import remove  # 可选依赖，惰性导入
    except ImportError as e:
        raise InputError("rembg_missing", _rembg_hint()) from e
    out: Image.Image = remove(img.convert("RGBA"))
    return out.convert("RGBA")


def has_transparency(img: Image.Image) -> bool:
    if img.mode not in ("RGBA", "LA") and "transparency" not in img.info:
        return False
    alpha = img.convert("RGBA").getchannel("A")
    lo, _hi = cast("tuple[int, int]", alpha.getextrema())
    return lo < 250


def trim_alpha(img: Image.Image, padding: int = 0) -> Image.Image:
    rgba = img.convert("RGBA")
    bbox = rgba.getchannel("A").getbbox()
    if bbox is None:
        return rgba
    left, top, right, bottom = bbox
    left, top = max(0, left - padding), max(0, top - padding)
    right, bottom = min(rgba.width, right + padding), min(rgba.height, bottom + padding)
    return rgba.crop((left, top, right, bottom))


def clean_faint_alpha(img: Image.Image, threshold: int, edge: int = 2) -> Image.Image:
    """清理半透明残留：透明度低于 threshold、且离"实体像素"（透明度不低于 threshold）超过 edge 个像素的点，
    擦成全透明。用来去掉模型画在主体脚下的淡阴影、抠图留下的雾边；紧贴主体的抗锯齿边缘不动。
    threshold <= 0 表示不清理；颜色通道不改。"""
    rgba = img.convert("RGBA")
    if threshold <= 0:
        return rgba
    threshold = min(threshold, 255)
    alpha = rgba.getchannel("A")
    solid = alpha.point(lambda a: 255 if a >= threshold else 0)
    keep = solid.filter(ImageFilter.MaxFilter(2 * max(edge, 0) + 1))
    rgba.putalpha(Image.composite(alpha, Image.new("L", rgba.size, 0), keep))
    return rgba


def fit_canvas(img: Image.Image, size: tuple[int, int], align: str = "center") -> Image.Image:
    if align != "center":
        raise InputError("bad_align", f"不支持的对齐方式：{align}（可用：center）")
    w, h = size
    rgba = img.convert("RGBA")
    scale = min(w / rgba.width, h / rgba.height)
    nw, nh = max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale))
    resized = rgba.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2))
    return canvas


def _checkerboard(w: int, h: int, tile: int = 16) -> Image.Image:
    bg = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    d = ImageDraw.Draw(bg)
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            if ((x // tile) + (y // tile)) % 2:
                d.rectangle([x, y, x + tile - 1, y + tile - 1], fill=(204, 204, 204, 255))
    return bg


def flatten_for_review(img: Image.Image) -> Image.Image:
    """RGBA 叠在棋盘格上转 RGB，让视觉模型能看出透明问题（残留底色、毛边）。"""
    rgba = img.convert("RGBA")
    bg = _checkerboard(rgba.width, rgba.height)
    bg.alpha_composite(rgba)
    return bg.convert("RGB")


def contact_sheet(
    images: list[Image.Image], labels: list[str], cell: int = 256, cols: int = 4
) -> Image.Image:
    if not images:
        raise InputError("empty_images", "拼图预览需要至少一张图片")
    if len(labels) != len(images):
        raise InputError("label_mismatch", f"标签数量（{len(labels)}）与图片数量（{len(images)}）不一致")
    rows = math.ceil(len(images) / cols)
    sheet = Image.new("RGBA", (cols * cell, rows * cell), (255, 255, 255, 255))
    font = ImageFont.load_default(size=max(12, cell // 10))
    for i, (im, label) in enumerate(zip(images, labels, strict=True)):
        x0, y0 = (i % cols) * cell, (i // cols) * cell
        tile = _checkerboard(cell, cell)
        tile.alpha_composite(fit_canvas(im, (cell, cell)))
        d = ImageDraw.Draw(tile)
        text = str(label)
        _left, _top, r, b = d.textbbox((4, 4), text, font=font)
        d.rectangle([0, 0, r + 4, b + 4], fill=(0, 0, 0, 255))
        d.text((4, 4), text, fill=(255, 255, 255, 255), font=font)
        sheet.paste(tile, (x0, y0))
    return sheet
