"""PIL / base64 互转。"""
from __future__ import annotations

import base64
import io
import math
from typing import Literal, cast

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

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


# 量主体时，透明度低于这个值的像素不算主体（脚下的淡影、抠图留下的雾边）；和保存时清理残留的常用值一致
SUBJECT_ALPHA = 96
DEFAULT_SUBJECT_RATIO = 0.7
SUBJECT_TOLERANCE = 0.03
MAX_SUBJECT_WIDTH = 0.9  # 主体最宽占画面宽度的几成；横着拿长兵器的角色先碰到这条线


def subject_box(img: Image.Image, threshold: int = SUBJECT_ALPHA) -> tuple[int, int, int, int] | None:
    """主体在画面里占的矩形范围（左、上、右、下），按不透明的像素量。
    不带透明底的图量不出主体在哪，返回 None；整张都是透明的也返回 None。"""
    if not has_transparency(img):
        return None
    solid = img.convert("RGBA").getchannel("A").point(lambda a: 255 if a >= threshold else 0)
    return solid.getbbox()


def subject_ratio(img: Image.Image) -> float | None:
    """主体的高度占画面高度的几成（0 到 1）。量不出来时返回 None。"""
    box = subject_box(img)
    return None if box is None else (box[3] - box[1]) / img.height


def subject_target(img: Image.Image, ratio: float = DEFAULT_SUBJECT_RATIO,
                   max_width: float = MAX_SUBJECT_WIDTH) -> float | None:
    """这张图的主体调整后实际能占到画面高度的几成：一般就是 ratio；
    主体很宽时受"宽度不超过画面的 max_width"限制，会比 ratio 小。量不出主体时返回 None。"""
    box = subject_box(img)
    if box is None:
        return None
    by_width = max_width * img.width / (box[2] - box[0]) * (box[3] - box[1]) / img.height
    return min(ratio, by_width)


def fit_subject(img: Image.Image, ratio: float = DEFAULT_SUBJECT_RATIO,
                max_width: float = MAX_SUBJECT_WIDTH) -> Image.Image:
    """把主体等比例缩放到占画面高度的 ratio，并放到画面正中；画布尺寸不变。
    主体很宽时以宽度不超过画面的 max_width 为准，不让它贴到左右两边。
    纯本机处理，不调模型。放大超过三成左右线条会发虚，那种情况应该重新出图。
    不带透明底的图量不出主体，报错。"""
    box = subject_box(img)
    if box is None:
        raise InputError("no_subject", "这张图没有透明底，量不出主体在哪，不能调整主体大小")
    rgba = img.convert("RGBA")
    # 往外多留两个像素，把主体边缘半透明的抗锯齿像素一起带上
    left, top = max(0, box[0] - 2), max(0, box[1] - 2)
    right, bottom = min(rgba.width, box[2] + 2), min(rgba.height, box[3] + 2)
    part = rgba.crop((left, top, right, bottom))
    scale = min(ratio * rgba.height / (box[3] - box[1]), max_width * rgba.width / (box[2] - box[0]))
    nw, nh = max(1, round(part.width * scale)), max(1, round(part.height * scale))
    if (nw, nh) != part.size:
        part = part.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    canvas.paste(part, ((rgba.width - nw) // 2, (rgba.height - nh) // 2))
    return canvas


def _checkerboard(w: int, h: int, tile: int = 16) -> Image.Image:
    bg = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    d = ImageDraw.Draw(bg)
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            if ((x // tile) + (y // tile)) % 2:
                d.rectangle([x, y, x + tile - 1, y + tile - 1], fill=(204, 204, 204, 255))
    return bg


def shrink_to(img: Image.Image, max_side: int) -> Image.Image:
    """长边超过 max_side 时等比缩小到 max_side，否则原样返回；max_side <= 0 表示不缩小。"""
    if max_side <= 0 or max(img.size) <= max_side:
        return img
    scale = max_side / max(img.size)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.Resampling.LANCZOS)


def _edge_ramp(length: int, blend: float) -> Image.Image:
    """一条 1 像素高的渐变：两端是 0，往里 blend 这么宽的一段逐渐升到 255，中间一直是 255。"""
    width = max(1, round(length * blend))
    row = [round(255 * min(1.0, min(i, length - 1 - i) / width)) for i in range(length)]
    ramp = Image.new("L", (length, 1))
    ramp.putdata(row)
    return ramp


def make_seamless(img: Image.Image, blend: float = 0.25) -> Image.Image:
    """把一张贴图处理成能无缝平铺的：上下左右首尾相接时看不出接缝。

    做法分两遍，先处理左右、再处理上下。每一遍都把图沿这个方向错开半张（原来的两条边转到了中间，
    错开后的图自己的两边天然接得上），再和没错开的图融合：靠近两边用错开后的图，中间用没错开的图，
    过渡带的宽度是边长的 blend 倍。分两遍是因为一次同时错开横竖两个方向时，错开后的图中间有个十字接缝，
    会在靠近四边的地方露出来。
    适合草地、土地、石板、水面这类没有明显主体的材质；有单独物体的图会在过渡带里出现重影。
    blend 取 0.05 到 0.5；不透明图片照原样处理，带透明度的图连透明度一起融合。
    """
    blend = min(0.5, max(0.05, blend))
    base = img if img.mode in ("RGB", "RGBA") else img.convert("RGBA")
    w, h = base.size
    across = _edge_ramp(w, blend).resize((w, h))
    base = Image.composite(base, ImageChops.offset(base, w // 2, 0), across)
    down = _edge_ramp(h, blend).rotate(90, expand=True).resize((w, h))
    return Image.composite(base, ImageChops.offset(base, 0, h // 2), down)


def tile_preview(img: Image.Image, repeat: int = 2) -> Image.Image:
    """把一张贴图横竖各铺 repeat 次拼成一张，用来检查接缝。"""
    repeat = max(1, repeat)
    w, h = img.size
    out = Image.new(img.mode, (w * repeat, h * repeat))
    for x in range(repeat):
        for y in range(repeat):
            out.paste(img, (x * w, y * h))
    return out


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
