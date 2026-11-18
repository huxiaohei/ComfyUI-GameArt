"""模板合成：布局 YAML → RGBA。文字与精确数量图案一律在这里程序化绘制，不交给 AI。"""
from __future__ import annotations

import string
from pathlib import Path
from typing import Annotated, Literal

import yaml
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field, ValidationError

from game_art.errors import InputError

Box = tuple[int, int, int, int]


class Stroke(BaseModel):
    width: int
    color: str


class ImageLayer(BaseModel):
    type: Literal["image"]
    src: str
    box: Box
    fit: Literal["contain", "cover", "stretch"] = "contain"
    opacity: float = 1


class TextLayer(BaseModel):
    type: Literal["text"]
    text: str
    font: str
    size: int
    color: str
    box: Box
    align: Literal["center", "left", "right"] = "center"
    vertical: bool = False
    stroke: Stroke | None = None


class RepeatLayer(BaseModel):
    type: Literal["repeat"]
    src: str
    positions: list[Box]


Layer = Annotated[ImageLayer | TextLayer | RepeatLayer, Field(discriminator="type")]


class Layout(BaseModel):
    size: tuple[int, int]
    layers: list[Layer]


def load_layout(path: Path) -> Layout:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise InputError("layout_unreadable", f"无法读取布局文件 {path}：{e}") from e
    except yaml.YAMLError as e:
        raise InputError("layout_bad_yaml", f"布局文件 {path} 不是合法 YAML：{e}") from e
    try:
        return Layout.model_validate(data)
    except ValidationError as e:
        raise InputError("layout_invalid", f"布局文件 {path} 格式有误：{e}") from e


def _fields(text: str) -> set[str]:
    names = set()
    for _, field, _, _ in string.Formatter().parse(text):
        if field is not None:
            names.add(field.split(".")[0].split("[")[0])
    return names


def _check_inputs(layout: Layout, slots: dict[str, Image.Image], vars: dict[str, str]) -> None:
    need_slots: list[str] = []
    need_vars: list[str] = []
    for layer in layout.layers:
        if isinstance(layer, (ImageLayer, RepeatLayer)) and layer.src.startswith("slot:"):
            name = layer.src[5:]
            if name not in slots and name not in need_slots:
                need_slots.append(name)
        elif isinstance(layer, TextLayer):
            for n in sorted(_fields(layer.text)):
                if n not in vars and n not in need_vars:
                    need_vars.append(n)
    parts = []
    if need_slots:
        parts.append("缺少 slot：" + ", ".join(need_slots))
    if need_vars:
        parts.append("缺少文字变量：" + ", ".join(need_vars))
    if parts:
        raise InputError(
            "compose_missing_input", "；".join(parts),
            {"missing_slots": need_slots, "missing_vars": need_vars},
        )


def _load_src(src: str, slots: dict[str, Image.Image], base_dir: Path) -> Image.Image:
    if src.startswith("slot:"):
        return slots[src[5:]].convert("RGBA")
    p = Path(src).expanduser()
    if not p.is_absolute():
        p = Path(base_dir) / p
    try:
        with Image.open(p) as im:
            return im.convert("RGBA")
    except FileNotFoundError as e:
        raise InputError("image_not_found", f"图片文件不存在：{p}") from e
    except OSError as e:
        raise InputError("image_unreadable", f"无法读取图片 {p}：{e}") from e


def _fit(img: Image.Image, w: int, h: int, fit: str) -> Image.Image:
    """返回 w×h 的 RGBA 画布，img 按 fit 方式放入。"""
    if fit == "stretch":
        return img.resize((w, h), Image.Resampling.LANCZOS)
    scale = (min if fit == "contain" else max)(w / img.width, h / img.height)
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2))
    return canvas


def _paste(canvas: Image.Image, img: Image.Image, x: int, y: int, opacity: float = 1) -> None:
    if opacity < 1:
        img = img.copy()
        a = img.getchannel("A").point(lambda v: round(v * max(0.0, opacity)))
        img.putalpha(a)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer.paste(img, (x, y))
    canvas.alpha_composite(layer)


def _font(layer: TextLayer, base_dir: Path) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if layer.font == "default":
        return ImageFont.load_default(layer.size)
    p = Path(layer.font).expanduser()
    if not p.is_absolute():
        p = Path(base_dir) / p
    if not p.is_file():
        raise InputError("font_not_found",
                         f"字体文件不存在：{p}（字体不随仓库分发，获取方法见 examples/fonts/README.md）")
    try:
        return ImageFont.truetype(str(p), layer.size)
    except OSError as e:
        raise InputError("font_unreadable", f"无法加载字体 {p}：{e}") from e


def _draw_text(canvas: Image.Image, layer: TextLayer, text: str,
               font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> None:
    x, y, w, h = layer.box
    d = ImageDraw.Draw(canvas)
    sw = layer.stroke.width if layer.stroke else 0
    sf = layer.stroke.color if layer.stroke else None
    if layer.vertical:
        gap = max(2, layer.size // 8)
        boxes = [d.textbbox((0, 0), ch, font=font, stroke_width=sw) for ch in text]
        total = sum(b[3] - b[1] for b in boxes) + gap * (len(boxes) - 1)
        cy = y + (h - total) / 2
        for ch, b in zip(text, boxes, strict=True):
            cx = x + (w - (b[2] - b[0])) / 2
            d.text((cx - b[0], cy - b[1]), ch, font=font, fill=layer.color, stroke_width=sw, stroke_fill=sf)
            cy += (b[3] - b[1]) + gap
        return
    b = d.textbbox((0, 0), text, font=font, stroke_width=sw)
    tw, th = b[2] - b[0], b[3] - b[1]
    cx = {"left": x, "center": x + (w - tw) / 2, "right": x + w - tw}[layer.align]
    cy = y + (h - th) / 2
    d.text((cx - b[0], cy - b[1]), text, font=font, fill=layer.color, stroke_width=sw, stroke_fill=sf)


def compose(layout: Layout, slots: dict[str, Image.Image], vars: dict[str, str],
            base_dir: Path) -> Image.Image:
    _check_inputs(layout, slots, vars)
    canvas = Image.new("RGBA", layout.size, (0, 0, 0, 0))
    for layer in layout.layers:
        if isinstance(layer, ImageLayer):
            x, y, w, h = layer.box
            img = _fit(_load_src(layer.src, slots, base_dir), w, h, layer.fit)
            _paste(canvas, img, x, y, layer.opacity)
        elif isinstance(layer, RepeatLayer):
            src = _load_src(layer.src, slots, base_dir)
            for x, y, w, h in layer.positions:
                _paste(canvas, _fit(src, w, h, "contain"), x, y)
        else:
            text = layer.text.format_map({k: str(v) for k, v in vars.items()})
            _draw_text(canvas, layer, text, _font(layer, base_dir))
    return canvas
