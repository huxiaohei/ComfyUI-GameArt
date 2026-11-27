from typing import Any

import pytest

from game_art.comfy.editor import from_editor_graph, to_editor_graph
from game_art.errors import InputError

INFO: dict[str, Any] = {
    "Load": {
        "input": {"required": {"image": [["a.png", "b.png"], {"image_upload": True}]}},
        "input_order": {"required": ["image"]},
        "output": ["IMAGE", "MASK"], "output_name": ["IMAGE", "MASK"],
    },
    "Gen": {
        "input": {
            "required": {
                "prompt": ["STRING", {"multiline": True, "default": ""}],
                "n": ["INT", {"default": 1}],
                "seed": ["INT", {"default": -1}],
                "images": ["IMAGE"],
            },
            "optional": {"tag": ["STRING", {"default": "t"}], "mask": ["MASK"]},
        },
        "input_order": {"required": ["prompt", "n", "seed", "images"], "optional": ["tag", "mask"]},
        "output": ["IMAGE"], "output_name": ["image"],
    },
    "Text": {
        "input": {"required": {"text": ["STRING", {"default": ""}]}},
        "input_order": {"required": ["text"]},
        "output": ["STRING"], "output_name": ["text"],
    },
}

WF: dict[str, Any] = {
    "ref": {"class_type": "Load", "inputs": {"image": "b.png"}, "_meta": {"title": "参考图"}},
    "txt": {"class_type": "Text", "inputs": {"text": "@repo/x"}},
    "gen": {"class_type": "Gen", "inputs": {"prompt": ["txt", 0], "n": 2, "seed": 7,
                                            "images": ["ref", 0], "mask": ["ref", 1]}},
}


def _node(g: dict[str, Any], ga_id: str) -> dict[str, Any]:
    return next(n for n in g["nodes"] if n["properties"]["ga_id"] == ga_id)


def test_graph_shape_and_ids() -> None:
    g = to_editor_graph(WF, INFO, "demo")
    assert g["version"] == 0.4 and isinstance(g["nodes"], list) and isinstance(g["links"], list)
    assert [n["id"] for n in g["nodes"]] == [1, 2, 3] and g["last_node_id"] == 3
    assert g["last_link_id"] == len(g["links"]) == 3
    assert _node(g, "ref")["title"] == "参考图 · ref" and _node(g, "txt")["title"] == "Text · txt"
    assert g == to_editor_graph(WF, INFO, "demo")  # 结果稳定，重复导出不产生无意义的 diff
    assert g["id"] != to_editor_graph(WF, INFO, "other")["id"]


def test_widgets_values_follow_editor_order() -> None:
    g = to_editor_graph(WF, INFO, "demo")
    # 上传控件多一个值；seed 多一个 control_after_generate；被连线的控件与缺省的可选控件取默认值
    assert _node(g, "ref")["widgets_values"] == ["b.png", "image"]
    assert _node(g, "gen")["widgets_values"] == ["", 2, 7, "fixed", "t"]
    assert _node(g, "txt")["widgets_values"] == ["@repo/x"]


def test_links_are_consistent_on_both_ends() -> None:
    g = to_editor_graph(WF, INFO, "demo")
    by_id = {n["id"]: n for n in g["nodes"]}
    for link_id, src, src_slot, dst, dst_slot, typ in g["links"]:
        assert link_id in by_id[src]["outputs"][src_slot]["links"]
        assert by_id[src]["outputs"][src_slot]["type"] == typ
        assert by_id[dst]["inputs"][dst_slot]["link"] == link_id
    gen = _node(g, "gen")
    assert [i["name"] for i in gen["inputs"]] == ["prompt", "n", "seed", "images", "tag", "mask"]
    assert gen["inputs"][0]["widget"] == {"name": "prompt"} and gen["inputs"][0]["link"] is not None
    assert "widget" not in gen["inputs"][3] and gen["inputs"][1]["link"] is None
    assert _node(g, "ref")["inputs"][0]["type"] == "COMBO"
    assert _node(g, "txt")["outputs"][0]["links"] == [gen["inputs"][0]["link"]]


def test_layout_puts_downstream_to_the_right_without_overlap() -> None:
    g = to_editor_graph(WF, INFO, "demo")
    assert _node(g, "gen")["pos"][0] > _node(g, "ref")["pos"][0] == _node(g, "txt")["pos"][0]
    ref, txt = _node(g, "ref"), _node(g, "txt")
    assert txt["pos"][1] >= ref["pos"][1] + ref["size"][1]
    assert _node(g, "gen")["order"] == 2


def test_output_nodes_reserve_room_for_their_preview() -> None:
    info = {**INFO, "Show": {"input": {"required": {"source": ["*"]}}, "output": [], "output_node": True}}
    wf = {**WF, "s1": {"class_type": "Show", "inputs": {"source": ["txt", 0]}},
          "s2": {"class_type": "Show", "inputs": {"source": ["txt", 0]}}}
    g = to_editor_graph(wf, info, "demo")
    s1, s2 = _node(g, "s1"), _node(g, "s2")
    assert s1["size"][1] >= 240 and s2["pos"][1] >= s1["pos"][1] + s1["size"][1]
    assert g["links"][-1][5] == "STRING"  # 连到 "*" 输入的线取上游输出的类型


def test_errors() -> None:
    with pytest.raises(InputError) as e:
        to_editor_graph({"a": {"class_type": "Nope", "inputs": {}}}, INFO, "x")
    assert e.value.code == "unknown_node_class" and "Nope" in e.value.message
    with pytest.raises(InputError):
        to_editor_graph({"a": {"class_type": "Text", "inputs": {"text": ["gone", 0]}}}, INFO, "x")
    with pytest.raises(InputError):
        to_editor_graph({"a": {"class_type": "Text", "inputs": {"text": ["b", 3]}},
                         "b": {"class_type": "Text", "inputs": {"text": ""}}}, INFO, "x")
    with pytest.raises(InputError) as c:
        to_editor_graph({"a": {"class_type": "Text", "inputs": {"text": ["b", 0]}},
                         "b": {"class_type": "Text", "inputs": {"text": ["a", 0]}}}, INFO, "x")
    assert "环" in c.value.message


# ---------------------------------------------------------------- 编辑器格式 → API 格式
def test_from_editor_graph_round_trip() -> None:
    """导出成编辑器格式再转回来，和原来一样（没写的可选控件补上默认值，和编辑器点运行时发出去的一致）。"""
    back = from_editor_graph(to_editor_graph(WF, INFO, "demo"), INFO)
    assert list(back) == ["ref", "txt", "gen"]
    assert back["ref"] == {"class_type": "Load", "inputs": {"image": "b.png"}, "_meta": {"title": "参考图"}}
    assert back["txt"]["inputs"] == {"text": "@repo/x"} and back["txt"]["_meta"]["title"] == "Text"
    assert back["gen"]["inputs"] == {"prompt": ["txt", 0], "n": 2, "seed": 7, "images": ["ref", 0],
                                     "mask": ["ref", 1], "tag": "t"}


def test_from_editor_graph_edited_values_and_helpers() -> None:
    g = to_editor_graph(WF, INFO, "demo")
    _node(g, "gen")["widgets_values"] = ["", 3, 9, "randomize", "我的标签"]  # 在编辑器里改过的值
    # 一个说明框、一个转接点（txt → 转接点 → gen.prompt）、一个被静音的节点连到 gen.mask
    g["nodes"] += [
        {"id": 10, "type": "Note", "mode": 0, "inputs": [], "widgets_values": ["备注"]},
        {"id": 11, "type": "Reroute", "mode": 0, "inputs": [{"name": "", "type": "*", "link": 90}]},
        {"id": 12, "type": "Load", "mode": 2, "inputs": [], "widgets_values": ["a.png", "image"],
         "properties": {"ga_id": "muted"}},
    ]
    prompt = next(i for i in _node(g, "gen")["inputs"] if i["name"] == "prompt")
    mask = next(i for i in _node(g, "gen")["inputs"] if i["name"] == "mask")
    g["links"] += [[90, 2, 0, 11, 0, "STRING"], [91, 11, 0, 3, 0, "STRING"], [92, 12, 1, 3, 5, "MASK"]]
    prompt["link"], mask["link"] = 91, 92
    back = from_editor_graph(g, INFO)
    assert set(back) == {"ref", "txt", "gen"}
    assert back["gen"]["inputs"] == {"prompt": ["txt", 0], "n": 3, "seed": 9, "images": ["ref", 0], "tag": "我的标签"}


def test_from_editor_graph_errors() -> None:
    g = to_editor_graph(WF, INFO, "demo")
    with pytest.raises(InputError, match="不认识节点"):
        from_editor_graph(g, {k: v for k, v in INFO.items() if k != "Gen"})
    _node(g, "gen")["mode"] = 4
    with pytest.raises(InputError, match="旁路"):
        from_editor_graph(g, INFO)
    with pytest.raises(InputError, match="不是编辑器格式"):
        from_editor_graph(WF, INFO)
