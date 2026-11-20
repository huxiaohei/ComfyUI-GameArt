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
       "GARemoveBackground", "GAQCScore", "GAQCReview", "GAComposeTemplate", "GASaveAsset"}


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
    score, report = nodes.GAQCScore().run(image=torch.zeros(1, 8, 8, 3), brief="龙")
    assert score == 4.0 and "糊" in report and "未通过" in report


class SeqRouter(FakeRouter):
    """按顺序返回预设的回答（最后一个重复用）。"""
    def __init__(self, *texts: str) -> None:
        super().__init__()
        self.texts = list(texts)

    def chat(self, profile: str, messages: list[Message], *, tag: str,
             json_mode: bool = False) -> tuple[ChatResult, Target]:
        self.text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return super().chat(profile, messages, tag=tag, json_mode=json_mode)


@pytest.fixture
def temp_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    d = tmp_path / "temp"
    fp = types.ModuleType("folder_paths")
    monkeypatch.setattr(fp, "get_temp_directory", lambda: str(d), raising=False)
    monkeypatch.setitem(sys.modules, "folder_paths", fp)
    return d


def test_prompt_expand_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRouter()
    monkeypatch.setattr(common, "get_router", lambda: r)
    spec = nodes.GAPromptExpand.INPUT_TYPES()["optional"]["feedback"]
    assert spec[0] == "STRING" and spec[1]["multiline"] and spec[1]["default"] == ""
    *_, meta = nodes.GAPromptExpand().run(brief="貂蝉", asset_type="character", feedback="- 头顶贴边\n\n步摇不精致")
    assert meta["feedback"] == ["头顶贴边", "步摇不精致"]
    assert "1. 头顶贴边" in r.calls[0][2][0].text
    *_, meta = nodes.GAPromptExpand().run(brief="貂蝉", asset_type="character", feedback=" \n")
    assert "feedback" not in meta  # 没有修订意见时不往记录里写


def test_qc_review_single_image(monkeypatch: pytest.MonkeyPatch, temp_dir: pathlib.Path) -> None:
    r = SeqRouter('{"score":6,"issues":[{"text":"头顶贴边","level":"must"},{"text":"袖口再细些","level":"suggest"}]}')
    monkeypatch.setattr(common, "get_router", lambda: r)
    image, mask = torch.rand(1, 8, 6, 3), torch.zeros(1, 8, 6)
    out = nodes.GAQCReview().run(image=image, mask=mask, meta={"brief": "貂蝉", "tag": "b/1", "prompt": "p"})
    got_image, got_mask, meta, report = out["result"]
    assert got_image is image and got_mask is mask  # 图片原样往下传
    assert meta["prompt"] == "p" and meta["qc"][0]["score"] == 6
    assert meta["qc"][0]["issues"] == ["头顶贴边", "袖口再细些"]
    assert "未通过" in report and "[必须改] 头顶贴边" in report and "[建议] 袖口再细些" in report
    (ui,) = out["ui"]["ga_qc"]
    assert ui["threshold"] == 7.0 and ui["common"] == [] and len(ui["results"]) == 1
    res = ui["results"][0]
    assert res["passed"] is False and res["issues"] == [{"text": "头顶贴边", "level": "must"},
                                                        {"text": "袖口再细些", "level": "suggest"}]
    thumb = res["thumb"]
    assert thumb["type"] == "temp" and (temp_dir / thumb["subfolder"] / thumb["filename"]).is_file()
    # 只有一张图：不调用归并；描述取自 meta，记账标签沿用 meta 里的
    assert [(c[0], c[1], c[3]) for c in r.calls] == [("chat", "vision", "b/1")]
    assert "貂蝉" in r.calls[0][2][0].text


def test_qc_review_batch_finds_common_issues(monkeypatch: pytest.MonkeyPatch, temp_dir: pathlib.Path) -> None:
    r = SeqRouter('{"score":8,"issues":["脚下没留白"]}', '{"score":5,"issues":["脚贴底边","步摇糊"]}',
                  '{"score":9,"issues":[]}', '{"common":[{"text":"脚下留白不足","images":[1,2]}]}')
    monkeypatch.setattr(common, "get_router", lambda: r)
    out = nodes.GAQCReview().run(image=torch.rand(3, 8, 8, 3), brief="自己写的描述", threshold=6.0,
                                 meta={"brief": "meta 里的描述"})
    _image, mask, meta, report = out["result"]
    assert mask.shape == (3, 8, 8) and mask.sum().item() == 0  # 没接 mask：全不透明
    (ui,) = out["ui"]["ga_qc"]
    assert [x["score"] for x in ui["results"]] == [8, 5, 9]
    assert [x["passed"] for x in ui["results"]] == [True, False, True]
    assert ui["common"] == [{"text": "脚下留白不足", "count": 2}] and ui["note"] == ""
    assert "共性问题" in report and "脚下留白不足（2/3 张）" in report
    assert len({x["thumb"]["filename"] for x in ui["results"]}) == 3
    # brief 写了就用它；三次看图 + 一次归并
    assert [c[1] for c in r.calls] == ["vision", "vision", "vision", "text"]
    assert "自己写的描述" in r.calls[0][2][0].text and "meta 里的描述" not in r.calls[0][2][0].text
    assert len(meta["qc"]) == 3


def test_qc_review_survives_failed_merge(monkeypatch: pytest.MonkeyPatch, temp_dir: pathlib.Path) -> None:
    """归并共性问题失败不该让整个节点失败：每张图自己的结果照常给出，并在报告里说明。"""
    r = SeqRouter('{"score":5,"issues":["糊"]}', '{"score":5,"issues":["糊"]}', "不是 JSON")
    monkeypatch.setattr(common, "get_router", lambda: r)
    out = nodes.GAQCReview().run(image=torch.rand(2, 8, 8, 3), brief="龙")
    (ui,) = out["ui"]["ga_qc"]
    assert ui["common"] == [] and "没能归并出共性问题" in ui["note"] and "没能归并出共性问题" in out["result"][3]
    assert len(ui["results"]) == 2


def test_qc_review_needs_brief(monkeypatch: pytest.MonkeyPatch, temp_dir: pathlib.Path) -> None:
    monkeypatch.setattr(common, "get_router", FakeRouter)
    with pytest.raises(RuntimeError, match="原本想要什么"):
        nodes.GAQCReview().run(image=torch.rand(1, 8, 8, 3), meta={"prompt": "p"})


def test_save_asset_splits_qc_per_image(out_dir: pathlib.Path) -> None:
    """「质检结果」给的是整批的质检列表，每张图旁边的记录里只留它自己那一条。"""
    meta = {"prompt": "p", "qc": [{"score": 8}, {"score": 5}]}
    nodes.GASaveAsset().save(image=torch.zeros(2, 4, 4, 3), subfolder="q", name="a", meta=meta)
    got = [json.loads((out_dir / "q" / f"a_0000{i}.json").read_text())["qc"] for i in (1, 2)]
    assert got == [{"score": 8}, {"score": 5}]
    # 张数对不上（比如中间换过图）：不猜，原样保留
    nodes.GASaveAsset().save(image=torch.zeros(1, 4, 4, 3), subfolder="q", name="b", meta=meta)
    assert json.loads((out_dir / "q" / "b_00001.json").read_text())["qc"] == meta["qc"]


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


def test_save_asset_to_absolute_folder(out_dir: pathlib.Path, tmp_path: pathlib.Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """subfolder 写完整路径：直接存到那个文件夹；编辑器预览用临时目录里的副本。"""
    temp = tmp_path / "temp"
    monkeypatch.setattr(sys.modules["folder_paths"], "get_temp_directory", lambda: str(temp), raising=False)
    dest = tmp_path / "project" / "art"  # 不存在：自动创建
    dest.mkdir(parents=True)
    (dest / "hero_00001.png").write_bytes(b"old")  # 已有同名文件：换下一个编号，不覆盖
    res = nodes.GASaveAsset().save(image=torch.ones(2, 4, 4, 3), subfolder=f" '{dest}' ", name="hero",
                                   meta={"prompt": "p"})
    assert (dest / "hero_00001.png").read_bytes() == b"old"
    assert sorted(f.name for f in dest.iterdir()) == [
        "hero_00001.png", "hero_00002.json", "hero_00002.png", "hero_00003.json", "hero_00003.png"]
    assert json.loads((dest / "hero_00002.json").read_text(encoding="utf-8"))["prompt"] == "p"
    assert not any(out_dir.iterdir())  # 输出目录里不留副本
    shown = res["ui"]["images"]
    assert [i["type"] for i in shown] == ["temp", "temp"] and len({i["filename"] for i in shown}) == 2
    for item, real in zip(shown, ("hero_00002.png", "hero_00003.png"), strict=True):
        assert item["filename"].endswith(f"_{real}")
        assert (temp / item["subfolder"] / item["filename"]).read_bytes() == (dest / real).read_bytes()
    # 文件夹还不存在：自动创建
    new_dir = tmp_path / "made" / "later"
    nodes.GASaveAsset().save(image=torch.ones(1, 4, 4, 3), subfolder=str(new_dir), name="n")
    assert (new_dir / "n_00001.png").is_file()
    # 完整路径其实在输出目录里面：按相对路径处理，预览直接用原文件
    res = nodes.GASaveAsset().save(image=torch.ones(1, 4, 4, 3), subfolder=str(out_dir / "in" / "side"), name="a")
    assert res["ui"]["images"] == [{"filename": "a_00001.png", "subfolder": "in/side", "type": "output"}]
    with pytest.raises(RuntimeError, match="不是一个文件夹"):
        nodes.GASaveAsset().save(image=torch.ones(1, 4, 4, 3), subfolder=str(dest / "hero_00001.png"), name="x")
    with pytest.raises(RuntimeError, match="无法创建保存位置"):
        nodes.GASaveAsset().save(image=torch.ones(1, 4, 4, 3), subfolder=str(dest / "hero_00001.png" / "sub"),
                                 name="x")


def test_save_asset_validates_before_run(tmp_path: pathlib.Path) -> None:
    """VALIDATE_INPUTS 在任何节点执行前调用：保存位置不对要当场拦下，不能等图出完才报错。"""
    cls: Any = nodes.GASaveAsset
    assert cls.VALIDATE_INPUTS(subfolder="ga/x", name="asset") is True
    assert cls.VALIDATE_INPUTS(subfolder=str(tmp_path / "anywhere"), name="asset") is True  # 完整路径可以
    assert cls.VALIDATE_INPUTS(subfolder="~/art") is True and cls.VALIDATE_INPUTS() is True
    (tmp_path / "f.txt").write_text("x")
    for kwargs, word in (({"subfolder": "a/../b"}, "保存位置"), ({"subfolder": "../x"}, "保存位置"),
                         ({"subfolder": str(tmp_path / "f.txt")}, "不是一个文件夹"), ({"name": "a/b"}, "名称")):
        msg = cls.VALIDATE_INPUTS(**kwargs)
        assert isinstance(msg, str) and "保存资产" in msg and word in msg, kwargs


def test_choose_folder_helpers() -> None:
    from comfy_nodes import routes
    mac = routes.choose_folder_command('选 "这个"', platform="darwin")
    assert mac[0] == "osascript" and mac[-1] == 'POSIX path of (choose folder with prompt "选 \\"这个\\"")'
    other = routes.choose_folder_command("选", platform="linux")
    assert other[0] == sys.executable and other[-1] == "选" and "askdirectory" in other[2]
    assert routes.parse_chosen_folder(0, "/Users/me/art/\n") == "/Users/me/art"
    assert routes.parse_chosen_folder(0, "/\n") == "/"
    assert routes.parse_chosen_folder(1, "") == "" and routes.parse_chosen_folder(0, "  \n") == ""
    assert routes.parse_chosen_folder(1, "/tmp/x") == ""  # 退出码非 0 一律当作没选
    # 选中的文件夹 → subfolder 的值：输出目录里面的写相对路径，外面的写完整路径
    assert routes.as_save_location("/out/ga/icons", "/out") == "ga/icons"
    assert routes.as_save_location("/out", "/out") == ""
    assert routes.as_save_location("/Users/me/art", "/out") == "/Users/me/art"
    assert routes.as_save_location("/outside", "/out") == "/outside"  # 名字相近不算在里面
    assert routes.as_save_location("", "/out") == "" and routes.as_save_location("/x", None) == "/x"


def test_choose_folder_runs_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from comfy_nodes import routes
    # 用一条必定可用的命令代替系统选择窗口
    monkeypatch.setattr(routes, "choose_folder_command",
                        lambda prompt: [sys.executable, "-c", "print('/picked/dir/')"])
    assert asyncio.run(routes.choose_folder()) == "/picked/dir"
    cancel = "import sys; sys.stderr.write('User canceled. (-128)'); sys.exit(1)"  # osascript 点取消时的表现
    monkeypatch.setattr(routes, "choose_folder_command", lambda prompt: [sys.executable, "-c", cancel])
    assert asyncio.run(routes.choose_folder()) == ""
    monkeypatch.setattr(routes, "choose_folder_command",
                        lambda prompt: [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"])
    with pytest.raises(Exception, match="boom"):
        asyncio.run(routes.choose_folder())
    monkeypatch.setattr(routes, "choose_folder_command", lambda prompt: ["/no/such/program-xyz"])
    with pytest.raises(Exception, match="无法弹出"):
        asyncio.run(routes.choose_folder())


def test_save_asset_fills_first_free_number(out_dir: pathlib.Path) -> None:
    (out_dir / "a_00001.png").write_bytes(b"x")
    (out_dir / "a_00003.json").write_text("{}")
    res = nodes.GASaveAsset().save(image=torch.zeros(1, 4, 4, 3), subfolder="", name="a")
    assert res["ui"]["images"][0]["filename"] == "a_00002.png"


@pytest.mark.parametrize("sub", ["../x", "a/../../b", "a/..", "C:\\x\\..\\y"])
def test_save_asset_rejects_bad_subfolder(out_dir: pathlib.Path, sub: str) -> None:
    with pytest.raises(RuntimeError, match="保存位置"):
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
