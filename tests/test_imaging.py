import builtins
import importlib.util
import random
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from game_art.errors import InputError
from game_art.imaging import (
    clean_faint_alpha,
    contact_sheet,
    fit_canvas,
    fit_subject,
    flatten_for_review,
    from_bytes,
    has_transparency,
    make_seamless,
    remove_background,
    subject_box,
    subject_ratio,
    tile_preview,
    to_data_url,
    to_png_bytes,
    trim_alpha,
)


def test_png_roundtrip() -> None:
    im = Image.new("RGBA", (4, 4), (1, 2, 3, 4))
    assert from_bytes(to_png_bytes(im)).size == (4, 4)
    assert to_data_url(im).startswith("data:image/png;base64,")


def test_trim_alpha_removes_empty_border() -> None:
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (10, 10), (255, 0, 0, 255)), (45, 45))
    assert trim_alpha(im).size == (10, 10)
    assert trim_alpha(im, padding=2).size == (14, 14)


def test_fit_canvas_keeps_aspect() -> None:
    out = fit_canvas(Image.new("RGBA", (200, 100), (255, 0, 0, 255)), (64, 64))
    assert out.size == (64, 64) and out.mode == "RGBA"
    assert out.getchannel("A").getbbox() == (0, 16, 64, 48)


def test_has_transparency() -> None:
    assert not has_transparency(Image.new("RGB", (4, 4)))
    assert not has_transparency(Image.new("RGBA", (4, 4), (0, 0, 0, 255)))
    assert has_transparency(Image.new("RGBA", (4, 4), (0, 0, 0, 0)))


def test_contact_sheet_size_and_count() -> None:
    imgs = [Image.new("RGBA", (50, 80), (255, 0, 0, 128)) for _ in range(5)]
    assert contact_sheet(imgs, [str(i + 1) for i in range(5)], cell=256, cols=4).size == (1024, 512)


def test_contact_sheet_empty_raises() -> None:
    with pytest.raises(InputError):
        contact_sheet([], [])


def test_remove_background_none() -> None:
    assert remove_background(Image.new("RGB", (4, 4)), "none").mode == "RGBA"


def test_rembg_missing_raises_input_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real = builtins.__import__

    def fake(name: str, *a: Any, **k: Any) -> Any:
        if name == "rembg":
            raise ImportError("no rembg")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(InputError) as ei:
        remove_background(Image.new("RGB", (4, 4)))
    assert ei.value.code == "rembg_missing" and "pip install rembg" in ei.value.message


@pytest.mark.skipif(importlib.util.find_spec("rembg") is None, reason="rembg 未安装")
def test_rembg_produces_alpha() -> None:
    out = remove_background(Image.new("RGB", (64, 64), (200, 30, 30)))
    assert out.mode == "RGBA"


def test_flatten_for_review_shows_checkerboard_under_transparency() -> None:
    out = flatten_for_review(Image.new("RGBA", (32, 32), (0, 0, 0, 0)))
    assert out.mode == "RGB" and out.size == (32, 32)
    assert out.getpixel((0, 0)) == (255, 255, 255)
    assert out.getpixel((16, 0)) == (204, 204, 204)


def test_flatten_for_review_keeps_opaque_pixels() -> None:
    out = flatten_for_review(Image.new("RGBA", (8, 8), (10, 20, 30, 255)))
    assert out.getpixel((3, 3)) == (10, 20, 30)
    assert flatten_for_review(Image.new("RGB", (8, 8), (1, 2, 3))).getpixel((0, 0)) == (1, 2, 3)


def test_rembg_hint_uses_comfy_root_from_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"comfy_root": str(tmp_path / "MyComfy")}))
    monkeypatch.setenv("GAME_ART_CONFIG", str(cfg))
    from game_art.imaging import _rembg_hint
    h = _rembg_hint()
    assert f"{tmp_path / 'MyComfy'}/.venv/bin/python3 -m pip install rembg" in h
    assert "uv sync --extra rembg" in h


def test_rembg_hint_falls_back_when_config_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAME_ART_CONFIG", str(tmp_path / "missing.yaml"))
    from game_art.imaging import _rembg_hint
    assert "Documents/ComfyUI/.venv/bin/python3 -m pip install rembg" in _rembg_hint()


def test_clean_faint_alpha_removes_detached_haze_keeps_edges() -> None:
    im = Image.new("RGBA", (12, 6), (0, 0, 0, 0))
    for y in range(6):
        im.putpixel((1, y), (9, 9, 9, 255))    # 实体
        im.putpixel((2, y), (9, 9, 9, 60))     # 紧贴实体的抗锯齿边
        im.putpixel((3, y), (9, 9, 9, 30))     # 仍在 edge=2 范围内
        im.putpixel((4, y), (9, 9, 9, 30))     # 超出范围：擦掉
        im.putpixel((10, y), (7, 8, 9, 60))    # 远处的淡阴影：擦掉
    out = clean_faint_alpha(im, 96)
    alphas = [out.getchannel("A").getpixel((x, 3)) for x in (1, 2, 3, 4, 10)]
    assert alphas == [255, 60, 30, 0, 0]
    assert out.getpixel((10, 3)) == (7, 8, 9, 0)  # 只改透明度，不改颜色
    assert im.getchannel("A").getpixel((10, 3)) == 60  # 不修改入参
    # 阴影自身的透明度达到阈值时算实体，不会被擦
    assert clean_faint_alpha(im, 60).getchannel("A").getpixel((10, 3)) == 60
    # edge=0：只留达到阈值的像素
    assert [clean_faint_alpha(im, 96, edge=0).getchannel("A").getpixel((x, 3)) for x in (1, 2)] == [255, 0]


def test_clean_faint_alpha_zero_is_noop_and_handles_rgb() -> None:
    im = Image.new("RGBA", (4, 4), (1, 2, 3, 10))
    assert clean_faint_alpha(im, 0).tobytes() == im.tobytes()
    assert clean_faint_alpha(Image.new("RGB", (4, 4), (5, 5, 5)), 128).getpixel((0, 0)) == (5, 5, 5, 255)
    # 整张都很淡：全部擦掉；阈值超过 255 按 255 算
    assert clean_faint_alpha(im, 999).getchannel("A").getextrema() == (0, 0)


def _noise(size: tuple[int, int], mode: str = "RGB") -> Image.Image:
    rng = random.Random(7)
    img = Image.new(mode, size)
    bands = len(mode)
    img.putdata([tuple(rng.randrange(256) for _ in range(bands)) for _ in range(size[0] * size[1])])
    return img


def _wrap_jump(img: Image.Image) -> int:
    """平铺时接缝两侧的最大色差：最左列对最右列、最上行对最下行。"""
    w, h = img.size
    data = img.convert("L").tobytes()
    lr = max(abs(data[y * w] - data[y * w + w - 1]) for y in range(h))
    tb = max(abs(data[x] - data[(h - 1) * w + x]) for x in range(w))
    return max(lr, tb)


def test_make_seamless_edges_match_everywhere_and_center_kept() -> None:
    # 斜向渐变：左上暗、右下亮，直接平铺时左右、上下接缝处都有明显色差
    grad = Image.linear_gradient("L").resize((64, 64))
    src = Image.blend(grad, grad.rotate(90), 0.5).convert("RGB")
    assert _wrap_jump(src) > 100
    out = make_seamless(src, 0.25)
    assert out.size == src.size and out.mode == "RGB"
    assert _wrap_jump(out) <= 8  # 四条边整条都接得上，包括靠近角上的位置
    assert out.getpixel((32, 32)) == src.getpixel((32, 32))  # 中间保持原图


def test_make_seamless_keeps_alpha_and_clamps_blend() -> None:
    src = _noise((20, 12), "RGBA")
    assert make_seamless(src, 5.0).mode == "RGBA"
    assert make_seamless(src.convert("L"), 0.0).mode == "RGBA"


def test_tile_preview_repeats() -> None:
    src = _noise((10, 6))
    out = tile_preview(src, 2)
    assert out.size == (20, 12)
    assert out.getpixel((13, 8)) == src.getpixel((3, 2))


def _figure(canvas: tuple[int, int], box: tuple[int, int, int, int]) -> Image.Image:
    img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    img.paste((200, 60, 60, 255), box)
    return img


def test_subject_ratio_measures_by_alpha_and_ignores_faint_pixels() -> None:
    img = _figure((100, 200), (40, 50, 60, 150))  # 主体高 100，占一半
    img.paste((0, 0, 0, 40), (0, 190, 100, 200))  # 脚下一条很淡的影子，不算主体
    assert subject_box(img) == (40, 50, 60, 150)
    assert subject_ratio(img) == pytest.approx(0.5)


def test_subject_ratio_none_for_opaque_or_empty() -> None:
    assert subject_ratio(Image.new("RGB", (10, 10), "red")) is None
    assert subject_ratio(Image.new("RGBA", (10, 10), (0, 0, 0, 0))) is None


@pytest.mark.parametrize("box", [(40, 80, 60, 120), (30, 5, 70, 195)], ids=["放大", "缩小"])
def test_fit_subject_scales_to_ratio_and_centers(box: tuple[int, int, int, int]) -> None:
    out = fit_subject(_figure((100, 200), box), 0.7)
    assert out.size == (100, 200) and out.mode == "RGBA"
    assert subject_ratio(out) == pytest.approx(0.7, abs=0.02)
    left, top, right, bottom = subject_box(out) or (0, 0, 0, 0)
    assert abs(left - (100 - right)) <= 2 and abs(top - (200 - bottom)) <= 2  # 上下、左右都居中


def test_fit_subject_wide_subject_limited_by_width() -> None:
    out = fit_subject(_figure((100, 100), (10, 45, 90, 55)), 0.7)  # 很宽很扁：按高度放大会冲出左右
    left, _top, right, _bottom = subject_box(out) or (0, 0, 0, 0)
    assert (right - left) / 100 == pytest.approx(0.9, abs=0.03)


def test_fit_subject_rejects_opaque() -> None:
    with pytest.raises(InputError) as e:
        fit_subject(Image.new("RGB", (10, 10), "red"))
    assert e.value.code == "no_subject"
