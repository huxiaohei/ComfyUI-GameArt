import json
import pathlib
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest
import torch
from PIL import Image

from comfy_nodes import common, nodes
from comfy_nodes.tensors import pil_to_tensors, tensors_to_pils
from game_art import imaging
from game_art.config import Pricing, Target
from game_art.errors import UpstreamError
from game_art.providers.base import ChatResult, ImageRequest, ImageResult, Message
from game_art.style import StyleCard
from tests.conftest import write_cfg

ALL = {"GAStyleCard", "GAReferenceImages", "GAPromptExpand", "GAReferenceAnalyze", "GACloudGenerate", "GACloudEdit",
       "GARemoveBackground", "GAQCScore", "GAComposeTemplate", "GASaveAsset"}


class FakeLedger:
    pricing: ClassVar[dict[str, Pricing]] = {"fake:img": Pricing(per_image=0.5)}


class FakeRouter:
    def __init__(self, size: tuple[int, int] = (8, 8), sizes: list[tuple[int, int]] | None = None,
                 text: str = '{"prompt":"p","negative":"n"}', fail: Exception | None = None) -> None:
        self.ledger = FakeLedger()
        self.size, self.sizes, self.text, self.fail = size, sizes, text, fail
        self.calls: list[tuple[Any, ...]] = []

    def _images(self, req: ImageRequest) -> ImageResult:
        sizes = self.sizes or [self.size] * req.n
        return ImageResult(images=[Image.new("RGBA", s, (255, 0, 0, 255)) for s in sizes],
                           partial=False, seed=req.seed, raw={})

    def generate(self, profile: str, req: ImageRequest, *, tag: str) -> tuple[ImageResult, Target]:
        self.calls.append(("generate", profile, req, tag))
        if self.fail:
            raise self.fail
        return self._images(req), Target(provider="fake", model="img")

    def edit(self, profile: str, req: ImageRequest, *, tag: str) -> tuple[ImageResult, Target]:
        self.calls.append(("edit", profile, req, tag))
        return self._images(req), Target(provider="fake", model="img")

    def chat(self, profile: str, messages: list[Message], *, tag: str,
             json_mode: bool = False) -> tuple[ChatResult, Target]:
        self.calls.append(("chat", profile, messages, tag))
        return ChatResult(text=self.text, input_tokens=1, output_tokens=1, raw={}), \
            Target(provider="fake", model="txt")


def _px(im: Image.Image, xy: tuple[int, int]) -> tuple[int, ...]:
    v = im.getpixel(xy)
    assert isinstance(v, tuple)
    return v


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> FakeRouter:
    r = FakeRouter()
    monkeypatch.setattr(common, "get_router", lambda: r)
    return r


def test_tensor_roundtrip_rgba() -> None:
    im = Image.new("RGBA", (3, 2))
    px = [(255, 0, 0, 255), (0, 255, 0, 128), (0, 0, 255, 0), (10, 20, 30, 77), (1, 2, 3, 4), (9, 9, 9, 255)]
    im.putdata(px)
    image, mask = pil_to_tensors([im])
    assert image.shape == (1, 2, 3, 3) and mask.shape == (1, 2, 3)
    assert image.dtype == torch.float32 and mask.dtype == torch.float32
    assert mask[0, 0, 0].item() == 0 and mask[0, 0, 2].item() == pytest.approx(1.0)  # mask = 1 - alpha
    back = tensors_to_pils(image, mask)[0]
    assert [back.getpixel((x, y)) for y in range(2) for x in range(3)] == px


def test_tensors_to_pils_without_mask_is_rgb() -> None:
    out = tensors_to_pils(torch.ones(2, 4, 4, 3))
    assert len(out) == 2 and out[0].mode == "RGB" and out[0].getpixel((0, 0)) == (255, 255, 255)


def test_mapping_contains_all_nodes() -> None:
    assert set(nodes.NODE_CLASS_MAPPINGS) == ALL
    assert set(nodes.NODE_DISPLAY_NAME_MAPPINGS) == ALL


def test_input_types_are_valid_comfy_shapes() -> None:
    for name, node_cls in nodes.NODE_CLASS_MAPPINGS.items():
        cls: Any = node_cls
        it = cls.INPUT_TYPES()
        assert "required" in it, name
        assert isinstance(cls.RETURN_TYPES, tuple) and cls.CATEGORY == "GameArt", name
        assert callable(getattr(cls, cls.FUNCTION)), name
        if cls.RETURN_TYPES:
            assert len(cls.RETURN_NAMES) == len(cls.RETURN_TYPES), name
    assert nodes.GACloudGenerate.INPUT_TYPES()["optional"]["edit_profile"][1]["default"] == "image_edit"
    assert nodes.GASaveAsset.OUTPUT_NODE is True


def test_generate_node_with_fake_router(router: FakeRouter) -> None:
    image, mask, meta = nodes.GACloudGenerate().run(
        prompt="龙", negative="糊", n=2, seed=-1, meta={"brief": "b"}, tag="t1")
    assert image.shape[0] == 2 and mask.shape[0] == 2 and image.shape[-1] == 3
    assert meta["provider"] == "fake" and meta["model"] == "img" and meta["brief"] == "b"
    assert meta["cost"] == pytest.approx(1.0) and meta["size"] == "1024x1024"
    kind, profile, req, tag = router.calls[0]
    assert (kind, profile, tag, req.seed, req.n) == ("generate", "image", "t1", None, 2)


def test_generate_seed_and_empty_tag(router: FakeRouter) -> None:
    nodes.GACloudGenerate().run(prompt="x", negative="", seed=7)
    _, _, req, tag = router.calls[0]
    assert req.seed == 7 and tag.startswith("run/") and len(tag) > 4


def test_generate_unifies_mismatched_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRouter(sizes=[(8, 8), (4, 6)])
    monkeypatch.setattr(common, "get_router", lambda: r)
    image, _, _ = nodes.GACloudGenerate().run(prompt="x", negative="", size="16x12", n=2)
    assert tuple(image.shape) == (2, 12, 16, 3)


def test_generate_with_refs_switches_to_edit(router: FakeRouter, tmp_path: pathlib.Path) -> None:
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(a)
    Image.new("RGBA", (6, 2), (4, 5, 6, 7)).save(b)
    refs, _preview = nodes.GAReferenceImages().run(paths=f"{a}\n{b}")
    nodes.GACloudGenerate().run(prompt="x", negative="", refs=refs, edit_profile="my_edit")
    kind, profile, req, _ = router.calls[0]
    assert kind == "edit" and profile == "my_edit"
    assert [r.size for r in req.refs] == [(4, 4), (6, 2)]  # 多张、各自保留原始尺寸
    # 元数据里记下发了几张参考图，方便事后确认参考图有没有真的发出去
    assert nodes.GACloudGenerate().run(prompt="x", negative="", refs=refs)[2]["refs"] == 2
    assert nodes.GACloudGenerate().run(prompt="x", negative="")[2]["refs"] == 0
    assert nodes.GACloudEdit().run(prompt="x", refs=refs, images=torch.zeros(1, 8, 8, 3))[2]["refs"] == 3
    router.calls.clear()
    # 不接参考图、或参考图节点为空时仍走 generate
    nodes.GACloudGenerate().run(prompt="x", negative="")
    empty, _ = nodes.GAReferenceImages().run(paths="")
    nodes.GACloudGenerate().run(prompt="x", negative="", refs=empty)
    assert [c[0] for c in router.calls] == ["generate", "generate"]


def test_reference_images_node(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    monkeypatch.setitem(sys.modules, "folder_paths", types.SimpleNamespace(
        get_input_directory=lambda: str(input_dir)))
    a, b = tmp_path / "a.png", input_dir / "b.png"
    Image.new("RGBA", (8, 4), (255, 0, 0, 255)).save(a)
    Image.new("RGB", (2, 6), (0, 255, 0)).save(b)
    # 一行一个；分号也行；空行、# 注释、两端引号都能处理；只写文件名 = ComfyUI input 目录
    text = f"\n# 注释\n'{a}' ; b.png\n\n"
    refs, preview = nodes.GAReferenceImages().run(paths=text)
    assert [(r.mode, r.size) for r in refs] == [("RGBA", (8, 4)), ("RGBA", (2, 6))]
    assert tuple(preview.shape) == (2, 4, 8, 3)  # 预览统一成第一张的尺寸
    # images 口进来的图排在 paths 后面
    refs, preview = nodes.GAReferenceImages().run(paths=str(a), images=torch.zeros(2, 5, 5, 3))
    assert [r.size for r in refs] == [(8, 4), (5, 5), (5, 5)] and preview.shape[0] == 3
    # 留空 = 不用参考图
    refs, preview = nodes.GAReferenceImages().run()
    assert refs == [] and tuple(preview.shape) == (1, 64, 64, 3)
    with pytest.raises(RuntimeError, match="参考图不存在"):
        nodes.GAReferenceImages().run(paths=str(tmp_path / "nope.png"))
    (tmp_path / "bad.png").write_text("not an image")
    with pytest.raises(RuntimeError, match="无法读取参考图"):
        nodes.GAReferenceImages().run(paths=str(tmp_path / "bad.png"))


def test_reference_images_extra_image_slots(tmp_path: pathlib.Path) -> None:
    """拖图进来时每张图占一个接线口；顺序是 paths → images → image_2 … image_8，和连线先后无关。"""
    cls: Any = nodes.GAReferenceImages
    optional = cls.INPUT_TYPES()["optional"]
    assert list(optional) == ["images", *(f"image_{i}" for i in range(2, 9))]
    assert all(spec[0] == "IMAGE" for spec in optional.values())
    a = tmp_path / "a.png"
    Image.new("RGB", (9, 9)).save(a)
    refs, preview = cls().run(paths=str(a), image_3=torch.zeros(1, 3, 3, 3), images=torch.zeros(1, 1, 1, 3),
                              image_2=None, image_8=torch.zeros(2, 8, 8, 3))
    assert [r.size for r in refs] == [(9, 9), (1, 1), (3, 3), (8, 8), (8, 8)] and preview.shape[0] == 5


def test_reference_images_validates_paths_before_run(tmp_path: pathlib.Path) -> None:
    """VALIDATE_INPUTS 在点运行时、任何节点执行前被调用：路径不对要当场拦下，并说清是哪一个。"""
    cls: Any = nodes.GAReferenceImages
    ok = tmp_path / "ok.png"
    Image.new("RGB", (2, 2)).save(ok)
    assert cls.VALIDATE_INPUTS(paths="") is True
    assert cls.VALIDATE_INPUTS(paths=f"{ok}\n# {tmp_path / 'ignored.png'}\n") is True
    msg = cls.VALIDATE_INPUTS(paths=f"{ok}\n{tmp_path / 'nope.png'}\n{tmp_path}")
    assert isinstance(msg, str) and "参考图" in msg and "paths" in msg
    assert f"找不到文件：{tmp_path / 'nope.png'}" in msg and f"不是文件：{tmp_path}" in msg
    assert str(ok) not in msg
    assert cls.VALIDATE_INPUTS() is True  # paths 被连线接管时不会传进来


def test_reference_images_is_changed_follows_files(tmp_path: pathlib.Path) -> None:
    a = tmp_path / "a.png"
    Image.new("RGB", (2, 2)).save(a)
    cls: Any = nodes.GAReferenceImages
    first = cls.IS_CHANGED(paths=str(a))
    assert first == cls.IS_CHANGED(paths=str(a))  # 文件没变可复用
    Image.new("RGB", (9, 9), (1, 1, 1)).save(a)
    assert cls.IS_CHANGED(paths=str(a)) != first  # 同一路径换了内容要重读
    assert cls.IS_CHANGED(paths="") == "" and "missing" in cls.IS_CHANGED(paths=str(tmp_path / "x.png"))


def test_edit_node_passes_refs_and_mask(router: FakeRouter) -> None:
    img = torch.zeros(1, 8, 8, 3)
    mask = torch.ones(1, 8, 8)  # 全部重画
    _image, _, meta = nodes.GACloudEdit().run(images=img, prompt="改", negative="", mask=mask)
    kind, profile, req, _ = router.calls[0]
    assert kind == "edit" and profile == "image_edit" and len(req.refs) == 1
    assert req.mask.mode == "RGBA" and req.mask.getpixel((0, 0))[3] == 0  # alpha = 1 - mask
    assert meta["provider"] == "fake"


def test_edit_node_accepts_refs_and_requires_an_image(router: FakeRouter) -> None:
    refs = [Image.new("RGBA", (6, 4)), Image.new("RGBA", (2, 2))]
    nodes.GACloudEdit().run(prompt="改", refs=refs)
    assert [r.size for r in router.calls[0][2].refs] == [(6, 4), (2, 2)]
    # 两个都接：images 里的原图排最前（蒙版按它的尺寸）
    nodes.GACloudEdit().run(prompt="改", refs=refs, images=torch.zeros(1, 8, 8, 3), mask=torch.ones(1, 8, 8))
    req = router.calls[1][2]
    assert [r.size for r in req.refs] == [(8, 8), (6, 4), (2, 2)] and req.mask.size == (8, 8)
    with pytest.raises(RuntimeError, match="至少需要一张图"):
        nodes.GACloudEdit().run(prompt="改")
    with pytest.raises(RuntimeError, match="至少需要一张图"):
        nodes.GACloudEdit().run(prompt="改", refs=[])


def test_prompt_expand_meta(router: FakeRouter) -> None:
    style = StyleCard(name="s", description="d", positive="国潮")
    prompt, negative, meta = nodes.GAPromptExpand().run(brief="龙", asset_type="icon", style=style, tag="tt")
    assert "国潮" in prompt and negative == "n"
    assert meta["brief"] == "龙" and meta["prompt"] == prompt and meta["negative"] == negative
    assert meta["text_model"] == "fake:txt" and meta["tag"] == "tt"
    assert router.calls[0][3] == "tt"


def test_reference_analyze_and_qc(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRouter(text="好看的描述")
    monkeypatch.setattr(common, "get_router", lambda: r)
    (desc,) = nodes.GAReferenceAnalyze().run(images=torch.zeros(2, 8, 8, 3))
    assert desc == "好看的描述" and len(r.calls[0][2][1].images) == 2
    nodes.GAReferenceAnalyze().run(refs=[Image.new("RGBA", (4, 4))], images=torch.zeros(1, 8, 8, 3))
    assert [im.size for im in r.calls[1][2][1].images] == [(8, 8), (4, 4)]
    with pytest.raises(RuntimeError, match="至少需要一张图片"):
        nodes.GAReferenceAnalyze().run()
    r.text = '{"score":4,"issues":["糊"]}'
    score, report = nodes.GAQCScore().run(image=torch.zeros(1, 8, 8, 3), brief="龙", threshold=6)
    assert score == 4.0 and "糊" in report and "未通过" in report


def test_remove_background_none() -> None:
    image, mask = nodes.GARemoveBackground().run(image=torch.ones(1, 4, 4, 3), method="none")
    assert image.shape == (1, 4, 4, 3) and mask.shape == (1, 4, 4) and mask.sum().item() == 0


def test_compose_template(tmp_path: pathlib.Path) -> None:
    lay = tmp_path / "l.yaml"
    lay.write_text("size: [16, 16]\nlayers:\n  - {type: image, src: 'slot:art', box: [0, 0, 8, 8]}\n",
                   encoding="utf-8")
    art = torch.zeros(1, 4, 4, 3)
    image, mask = nodes.GAComposeTemplate().run(layout_path=str(lay), art=art)
    assert tuple(image.shape) == (1, 16, 16, 3) and mask[0, 0, 0].item() == 0 and mask[0, 15, 15].item() == 1
    with pytest.raises(RuntimeError, match=r"slot|art|缺"):
        nodes.GAComposeTemplate().run(layout_path=str(lay))
    with pytest.raises(RuntimeError, match="vars_json"):
        nodes.GAComposeTemplate().run(layout_path=str(lay), vars_json="{bad", art=art)


@pytest.fixture
def out_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    d = tmp_path / "out"
    d.mkdir()
    fp = types.ModuleType("folder_paths")
    monkeypatch.setattr(fp, "get_output_directory", lambda: str(d), raising=False)
    monkeypatch.setitem(sys.modules, "folder_paths", fp)
    return d


def test_save_asset_writes_png_and_sidecar(out_dir: pathlib.Path) -> None:
    img = torch.zeros(2, 4, 4, 3)
    mask = torch.zeros(2, 4, 4)
    mask[:, 0, 0] = 1
    res = nodes.GASaveAsset().save(image=img, subfolder="icons", name="dragon", mask=mask,
                                   meta={"prompt": "龙", "provider": "fake"})
    assert res["ui"]["images"] == [
        {"filename": "dragon_00001.png", "subfolder": "icons", "type": "output"},
        {"filename": "dragon_00002.png", "subfolder": "icons", "type": "output"}]
    png = Image.open(out_dir / "icons" / "dragon_00001.png")
    assert png.mode == "RGBA" and _px(png, (0, 0))[3] == 0 and _px(png, (1, 1))[3] == 255
    side = json.loads((out_dir / "icons" / "dragon_00001.json").read_text(encoding="utf-8"))
    assert side["prompt"] == "龙" and side["provider"] == "fake" and side["saved_at"]
    # 再存一次接着编号
    res = nodes.GASaveAsset().save(image=img[:1], subfolder="icons", name="dragon")
    assert res["ui"]["images"][0]["filename"] == "dragon_00003.png"


def test_save_asset_clean_alpha(out_dir: pathlib.Path) -> None:
    # 9x9：中间一列实体，右边紧贴一列抗锯齿边，最右一列是远离主体的淡阴影
    im = Image.new("RGBA", (9, 9), (0, 0, 0, 0))
    for y in range(9):
        im.putpixel((2, y), (10, 20, 30, 255))
        im.putpixel((3, y), (10, 20, 30, 40))
        im.putpixel((8, y), (10, 20, 30, 40))
    img, mask = pil_to_tensors([im])
    nodes.GASaveAsset().save(image=img, mask=mask, subfolder="c", name="keep")
    nodes.GASaveAsset().save(image=img, mask=mask, subfolder="c", name="clean", clean_alpha=96, meta={"a": 1})
    keep, clean = Image.open(out_dir / "c" / "keep_00001.png"), Image.open(out_dir / "c" / "clean_00001.png")
    assert _px(keep, (8, 4))[3] == 40  # 默认 0：原样保存
    assert _px(clean, (8, 4))[3] == 0 and _px(clean, (3, 4))[3] == 40 and _px(clean, (2, 4))[3] == 255
    assert json.loads((out_dir / "c" / "clean_00001.json").read_text(encoding="utf-8"))["clean_alpha"] == 96
    assert "clean_alpha" not in json.loads((out_dir / "c" / "keep_00001.json").read_text(encoding="utf-8"))
    # 没接 mask 的不透明图不受影响
    nodes.GASaveAsset().save(image=img, subfolder="c", name="opaque", clean_alpha=200)
    assert Image.open(out_dir / "c" / "opaque_00001.png").getchannel("A").getextrema() == (255, 255)


def test_save_asset_fills_first_free_number(out_dir: pathlib.Path) -> None:
    (out_dir / "a_00001.png").write_bytes(b"x")
    (out_dir / "a_00003.json").write_text("{}")
    res = nodes.GASaveAsset().save(image=torch.zeros(1, 4, 4, 3), subfolder="", name="a")
    assert res["ui"]["images"][0]["filename"] == "a_00002.png"


@pytest.mark.parametrize("sub", ["../x", "a/../../b", "/abs", "a/..", "C:\\x\\..\\y"])
def test_save_asset_rejects_bad_subfolder(out_dir: pathlib.Path, sub: str) -> None:
    with pytest.raises(RuntimeError, match="子目录"):
        nodes.GASaveAsset().save(image=torch.zeros(1, 4, 4, 3), subfolder=sub, name="a")


def test_save_asset_rejects_bad_name(out_dir: pathlib.Path) -> None:
    with pytest.raises(RuntimeError):
        nodes.GASaveAsset().save(image=torch.zeros(1, 4, 4, 3), subfolder="", name="../evil")


def test_ga_error_becomes_runtime_error_with_message(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRouter(fail=UpstreamError("all_targets_failed", "所有目标都失败了"))
    monkeypatch.setattr(common, "get_router", lambda: r)
    with pytest.raises(RuntimeError, match="所有目标都失败了") as ei:
        nodes.GACloudGenerate().run(prompt="x", negative="")
    assert isinstance(ei.value.__cause__, UpstreamError)


def test_bad_size_is_runtime_error(router: FakeRouter) -> None:
    with pytest.raises(RuntimeError, match="尺寸"):
        nodes.GACloudGenerate().run(prompt="x", negative="", size="abc")


def test_style_card_node(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text("name: n\ndescription: d\n", encoding="utf-8")
    (style,) = nodes.GAStyleCard().run(path=str(p))
    assert style.name == "n"
    with pytest.raises(RuntimeError, match="风格卡"):
        nodes.GAStyleCard().run(path=str(tmp_path / "nope.yaml"))


def test_router_cache_reloads_on_mtime_change(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = write_cfg(tmp_path)
    monkeypatch.setenv("GAME_ART_CONFIG", str(cfg))
    monkeypatch.setattr(common, "_STATE", {"key": None, "router": None})
    r1 = common.get_router()
    assert common.get_router() is r1
    import os
    st = cfg.stat()
    os.utime(cfg, (st.st_atime, st.st_mtime + 10))
    assert common.get_router() is not r1


def test_router_cache_reloads_on_dotenv_mtime_change(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    cfg = write_cfg(tmp_path)
    env = tmp_path / ".env"
    env.write_text("COMFYUI_LLM_BASE_KEY=a\n")
    monkeypatch.setenv("GAME_ART_CONFIG", str(cfg))
    monkeypatch.setattr(common, "_STATE", {"key": None, "router": None})
    r1 = common.get_router()
    assert common.get_router() is r1
    st = env.stat()
    os.utime(env, (st.st_atime, st.st_mtime + 10))
    assert common.get_router() is not r1


def test_seed_max_is_js_safe_integer() -> None:
    seed = nodes.GACloudGenerate.INPUT_TYPES()["required"]["seed"]
    assert seed[1]["max"] == 2**53 - 1


def test_remove_background_none_passes_incoming_alpha() -> None:
    mask = torch.zeros(1, 4, 4)
    mask[0, 0, 0] = 1.0
    _image, out_mask = nodes.GARemoveBackground().run(image=torch.ones(1, 4, 4, 3), method="none", mask=mask)
    assert out_mask[0, 0, 0].item() == pytest.approx(1.0) and out_mask[0, 1, 1].item() == 0


def test_remove_background_rembg_receives_composited_rgba(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_remove(im: Image.Image, method: str) -> Image.Image:
        seen.append(im.mode)
        return im.convert("RGBA")

    monkeypatch.setattr(imaging, "remove_background", fake_remove)
    mask = torch.zeros(1, 4, 4)
    mask[0, 0, 0] = 1.0
    _, out_mask = nodes.GARemoveBackground().run(image=torch.ones(1, 4, 4, 3), method="rembg", mask=mask)
    assert seen == ["RGBA"] and out_mask[0, 0, 0].item() == pytest.approx(1.0)


def test_chain_generate_bg_save_keeps_alpha(monkeypatch: pytest.MonkeyPatch, out_dir: pathlib.Path) -> None:
    """gen.0/gen.1 → bg(image, mask) → save(bg.0, bg.1)：上游透明底一路保留到 PNG。"""
    r = FakeRouter()

    def transparent_images(req: ImageRequest) -> ImageResult:
        im = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        im.putpixel((4, 4), (255, 0, 0, 255))
        return ImageResult(images=[im], partial=False, seed=None, raw={})

    monkeypatch.setattr(r, "_images", transparent_images)
    monkeypatch.setattr(common, "get_router", lambda: r)
    image, mask, meta = nodes.GACloudGenerate().run(prompt="x", negative="", size="8x8", transparent=True)
    bg_img, bg_mask = nodes.GARemoveBackground().run(image=image, method="none", mask=mask)
    res = nodes.GASaveAsset().save(image=bg_img, subfolder="c", name="a", mask=bg_mask, meta=meta)
    png = Image.open(out_dir / "c" / res["ui"]["images"][0]["filename"])
    assert png.mode == "RGBA" and _px(png, (0, 0))[3] == 0 and _px(png, (4, 4))[3] == 255


def test_cloud_nodes_is_changed() -> None:
    import math
    gen = nodes.GACloudGenerate.IS_CHANGED
    assert math.isnan(gen(prompt="x", negative="", seed=-1))
    assert gen(prompt="x", negative="", seed=42) == 42
    for cls in (nodes.GACloudEdit, nodes.GAPromptExpand, nodes.GAReferenceAnalyze, nodes.GAQCScore):
        assert math.isnan(cls.IS_CHANGED(prompt="x", brief="b")), cls.__name__
    # 不调用云端的节点不加 IS_CHANGED，保持 ComfyUI 默认缓存
    assert not hasattr(nodes.GAComposeTemplate, "IS_CHANGED")


def test_every_input_and_output_has_a_chinese_tooltip() -> None:
    for name, node_cls in nodes.NODE_CLASS_MAPPINGS.items():
        cls: Any = node_cls
        assert len(cls.DESCRIPTION) > 10, name
        if cls.RETURN_TYPES:
            assert len(cls.OUTPUT_TOOLTIPS) == len(cls.RETURN_TYPES), name
        it = cls.INPUT_TYPES()
        for section in ("required", "optional"):
            for key, spec in it.get(section, {}).items():
                assert len(spec) == 2 and len(spec[1].get("tooltip", "")) >= 4, f"{name}.{key} 缺少说明"


def test_choices_are_dropdowns() -> None:
    expand = nodes.GAPromptExpand.INPUT_TYPES()["required"]
    assert {"icon", "prop", "background"} <= set(expand["asset_type"][0])
    # 模型下拉：模型组名 + 配置里出现过的 provider:model（测试环境用的是仓库示例配置）
    models = expand["profile"][0]
    assert {"text", "vision", "image", "aihubmix:gpt-4o-mini", "aihubmix:gpt-image-1"} <= set(models)
    assert expand["profile"][1]["default"] == "text"
    gen = nodes.GACloudGenerate.INPUT_TYPES()
    assert gen["required"]["profile"][1]["default"] == "image"
    assert isinstance(nodes.GARemoveBackground.INPUT_TYPES()["required"]["method"][0], list)
    # 提示词是带名字的接线口，画布上看得到 prompt / negative
    assert gen["required"]["prompt"][1]["forceInput"] and gen["required"]["negative"][1]["forceInput"]


def test_model_dropdown_survives_broken_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("providers: [oops", encoding="utf-8")
    monkeypatch.setenv("GAME_ART_CONFIG", str(bad))
    assert nodes.GAPromptExpand.INPUT_TYPES()["required"]["profile"][0] == ["text"]


def test_style_card_node_accepts_inline_edit(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("name: n\ndescription: d\npositive: 旧\n", encoding="utf-8")
    assert nodes.GAStyleCard().run(path=str(p), edit="positive: 新")[0].positive == "新"
    assert nodes.GAStyleCard().run(path="", edit="name: a\ndescription: b")[0].name == "a"
    with pytest.raises(RuntimeError, match="风格卡为空"):
        nodes.GAStyleCard().run(path=" ", edit="")


def test_style_card_node_dropped_file_marker(tmp_path: Path) -> None:
    """拖进来的风格卡：path 只是显示用的「拖入:文件名」，内容全在编辑框里，不去读文件。"""
    (card,) = nodes.GAStyleCard().run(path=" 拖入:my_card.yaml ", edit="name: 拖\ndescription: d\npositive: p\n")
    assert card.name == "拖" and card.positive == "p"
    with pytest.raises(RuntimeError, match=r"my_card\.yaml.*重新拖"):
        nodes.GAStyleCard().run(path="拖入:my_card.yaml", edit="  ")


def test_style_card_text_route_helper(tmp_path: pathlib.Path) -> None:
    from comfy_nodes import WEB_DIRECTORY, routes

    text = routes.style_card_text("@repo/examples/style_cards/guochao.yaml")
    assert "国潮插画风" in text and "palette" in text
    # 不是通用读文件口：只读合法的 .yaml 风格卡
    secret = tmp_path / "secret.txt"
    secret.write_text("name: a\ndescription: b\n", encoding="utf-8")
    other = tmp_path / "other.yaml"
    other.write_text("just: something\n", encoding="utf-8")
    for bad in ("", str(secret), str(other), str(tmp_path / "nope.yaml")):
        with pytest.raises(Exception) as e:
            routes.style_card_text(bad)
        assert getattr(e.value, "code", "") == "bad_style_card"
    assert routes.register() is False  # 不在 ComfyUI 里时不注册
    assert (pathlib.Path(routes.__file__).parent / WEB_DIRECTORY / "style_card.js").is_file()


def test_size_is_a_dropdown_from_config(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for cls in (nodes.GACloudGenerate, nodes.GACloudEdit):
        choices, opts = cls.INPUT_TYPES()["required"]["size"]
        assert choices == ["1024x1024", "1536x1024", "1024x1536"] and opts["default"] == "1024x1024"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"comfy_root: {tmp_path}\nimage_sizes: ['512x512', '768x512']\n", encoding="utf-8")
    monkeypatch.setenv("GAME_ART_CONFIG", str(cfg))
    choices, opts = nodes.GACloudGenerate.INPUT_TYPES()["required"]["size"]
    assert choices == ["512x512", "768x512"] and opts["default"] == "512x512"
    cfg.write_text("providers: [oops", encoding="utf-8")  # 配置坏了也要能加载节点
    assert nodes.GACloudGenerate.INPUT_TYPES()["required"]["size"][0] == ["1024x1024"]


def test_model_dropdown_includes_upstream_models(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import responses

    from game_art import models

    models._MEM.clear()
    monkeypatch.setenv("GAME_ART_CONFIG", str(write_cfg(tmp_path, data_dir=str(tmp_path / "data"))))
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "k")
    with responses.RequestsMock() as rsps:
        rsps.get("https://example.test/v1/models", json={"data": [{"id": "chat-x"}, {"id": "flux-y"}]})
        choices = nodes.GAPromptExpand.INPUT_TYPES()["required"]["profile"][0]
    models._MEM.clear()
    assert choices[:2] == ["image", "text"] and {"aihubmix:chat-x", "aihubmix:flux-y"} <= set(choices)
