from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from game_art.config import Target
from game_art.errors import InputError, UpstreamError
from game_art.providers.base import ChatResult, Message
from game_art.steps import analyze_reference, common_issues, expand_prompt, feedback_lines, qc_score
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


def test_style_card_rejects_reference_images(tmp_path: Path) -> None:
    """参考图改由「参考图」节点提供；旧风格卡里还留着这个字段时要明确报错，而不是悄悄忽略。"""
    card = tmp_path / "c.yaml"
    card.write_text("name: n\ndescription: d\nreference_images: [refs/a.png]\n", encoding="utf-8")
    with pytest.raises(InputError, match="参考图 · ref") as e:
        load_style_card(card)
    assert e.value.code == "bad_style_card"
    with pytest.raises(InputError, match="reference_images"):
        load_style_card(None, "name: n\ndescription: d\nreference_images:\n  - a.png\n")
    # 只留字段名不写值 = 不设置，仍然能读
    card.write_text("name: n\ndescription: d\nreference_images:\n", encoding="utf-8")
    assert load_style_card(card).name == "n"


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


def test_qc_default_threshold_matches_rubric() -> None:
    """评分标准写的是 7 分起可用：默认及格线也是 7，6 分不算通过。"""
    img = Image.new("RGB", (4, 4))
    assert not qc_score(FakeRouter('{"score":6,"issues":[]}'), img, brief="龙", style=None, tag="t").passed
    assert qc_score(FakeRouter('{"score":7,"issues":[]}'), img, brief="龙", style=None, tag="t").passed


def test_qc_issue_levels() -> None:
    """每条问题带级别；旧格式（一句话）按"必须改"处理；issues 和 details 一一对应。"""
    text = ('{"score":5,"issues":[{"text":" 头顶贴边 ","level":"must"},{"text":"袖口可以更细","level":"suggest"},'
            '"老格式",{"text":"没写级别"},{"text":"  ","level":"must"},{"text":"乱写级别","level":"x"}]}')
    r = qc_score(FakeRouter(text), Image.new("RGB", (4, 4)), brief="龙", style=None, tag="t")
    assert r.issues == ["头顶贴边", "袖口可以更细", "老格式", "没写级别", "乱写级别"]
    assert [d.level for d in r.details] == ["must", "suggest", "must", "must", "must"]
    assert [d.text for d in r.details] == r.issues


def test_qc_prompt_explains_checkerboard() -> None:
    """透明底是叠在棋盘格上给模型看的：提示词里要说明棋盘格不是问题，否则会被误报成"背景不是纯色"。"""
    router = FakeRouter('{"score":8,"issues":[]}')
    qc_score(router, Image.new("RGBA", (8, 8), (0, 0, 0, 0)), brief="纯色背景的龙", style=None, tag="t")
    system = router.calls[0]["messages"][0].text
    assert "棋盘格就是透明区域" in system and "纯色背景的龙" in system


def test_feedback_lines() -> None:
    assert feedback_lines("") == [] and feedback_lines("  \n\n") == []
    text = "- 头顶贴边\n\n• 步摇不精致 \n头顶贴边\n* 飘带短一些"
    assert feedback_lines(text) == ["头顶贴边", "步摇不精致", "飘带短一些"]


def test_expand_passes_feedback_to_model() -> None:
    router = FakeRouter('{"prompt":"a","negative":"b"}')
    expand_prompt(router, "貂蝉", asset_type="character", style=None, feedback="头顶贴边\n- 步摇不精致", tag="t")
    system = router.calls[0]["messages"][0].text
    assert "1. 头顶贴边" in system and "2. 步摇不精致" in system and "$feedback" not in system
    expand_prompt(router, "貂蝉", asset_type="character", style=None, tag="t")
    assert "修订意见：（无）" in router.calls[1]["messages"][0].text


def test_common_issues() -> None:
    reply = ('{"common":[{"text":"脚下留白不足","images":[1,2,3,3]},{"text":"只有一张","images":[2]},'
             '{"text":"头顶贴边","images":[1,2,3,4,9,true]},{"text":" ","images":[1,2]}]}')
    router = FakeRouter(reply)
    per_image = [["a", "b"], ["c"], ["d"], []]
    got = common_issues(router, per_image, tag="t")
    # 编号去重、越界和非数字的丢掉；不足两张的不算共性；按出现张数从多到少排
    assert [(c.text, c.count) for c in got] == [("头顶贴边", 4), ("脚下留白不足", 3)]
    sent = router.calls[0]["messages"][1].text
    assert "第 1 张：a；b" in sent and "第 4 张：（无问题）" in sent
    assert "共 4 张" in router.calls[0]["messages"][0].text


def test_common_issues_skips_model_when_nothing_to_merge() -> None:
    router = FakeRouter("不该被调用")
    assert common_issues(router, [["a"], [], []], tag="t") == [] and common_issues(router, [], tag="t") == []
    assert router.calls == []


@pytest.mark.parametrize("bad", ["x", "{}", '{"common":"a"}', '{"common":[1]}', '{"common":[{"text":"a"}]}'])
def test_common_issues_rejects_garbage(bad: str) -> None:
    with pytest.raises(UpstreamError) as e:
        common_issues(FakeRouter(bad), [["a"], ["b"]], tag="t")
    assert e.value.code == "bad_llm_json"
