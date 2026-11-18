import builtins
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from game_art.errors import InputError
from game_art.imaging import (
    contact_sheet,
    fit_canvas,
    flatten_for_review,
    from_bytes,
    has_transparency,
    remove_background,
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
