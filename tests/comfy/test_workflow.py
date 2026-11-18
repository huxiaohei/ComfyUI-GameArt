import json
from pathlib import Path

import pytest

from game_art.comfy.workflow import apply_sets, find_uploads, load_workflow
from game_art.errors import InputError


def test_apply_sets_number_and_string() -> None:
    wf = {"gen": {"class_type": "X", "inputs": {"n": 1, "prompt": ""}}}
    out = apply_sets(wf, ["gen.n=4", "gen.prompt=a=b"])
    assert out["gen"]["inputs"] == {"n": 4, "prompt": "a=b"}
    assert wf["gen"]["inputs"] == {"n": 1, "prompt": ""}  # 不修改入参


def test_apply_sets_string_input_keeps_raw_text() -> None:
    wf = {"c": {"class_type": "X", "inputs": {"vars_json": "{}", "brief": "", "n": 1, "flag": False,
                                              "link": ["a", 0], "opt": None}}}
    out = apply_sets(wf, ['c.vars_json={"name":"萬"}', "c.brief=true", "c.n=3", "c.flag=true",
                          "c.link=12", "c.opt=[1, 2]"])["c"]["inputs"]
    assert out["vars_json"] == '{"name":"萬"}' and out["brief"] == "true"  # 字符串输入：原样
    assert out["n"] == 3 and out["flag"] is True and out["link"] == 12 and out["opt"] == [1, 2]


def test_apply_sets_string_input_accepts_quoted_form() -> None:
    wf = {"c": {"class_type": "X", "inputs": {"vars_json": "{}", "brief": ""}}}
    out = apply_sets(wf, ['c.vars_json="{\\"name\\": \\"萬\\"}"', 'c.brief="龙"'])["c"]["inputs"]
    assert out["vars_json"] == '{"name": "萬"}' and out["brief"] == "龙"  # 旧写法（JSON 字符串）仍兼容


def test_apply_sets_string_input_non_string_json_stays_text() -> None:
    wf = {"c": {"class_type": "X", "inputs": {"size": "1024x1024", "seed_text": ""}}}
    out = apply_sets(wf, ["c.size=512x512", "c.seed_text=42"])["c"]["inputs"]
    assert out == {"size": "512x512", "seed_text": "42"}


def test_apply_sets_unknown_input_lists_keys() -> None:
    wf = {"gen": {"class_type": "X", "inputs": {"n": 1, "prompt": ""}}}
    with pytest.raises(InputError) as e:
        apply_sets(wf, ["gen.nn=1"])
    assert "n" in e.value.message and "prompt" in e.value.message


def test_apply_sets_unknown_node_lists_nodes() -> None:
    wf = {"gen": {"class_type": "X", "inputs": {}}, "brief": {"class_type": "Y", "inputs": {}}}
    with pytest.raises(InputError) as e:
        apply_sets(wf, ["zzz.n=1"])
    assert "gen" in e.value.message and "brief" in e.value.message


def test_ui_format_rejected(tmp_path: Path) -> None:
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"nodes": [], "links": []}))
    with pytest.raises(InputError) as e:
        load_workflow(p)
    assert e.value.code == "ui_format"


def test_load_api_format(tmp_path: Path) -> None:
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"gen": {"class_type": "X", "inputs": {}}}))
    assert "gen" in load_workflow(p)
    p.write_text(json.dumps({"gen": {"inputs": {}}}))
    with pytest.raises(InputError):
        load_workflow(p)


def test_find_uploads(tmp_path: Path) -> None:
    wf = {"a": {"class_type": "L", "inputs": {"image": "@upload{/x/ref.png}", "n": 1}}}
    assert find_uploads(wf) == [("a", "image", tmp_path.__class__("/x/ref.png"))]
