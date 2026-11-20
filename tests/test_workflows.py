import json
from pathlib import Path
from typing import Any

import pytest
import torch

from comfy_nodes import nodes
from comfy_nodes.nodes import NODE_CLASS_MAPPINGS
from game_art.comfy.workflow import apply_sets, load_workflow
from game_art.paths import REPO_ROOT, resolve_repo_path

WF_DIR = REPO_ROOT / "workflows"
NAMES = ["text_to_asset", "text_to_asset_qc", "ref_to_asset", "style_samples", "compose_template"]
WORKFLOW_FILES = [WF_DIR / f"{n}.json" for n in NAMES]
BUILTINS: dict[str, Any] = {
    "LoadImage": {"required": {"image": ("STRING",)}, "returns": ("IMAGE", "MASK")},
    "PreviewAny": {"required": {"source": ("*",)}, "returns": ("STRING",)},  # ComfyUI 自带：把任意值显示成文字
}


def _required(cls_type: str) -> set[str]:
    if cls_type in BUILTINS:
        return set(BUILTINS[cls_type]["required"])
    node_cls: Any = NODE_CLASS_MAPPINGS[cls_type]
    return set(node_cls.INPUT_TYPES()["required"])


def _returns(cls_type: str) -> tuple[str, ...]:
    if cls_type in BUILTINS:
        return tuple(BUILTINS[cls_type]["returns"])
    node_cls: Any = NODE_CLASS_MAPPINGS[cls_type]
    return tuple(node_cls.RETURN_TYPES)


def _is_link(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)


@pytest.mark.parametrize("wf", WORKFLOW_FILES, ids=NAMES)
def test_api_format_and_known_classes(wf: Path) -> None:
    data = load_workflow(wf)
    assert data
    for node in data.values():
        assert node["class_type"] in NODE_CLASS_MAPPINGS or node["class_type"] in BUILTINS
        assert node["_meta"]["title"]


@pytest.mark.parametrize("wf", WORKFLOW_FILES, ids=NAMES)
def test_links_point_to_existing_nodes(wf: Path) -> None:
    data = load_workflow(wf)
    for nid, node in data.items():
        for name, v in node["inputs"].items():
            if _is_link(v):
                assert v[0] in data, f"{nid}.{name} -> 不存在的节点 {v[0]}"
                assert v[1] < len(_returns(data[v[0]]["class_type"])), f"{nid}.{name} 输出序号越界"


@pytest.mark.parametrize("wf", WORKFLOW_FILES, ids=NAMES)
def test_required_inputs_present(wf: Path) -> None:
    data = load_workflow(wf)
    for nid, node in data.items():
        missing = _required(node["class_type"]) - set(node["inputs"])
        assert not missing, f"{nid} 缺少必填输入 {missing}"


@pytest.mark.parametrize("wf", WORKFLOW_FILES, ids=NAMES)
def test_ids_are_readable_and_tags_empty(wf: Path) -> None:
    data = load_workflow(wf)
    assert all(not nid.isdigit() for nid in data)
    for node in data.values():
        if "tag" in node["inputs"]:
            assert node["inputs"]["tag"] == ""


def test_expected_structure() -> None:
    t = load_workflow(WF_DIR / "text_to_asset.json")
    assert [t[k]["class_type"] for k in ("style", "expand", "gen", "bg", "save")] == [
        "GAStyleCard", "GAPromptExpand", "GACloudGenerate", "GARemoveBackground", "GASaveAsset"]
    assert t["gen"]["inputs"]["transparent"] is True and t["bg"]["inputs"]["method"] == "none"
    assert t["save"]["inputs"]["subfolder"] == "ga/text_to_asset" and t["save"]["inputs"]["name"] == "asset"
    assert t["bg"]["inputs"]["image"] == ["gen", 0] and t["bg"]["inputs"]["mask"] == ["gen", 1]
    assert t["save"]["inputs"]["image"] == ["bg", 0] and t["save"]["inputs"]["mask"] == ["bg", 1]
    # 参考图由 ref 节点提供（默认留空 = 纯文字出图），不再经过风格卡
    assert t["ref"]["class_type"] == "GAReferenceImages" and t["ref"]["inputs"]["paths"] == ""
    assert t["gen"]["inputs"]["refs"] == ["ref", 0] and "style" not in t["gen"]["inputs"]
    assert t["save"]["inputs"]["clean_alpha"] == 0
    # 扩写出的提示词在画布上看得见
    assert t["show_prompt"]["inputs"]["source"] == ["expand", 0]
    assert t["show_negative"]["inputs"]["source"] == ["expand", 1]
    # 带质检的版本：质检结果放在生成和去背景之间，图片、蒙版、记录都经过它
    q = load_workflow(WF_DIR / "text_to_asset_qc.json")
    assert q["qc"]["class_type"] == "GAQCReview" and q["qc"]["inputs"]["image"] == ["gen", 0]
    assert q["qc"]["inputs"]["mask"] == ["gen", 1] and q["qc"]["inputs"]["meta"] == ["gen", 2]
    assert q["qc"]["inputs"]["style"] == ["style", 0] and q["qc"]["inputs"]["brief"] == ""
    assert q["bg"]["inputs"]["image"] == ["qc", 0] and q["bg"]["inputs"]["mask"] == ["qc", 1]
    assert q["save"]["inputs"]["meta"] == ["qc", 2] and q["expand"]["inputs"]["feedback"] == ""
    assert list(q).index("gen") < list(q).index("qc") < list(q).index("bg")
    r = load_workflow(WF_DIR / "ref_to_asset.json")
    assert r["ref"]["class_type"] == "GAReferenceImages" and r["ref"]["inputs"]["paths"] == ""
    assert r["expand"]["inputs"]["ref_description"] == ["analyze", 0]
    assert r["analyze"]["inputs"]["refs"] == ["ref", 0] and r["edit"]["inputs"]["refs"] == ["ref", 0]
    assert "images" not in r["edit"]["inputs"] and "images" not in r["analyze"]["inputs"]
    assert r["bg"]["inputs"]["image"] == ["edit", 0] and r["bg"]["inputs"]["mask"] == ["edit", 1]
    assert r["save"]["inputs"]["mask"] == ["bg", 1]
    s = load_workflow(WF_DIR / "style_samples.json")
    assert s["gen"]["inputs"]["n"] == 4 and s["save"]["inputs"]["subfolder"] == "style_samples"
    assert s["gen"]["inputs"]["refs"] == ["ref", 0] and "style" not in s["gen"]["inputs"]
    assert s["save"]["inputs"]["image"] == ["gen", 0] and s["save"]["inputs"]["mask"] == ["gen", 1]
    c = load_workflow(WF_DIR / "compose_template.json")
    assert c["compose"]["inputs"]["vars_json"] == '{"name": "萬"}'
    assert c["compose"]["inputs"]["art"] == ["art", 0]


@pytest.mark.parametrize("wf", WORKFLOW_FILES, ids=NAMES)
def test_workflows_accept_sets(wf: Path) -> None:
    data = load_workflow(wf)
    assert "save" in apply_sets(data, ["save.name=x"])


def test_repo_prefix_resolved_by_load_workflow() -> None:
    c = load_workflow(WF_DIR / "compose_template.json")
    p = Path(c["compose"]["inputs"]["layout_path"])
    assert p == REPO_ROOT / "examples/layouts/demo_tile.yaml" and p.is_file()
    for wf in WORKFLOW_FILES:
        for node in load_workflow(wf).values():
            for v in node["inputs"].values():
                assert not (isinstance(v, str) and v.startswith("@repo/"))


def test_resolve_repo_path() -> None:
    assert resolve_repo_path("@repo/a/b.yaml") == str(REPO_ROOT / "a/b.yaml")
    assert resolve_repo_path("/abs/x") == "/abs/x"
    assert resolve_repo_path("x@repo/y") == "x@repo/y"


def test_load_workflow_resolves_nested_strings(tmp_path: Path) -> None:
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"a": {"class_type": "X", "inputs": {"p": "@repo/q", "n": 1, "l": ["b", 0]}}}))
    assert load_workflow(p)["a"]["inputs"]["p"] == str(REPO_ROOT / "q")


def test_nodes_resolve_repo_prefix() -> None:
    card = nodes.GAStyleCard().run("@repo/examples/style_cards/guochao.yaml")[0]
    assert card.name == "国潮插画风"


@pytest.mark.skipif(not (REPO_ROOT / "examples/fonts/SourceHanSerifHK-Bold.otf").is_file(),
                    reason="未下载示例字体")
def test_compose_node_resolves_repo_prefix() -> None:
    img, _ = nodes.GAComposeTemplate().run(
        "@repo/examples/layouts/demo_tile.yaml", '{"name": "萬"}', art=torch.ones((1, 16, 16, 3)))
    assert img.shape[1:3] == (560, 400)
