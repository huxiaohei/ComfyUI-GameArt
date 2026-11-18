"""把 API 格式工作流转换成 ComfyUI 编辑器（图）格式。

编辑器侧边栏只能打开图格式（含 nodes/links）；API 格式是给 `ga run` 用的。
转换依据 ComfyUI 的 /api/object_info：哪些输入是控件、控件顺序、输出类型。
"""
from __future__ import annotations

import uuid
from typing import Any

from game_art.errors import InputError

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
SEED_NAMES = {"seed", "noise_seed"}
COL_W, NODE_W, GAP_Y, ROW_H, HEAD_H, MULTILINE_H = 420, 340, 60, 26, 60, 110
OUTPUT_NODE_H = 240
_NS = uuid.UUID("6f0c1f5e-7d0a-4b53-9f0e-0c7a1d2e3f40")


def _is_link(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)


def _input_specs(info: dict[str, Any]) -> list[tuple[str, Any, dict[str, Any]]]:
    """按编辑器的顺序返回 (输入名, 类型, 选项)。"""
    specs: list[tuple[str, Any, dict[str, Any]]] = []
    sections: dict[str, Any] = info.get("input", {})
    order: dict[str, list[str]] = info.get("input_order", {})
    for section in ("required", "optional"):
        defs: dict[str, Any] = sections.get(section, {})
        for name in order.get(section, list(defs)):
            spec = defs[name]
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            specs.append((name, spec[0], opts))
    return specs


def _is_widget(typ: Any, opts: dict[str, Any]) -> bool:
    return not opts.get("forceInput") and (isinstance(typ, list) or typ in WIDGET_TYPES)


def _default(typ: Any, opts: dict[str, Any]) -> Any:
    if "default" in opts:
        return opts["default"]
    if isinstance(typ, list):
        return typ[0] if typ else ""
    return {"INT": 0, "FLOAT": 0.0, "BOOLEAN": False}.get(typ, "")


def _depths(wf: dict[str, Any]) -> dict[str, int]:
    depth: dict[str, int] = {}

    def visit(nid: str, trail: tuple[str, ...]) -> int:
        if nid in depth:
            return depth[nid]
        if nid in trail:
            raise InputError("workflow_invalid", f"工作流存在环：{' -> '.join((*trail, nid))}")
        ups = [v[0] for v in wf[nid]["inputs"].values() if _is_link(v)]
        depth[nid] = 1 + max((visit(u, (*trail, nid)) for u in ups), default=-1)
        return depth[nid]

    for nid in wf:
        visit(nid, ())
    return depth


def to_editor_graph(wf: dict[str, Any], object_info: dict[str, Any], name: str) -> dict[str, Any]:
    """wf：API 格式（未解析 @repo/ 的原始内容即可）；返回编辑器图格式（version 0.4）。"""
    unknown = sorted({n["class_type"] for n in wf.values()} - set(object_info))
    if unknown:
        raise InputError(
            "unknown_node_class",
            f"ComfyUI 不认识这些节点：{', '.join(unknown)}。请先运行 ga comfy install 并重启 ComfyUI。",
            {"classes": unknown},
        )
    for nid, node in wf.items():
        for key, v in node["inputs"].items():
            if _is_link(v) and v[0] not in wf:
                raise InputError("workflow_invalid", f"{nid}.{key} 连到了不存在的节点 {v[0]}")

    depth = _depths(wf)
    ids = {nid: i + 1 for i, nid in enumerate(wf)}
    order = {nid: i for i, nid in enumerate(sorted(wf, key=lambda n: (depth[n], ids[n])))}
    links: list[list[Any]] = []
    out_links: dict[tuple[str, int], list[int]] = {}
    col_y: dict[int, int] = {}
    nodes: list[dict[str, Any]] = []

    for nid, node in wf.items():
        info = object_info[node["class_type"]]
        out_types: list[Any] = info.get("output", [])
        inputs: list[dict[str, Any]] = []
        widgets: list[Any] = []
        height = HEAD_H
        for key, typ, opts in _input_specs(info):
            value = node["inputs"].get(key)
            widget = _is_widget(typ, opts)
            slot: dict[str, Any] = {"name": key, "type": "COMBO" if isinstance(typ, list) else typ, "link": None}
            if widget:
                slot["widget"] = {"name": key}
                widgets.append(_default(typ, opts) if value is None or _is_link(value) else value)
                if typ == "INT" and (key in SEED_NAMES or opts.get("control_after_generate")):
                    widgets.append("fixed")
                if opts.get("image_upload"):
                    widgets.append("image")
                height += MULTILINE_H if opts.get("multiline") else ROW_H
            else:
                height += ROW_H
            if _is_link(value):
                src, src_slot = value
                src_types: list[Any] = object_info[wf[src]["class_type"]].get("output", [])
                if src_slot >= len(src_types):
                    raise InputError("workflow_invalid", f"{nid}.{key} 引用了 {src} 不存在的输出 {src_slot}")
                link_id = len(links) + 1
                links.append([link_id, ids[src], src_slot, ids[nid], len(inputs), src_types[src_slot]])
                out_links.setdefault((src, src_slot), []).append(link_id)
                slot["link"] = link_id
            inputs.append(slot)
        height = max(height, HEAD_H + ROW_H * len(out_types))
        if info.get("output_node"):
            # 输出节点运行后会在节点里显示预览（图片/文字），预留位置避免压住下面的节点
            height = max(height, OUTPUT_NODE_H)
        y = col_y.get(depth[nid], 80)
        col_y[depth[nid]] = y + height + GAP_Y
        title = str(node.get("_meta", {}).get("title") or node["class_type"])
        nodes.append({
            "id": ids[nid], "type": node["class_type"], "title": f"{title} · {nid}",
            "pos": [80 + depth[nid] * COL_W, y], "size": [NODE_W, height],
            "flags": {}, "order": order[nid], "mode": 0,
            "inputs": inputs,
            "outputs": [{"name": n, "type": t, "links": None}
                        for n, t in zip(info.get("output_name", out_types), out_types, strict=True)],
            "properties": {"Node name for S&R": node["class_type"], "ga_id": nid},
            "widgets_values": widgets,
        })

    for nid, node_out in zip(wf, nodes, strict=True):
        for i, out in enumerate(node_out["outputs"]):
            out["links"] = out_links.get((nid, i))
    return {
        "id": str(uuid.uuid5(_NS, name)), "revision": 0,
        "last_node_id": len(nodes), "last_link_id": len(links),
        "nodes": nodes, "links": links, "groups": [], "config": {}, "extra": {}, "version": 0.4,
    }
