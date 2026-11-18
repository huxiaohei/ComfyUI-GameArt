"""工作流（API 格式）的加载、--set 补丁与上传标记。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from game_art.errors import InputError
from game_art.paths import resolve_repo_path

UPLOAD_PREFIX = "@upload{"


def _resolve_repo(obj: Any) -> Any:
    if isinstance(obj, str):
        return resolve_repo_path(obj)
    if isinstance(obj, list):
        return [_resolve_repo(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _resolve_repo(v) for k, v in obj.items()}
    return obj


def load_workflow(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise InputError("workflow_not_found", f"找不到工作流文件：{path}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise InputError("workflow_invalid", f"工作流不是合法 JSON：{path}（{e}）") from e
    if not isinstance(data, dict):
        raise InputError("workflow_invalid", f"工作流顶层必须是对象：{path}")
    if "nodes" in data and "links" in data:
        raise InputError("ui_format", "这是编辑器 UI 格式的工作流。请在编辑器中 导出(API) 后再使用。")
    for node_id, node in data.items():
        if not (isinstance(node, dict) and "class_type" in node and "inputs" in node):
            raise InputError(
                "workflow_invalid",
                f"节点 {node_id!r} 缺少 class_type 或 inputs，不是 API 格式工作流：{path}",
            )
    return {nid: {**node, "inputs": _resolve_repo(node["inputs"])} for nid, node in data.items()}


def _coerce(existing: Any, raw: str) -> Any:
    """按原值类型解释 --set 的文本：
    - 原值是字符串（STRING 输入）→ 保留原文（如 vars_json={"name":"萬"}、brief=true）；
      唯一例外：原文本身是 JSON 字符串（"..."）时取其内容，兼容旧的带引号写法；
    - 其他（数字/布尔/列表/连线/对象/None）→ 按 JSON 解析，解析失败才当字符串。
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(existing, str):
        return parsed if isinstance(parsed, str) else raw
    return parsed


def apply_sets(wf: dict[str, Any], sets: list[str]) -> dict[str, Any]:
    """应用 "<node_id>.<input>=<value>" 补丁，返回深拷贝，不修改入参。值的解释见 _coerce。"""
    out = copy.deepcopy(wf)
    for item in sets:
        if "=" not in item:
            raise InputError("bad_set", f"--set 格式应为 <节点>.<输入>=<值>：{item!r}")
        target, raw = item.split("=", 1)
        if "." not in target:
            raise InputError("bad_set", f"--set 格式应为 <节点>.<输入>=<值>：{item!r}")
        node_id, input_name = target.split(".", 1)
        if node_id not in out:
            raise InputError(
                "set_unknown_node",
                f"工作流中没有节点 {node_id!r}，可用节点：{', '.join(out)}",
                {"available": list(out)},
            )
        inputs = out[node_id]["inputs"]
        if input_name not in inputs:
            raise InputError(
                "set_unknown_input",
                f"节点 {node_id!r} 没有输入 {input_name!r}，可用输入：{', '.join(inputs)}",
                {"available": list(inputs)},
            )
        inputs[input_name] = _coerce(inputs[input_name], raw)
    return out


def find_uploads(wf: dict[str, Any]) -> list[tuple[str, str, Path]]:
    found: list[tuple[str, str, Path]] = []
    for node_id, node in wf.items():
        for name, value in node.get("inputs", {}).items():
            if isinstance(value, str) and value.startswith(UPLOAD_PREFIX) and value.endswith("}"):
                found.append((node_id, name, Path(value[len(UPLOAD_PREFIX):-1])))
    return found
