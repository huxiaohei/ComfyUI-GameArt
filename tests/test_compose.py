from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from game_art.compose import Layout, compose, load_layout
from game_art.errors import InputError

ROOT = Path(__file__).resolve().parents[1]
# 示例字体不随仓库分发（见 examples/fonts/README.md）；没下载时跳过依赖它的测试
needs_demo_font = pytest.mark.skipif(
    not (ROOT / "examples/fonts/SourceHanSerifHK-Bold.otf").is_file(), reason="未下载示例字体")


def solid(color: tuple[int, int, int, int], size: tuple[int, int] = (20, 20)) -> Image.Image:
    return Image.new("RGBA", size, color)


def _px(im: Image.Image, xy: tuple[int, int]) -> tuple[int, ...]:
    v = im.getpixel(xy)
    assert isinstance(v, tuple)
    return v


def mk(layers: list[dict[str, Any]], size: tuple[int, int] = (100, 100)) -> Layout:
    return Layout.model_validate({"size": size, "layers": layers})


def test_layers_drawn_in_order(tmp_path: Path) -> None:
    Image.new("RGBA", (10, 10), (255, 0, 0, 255)).save(tmp_path / "red.png")
    lay = mk([
        {"type": "image", "src": "red.png", "box": [0, 0, 100, 100], "fit": "stretch"},
        {"type": "image", "src": "slot:art", "box": [25, 25, 50, 50], "fit": "stretch"},
    ])
    out = compose(lay, {"art": solid((0, 0, 255, 255))}, {}, tmp_path)
    assert out.mode == "RGBA" and out.size == (100, 100)
    assert out.getpixel((5, 5)) == (255, 0, 0, 255)
    assert out.getpixel((50, 50)) == (0, 0, 255, 255)


def test_text_var_substitution_and_missing_var(tmp_path: Path) -> None:
    lay = mk([{"type": "text", "text": "{name}", "font": "default", "size": 40,
               "color": "#000000", "box": [0, 0, 100, 100]}])
    with pytest.raises(InputError) as e:
        compose(lay, {}, {}, tmp_path)
    assert "name" in e.value.message
    out = compose(lay, {}, {"name": "A"}, tmp_path)
    assert out.getchannel("A").getbbox() is not None


def test_vertical_text_and_stroke(tmp_path: Path) -> None:
    lay = mk([{"type": "text", "text": "AB", "font": "default", "size": 30, "color": "#ffffff",
               "box": [0, 0, 100, 100], "vertical": True,
               "stroke": {"width": 2, "color": "#ff0000"}}])
    out = compose(lay, {}, {}, tmp_path)
    bbox = out.getchannel("A").getbbox()
    assert bbox is not None
    left, t, r, b = bbox
    assert b - t > r - left  # 竖排：高大于宽
    assert _px(out, (50, 2))[3] == 0


def test_missing_slot_lists_names(tmp_path: Path) -> None:
    lay = mk([
        {"type": "image", "src": "slot:art", "box": [0, 0, 10, 10]},
        {"type": "repeat", "src": "slot:dot", "positions": [[0, 0, 5, 5]]},
    ])
    with pytest.raises(InputError) as e:
        compose(lay, {}, {}, tmp_path)
    assert "art" in e.value.message and "dot" in e.value.message


def test_missing_font_file(tmp_path: Path) -> None:
    lay = mk([{"type": "text", "text": "x", "font": "nope.ttf", "size": 20,
               "color": "#000", "box": [0, 0, 50, 50]}])
    with pytest.raises(InputError) as e:
        compose(lay, {}, {}, tmp_path)
    assert "nope.ttf" in e.value.message


def test_missing_image_file(tmp_path: Path) -> None:
    lay = mk([{"type": "image", "src": "gone.png", "box": [0, 0, 10, 10]}])
    with pytest.raises(InputError):
        compose(lay, {}, {}, tmp_path)


def test_repeat_layer_places_n_copies(tmp_path: Path) -> None:
    Image.new("RGBA", (10, 10), (0, 255, 0, 255)).save(tmp_path / "dot.png")
    pos = [[5, 5, 10, 10], [40, 5, 10, 10], [75, 5, 10, 10]]
    lay = mk([{"type": "repeat", "src": "dot.png", "positions": pos}])
    out = compose(lay, {}, {}, tmp_path)
    for x, y, w, h in pos:
        assert out.getpixel((x + w // 2, y + h // 2)) == (0, 255, 0, 255)
    assert _px(out, (25, 10))[3] == 0 and _px(out, (60, 10))[3] == 0


def test_opacity_and_fit_contain(tmp_path: Path) -> None:
    lay = mk([{"type": "image", "src": "slot:a", "box": [0, 0, 100, 100],
               "fit": "contain", "opacity": 0.5}])
    out = compose(lay, {"a": solid((255, 255, 255, 255), (40, 20))}, {}, tmp_path)
    assert _px(out, (50, 50))[3] in (127, 128)
    assert _px(out, (50, 5))[3] == 0  # contain 留边


def test_cover_fills_box(tmp_path: Path) -> None:
    lay = mk([{"type": "image", "src": "slot:a", "box": [0, 0, 100, 100], "fit": "cover"}])
    out = compose(lay, {"a": solid((9, 9, 9, 255), (40, 20))}, {}, tmp_path)
    assert _px(out, (1, 1))[3] == 255 and _px(out, (98, 98))[3] == 255


@needs_demo_font
def test_output_is_rgba_with_transparent_outside(tmp_path: Path) -> None:
    lay = load_layout(ROOT / "examples/layouts/demo_tile.yaml")
    out = compose(lay, {"art": solid((0, 0, 255, 255))}, {"name": "A"}, ROOT / "examples/layouts")
    assert out.mode == "RGBA"
    assert _px(out, (0, 0))[3] == 0


@needs_demo_font
def test_demo_layout_loads_and_renders() -> None:
    base = ROOT / "examples/layouts"
    lay = load_layout(base / "demo_tile.yaml")
    assert lay.size == (400, 560)
    out = compose(lay, {"art": solid((200, 0, 0, 255), (50, 80))}, {"name": "一二"}, base)
    assert out.size == (400, 560)
    assert out.getpixel((200, 200)) == (200, 0, 0, 255)


@needs_demo_font
def test_demo_layout_renders_cjk_with_bundled_font() -> None:
    # 示例牌面用仓库内的思源宋体（香港字形），"萬"必须真正画出字形，而不是空心方框
    from PIL import ImageFont

    base = ROOT / "examples/layouts"
    lay = load_layout(base / "demo_tile.yaml")
    text_layer = next(layer for layer in lay.layers if layer.type == "text")
    font_path = base / text_layer.font
    assert font_path.is_file()
    font = ImageFont.truetype(str(font_path), 64)
    tofu = font.getmask("\U000f0000").getbbox()  # 私用区字符：字体里没有，渲染为缺字框
    wan = font.getmask("萬").getbbox()
    assert wan is not None and wan != tofu
    out = compose(lay, {"art": solid((0, 0, 0, 0))}, {"name": "萬"}, base)
    region = out.crop((150, 350, 250, 530)).convert("L")
    import numpy as np

    dark = int((np.asarray(region) < 100).sum())
    assert dark > 300  # 宋体粗笔画的墨量远多于空心方框


def test_missing_font_error_points_to_readme(tmp_path: Path) -> None:
    lay = mk([{"type": "text", "text": "A", "font": "nope.otf", "size": 10,
              "color": "#000000", "box": [0, 0, 10, 10]}])
    with pytest.raises(InputError) as e:
        compose(lay, {}, {}, tmp_path)
    assert e.value.code == "font_not_found" and "examples/fonts/README.md" in e.value.message
