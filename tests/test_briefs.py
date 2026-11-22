from pathlib import Path

import pytest

from game_art.briefs import load_briefs, parse_briefs
from game_art.errors import InputError

ROOT = Path(__file__).resolve().parent.parent


def test_example_briefs_load() -> None:
    b = load_briefs(ROOT / "examples/briefs/demo.yaml")
    assert [i.id for i in b.items] == ["warrior", "lady", "elder", "soldier", "granary"]
    assert b.asset_type == "character" and b.prefix == ""
    assert b.find("granary").asset_type == "prop" and b.find("warrior").asset_type is None
    assert b.full_brief(b.find("warrior")).startswith("无名的年轻男性武将")
    # 示例是配套的：清单里的每一项都有对应的标准风格图
    for item in b.items:
        assert (ROOT / "examples/refs" / f"{item.id}.png").is_file()


def test_batch_manifest_is_also_a_brief_list() -> None:
    """批量清单里也是 items: [{id, brief}]，多出来的字段忽略，可以直接当描述清单用。"""
    b = load_briefs(ROOT / "examples/manifests/smoke.yaml")
    assert [i.id for i in b.items] == ["warrior", "lady", "granary"] and b.prefix == ""
    assert b.full_brief(b.items[0]).startswith("无名的年轻男性武将")


def test_find_unknown_item() -> None:
    b = parse_briefs("items:\n  - {id: a, brief: 甲}\n  - {id: b, brief: 乙}")
    with pytest.raises(InputError, match="没有 'c' 这一项（现有：a、b）"):
        b.find("c")


@pytest.mark.parametrize(("text", "hint"), [
    ("name: 国潮\ndescription: d", "不是一份描述清单"),  # 把风格卡拖错了地方
    ("- a\n- b", "不是一份描述清单"),
    ("items: []", "格式不对"),
    ("items:\n  - {id: a}", "items.0.brief"),
    ("items:\n  - {id: '', brief: x}", "items.0.id"),
    ("items:\n  - {id: a, brief: x}\n  - {id: a, brief: y}", "重复的 id"),
    ("items: [", "不是合法的 YAML"),
])
def test_parse_briefs_rejects(text: str, hint: str) -> None:
    with pytest.raises(InputError, match=hint) as e:
        parse_briefs(text)
    assert e.value.code == "bad_briefs"


def test_load_briefs_file_errors(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="必须是 \\.yaml"):
        load_briefs(tmp_path / "a.txt")
    with pytest.raises(InputError, match="读取描述清单失败"):
        load_briefs(tmp_path / "missing.yaml")
