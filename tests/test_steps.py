from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from game_art.config import Target
from game_art.errors import InputError, UpstreamError
from game_art.providers.base import ChatResult, Message
from game_art.steps import analyze_reference, expand_prompt, qc_score
from game_art.style import StyleCard, load_style_card

ROOT = Path(__file__).resolve().parent.parent


class FakeRouter:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[dict[str, Any]] = []

    def chat(self, profile: str, messages: list[Message], *, tag: str,
             json_mode: bool = False) -> tuple[ChatResult, Target]:
        self.calls.append({"profile": profile, "messages": messages, "tag": tag, "json_mode": json_mode})
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return ChatResult(text=text, input_tokens=1, output_tokens=1, raw={}), Target(provider="p", model="m")


def _style(**kw: Any) -> StyleCard:
    return StyleCard(name="s", description="d", **kw)


def test_expand_merges_style_negative() -> None:
    router = FakeRouter('{"prompt":"a","negative":"b"}')
    r = expand_prompt(router, "龙", asset_type="icon",
                      style=_style(negative="模糊", positive="国潮"), tag="t")
    assert r.prompt.startswith("a") and "国潮" in r.prompt
    assert "模糊" in r.negative and r.negative.startswith("b")
    assert router.calls[0]["json_mode"] is True


def test_expand_does_not_duplicate_style() -> None:
    router = FakeRouter('{"prompt":"a, 国潮","negative":"模糊"}')
    r = expand_prompt(router, "龙", asset_type="icon",
                      style=_style(negative="模糊", positive="国潮"), tag="t")
    assert r.prompt == "a, 国潮" and r.negative == "模糊"


def test_expand_tolerates_json_fence() -> None:
    router = FakeRouter('```json\n{"prompt":"a","negative":"b"}\n```')
    r = expand_prompt(router, "龙", asset_type="icon", style=None, tag="t")
    assert r.prompt == "a" and len(router.calls) == 1


def test_expand_retries_once_on_bad_json() -> None:
    router = FakeRouter("xx", '{"prompt":"a","negative":"b"}')
    r = expand_prompt(router, "龙", asset_type="icon", style=None, tag="t")
    assert r.prompt == "a" and len(router.calls) == 2


def test_expand_raises_after_two_bad_json() -> None:
    router = FakeRouter("xx", "yy")
    with pytest.raises(UpstreamError) as e:
        expand_prompt(router, "龙", asset_type="icon", style=None, tag="t")
    assert e.value.code == "bad_llm_json" and len(router.calls) == 2


def test_analyze_sends_images() -> None:
    router = FakeRouter("暖色调")
    imgs = [Image.new("RGB", (4, 4)), Image.new("RGB", (4, 4))]
    assert analyze_reference(router, imgs, tag="t") == "暖色调"
    sent = [i for m in router.calls[0]["messages"] for i in m.images]
    assert len(sent) == 2


def test_analyze_empty_raises() -> None:
    with pytest.raises(InputError):
        analyze_reference(FakeRouter("x"), [], tag="t")


def test_qc_threshold() -> None:
    router = FakeRouter('{"score":5,"issues":["x"]}')
    r = qc_score(router, Image.new("RGB", (4, 4)), brief="龙", style=None, threshold=6, tag="t")
    assert r.passed is False and r.issues == ["x"] and r.score == 5


def test_qc_clamps_and_passes() -> None:
    r = qc_score(FakeRouter('{"score":15,"issues":[]}'), Image.new("RGB", (4, 4)),
                 brief="龙", style=_style(), tag="t")
    assert r.score == 10 and r.passed
    r = qc_score(FakeRouter('{"score":-3,"issues":[]}'), Image.new("RGB", (4, 4)),
                 brief="龙", style=None, tag="t")
    assert r.score == 0 and not r.passed


def test_style_card_relative_refs(tmp_path: Path) -> None:
    (tmp_path / "refs").mkdir()
    card = tmp_path / "c.yaml"
    card.write_text("name: n\ndescription: d\nreference_images: [refs/a.png]\n", encoding="utf-8")
    s = load_style_card(card)
    assert s.reference_images == [(tmp_path / "refs" / "a.png").resolve()]
    assert s.reference_images[0].is_absolute()


def test_style_card_bad_file(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        load_style_card(tmp_path / "nope.yaml")


def test_example_style_card_loads() -> None:
    s = load_style_card(ROOT / "examples" / "style_cards" / "guochao.yaml")
    assert s.name and s.palette and s.rules


@pytest.mark.parametrize("bad", ['{"prompt":null,"negative":"b"}', '{"prompt":123}',
                                 '{"prompt":["a"]}', '{"prompt":"a","negative":5}'])
def test_expand_rejects_non_string_fields(bad: str) -> None:
    router = FakeRouter(bad)
    with pytest.raises(UpstreamError) as e:
        expand_prompt(router, "龙", asset_type="icon", style=None, tag="t")
    assert e.value.code == "bad_llm_json" and len(router.calls) == 2


def test_expand_null_negative_ok() -> None:
    r = expand_prompt(FakeRouter('{"prompt":"a","negative":null}'), "龙",
                      asset_type="icon", style=None, tag="t")
    assert r.negative == ""


@pytest.mark.parametrize("bad", ['{"score":NaN,"issues":[]}', '{"score":Infinity,"issues":[]}',
                                 '{"score":true,"issues":[]}', '{"score":"8","issues":[]}',
                                 '{"score":8,"issues":"abc"}', '{"score":8,"issues":[1]}'])
def test_qc_rejects_garbage(bad: str) -> None:
    with pytest.raises(UpstreamError) as e:
        qc_score(FakeRouter(bad), Image.new("RGB", (4, 4)), brief="龙", style=None, tag="t")
    assert e.value.code == "bad_llm_json"


def test_qc_flattens_transparent_image_before_sending() -> None:
    router = FakeRouter('{"score":8,"issues":[]}')
    qc_score(router, Image.new("RGBA", (32, 32), (0, 0, 0, 0)), brief="龙", style=None, tag="t")
    sent = router.calls[0]["messages"][1].images[0]
    assert sent.mode == "RGB" and sent.getpixel((0, 0)) == (255, 255, 255)


def test_style_card_overrides_replace_file_fields(tmp_path: Path) -> None:
    card = tmp_path / "c.yaml"
    card.write_text("name: n\ndescription: d\npositive: 旧\nrules: [a]\n", encoding="utf-8")
    s = load_style_card(card, "positive: 新\npalette: ['#fff']\n")
    assert (s.name, s.positive, s.palette, s.rules) == ("n", "新", ["#fff"], ["a"])
    assert load_style_card(card, "  \n").positive == "旧"


def test_style_card_from_edit_text_only() -> None:
    s = load_style_card(None, "name: 水墨\ndescription: 黑白水墨\n")
    assert s.name == "水墨"
    for path, text in ((None, ""), (None, "positive: x"), (None, "- a\n- b"), (None, "a: [")):
        with pytest.raises(InputError) as e:
            load_style_card(path, text)
        assert e.value.code == "bad_style_card"


def test_asset_type_description_reaches_the_model() -> None:
    from game_art.steps import ASSET_TYPES, describe_asset_type

    assert describe_asset_type("prop").startswith("prop（道具")
    assert describe_asset_type("自定义类型") == "自定义类型"
    assert {"icon", "prop", "character", "background"} <= set(ASSET_TYPES)
    assert not any("麻将" in k + v for k, v in ASSET_TYPES.items())  # 通用流程，不为某个项目特设类型
    router = FakeRouter('{"prompt":"p","negative":"n"}')
    expand_prompt(router, "龙", asset_type="icon", style=None, tag="t")
    system = router.calls[0]["messages"][0].text
    assert "icon（图标" in system and "10%" in system


def test_style_card_blank_fields_mean_not_set(tmp_path: Path) -> None:
    # 只写字段名不写值（"palette:"）= 这项不设置；写在编辑框里时还会清掉文件里的同名字段
    card = tmp_path / "c.yaml"
    card.write_text("name: n\ndescription: d\npalette: ['#fff']\npositive: 旧\n", encoding="utf-8")
    edit = "name: 日本大富豪角色\ndescription: 日式二次元风格\npalette:\npositive: 少女风\nrules:\n  - 色彩鲜明\n"
    s = load_style_card(card, edit)
    assert (s.name, s.palette, s.positive, s.rules, s.negative) == ("日本大富豪角色", [], "少女风", ["色彩鲜明"], "")
    assert "色板" not in s.to_prompt_text()
    assert load_style_card(None, "name: a\ndescription: b\nrules:\nnegative:\n").rules == []
    with pytest.raises(InputError) as e:
        load_style_card(card, "name:\n")
    assert "name" in e.value.message and "直接编辑的内容" in e.value.message
    with pytest.raises(InputError) as e2:
        load_style_card(card, "palette: 红色\n")
    assert "palette" in e2.value.message and "列表" in e2.value.message
