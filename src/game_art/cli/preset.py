"""项目预设工作流（编辑器格式）在命令行里的用法：ga run --preset / ga fix / ga regen / ga qc。

预设是在编辑器里搭好、存在游戏项目里的工作流，风格卡、参考图、描述清单、保存位置都在里面。
命令行把它转成 API 格式，再按命令改几处输入，效果对应编辑器里「质检结果」面板上的按钮：

| 命令      | 编辑器里的操作       | 改了哪些输入 |
|-----------|----------------------|--------------|
| run --item| 在 item 里选一项     | expand.item / brief / asset_type、清空 feedback、save.name |
| fix       | 修这张               | qc.fix = {"file": 候选图完整路径, "notes", "issues"} |
| regen     | 采纳并重新生成       | expand.brief / item、expand.feedback 追加选中的问题 |
| qc        | 质检这张             | qc.fix = {"file": 图的完整路径, "qc": true} |

fix / regen 的结果存到候选图所在的文件夹，名字沿用候选图去掉编号的部分，编号由「保存资产」接着往下排。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from game_art import assets
from game_art.briefs import load_briefs
from game_art.comfy.editor import from_editor_graph, is_editor_graph
from game_art.comfy.workflow import load_workflow, resolve_repo_values
from game_art.errors import InputError
from game_art.paths import resolve_repo_path
from game_art.steps import feedback_lines

EXPAND, QC, SAVE = "expand", "qc", "save"
FIX_LEVELS = ("high", "medium", "low", "跟随配置")
FIDELITY_LEVELS = ("high", "low", "跟随配置")


def load_preset(path: Path, object_info: dict[str, Any]) -> dict[str, Any]:
    """读预设：编辑器格式就转成 API 格式；本来就是 API 格式的也收。"""
    path = Path(path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise InputError("workflow_not_found", f"找不到预设工作流：{path}") from e
    except ValueError as e:
        raise InputError("workflow_invalid", f"预设工作流不是合法 JSON：{path}（{e}）") from e
    if not is_editor_graph(data):
        return load_workflow(path)
    wf = from_editor_graph(data, object_info)
    return {nid: {**node, "inputs": resolve_repo_values(node["inputs"])} for nid, node in wf.items()}


def node(wf: dict[str, Any], nid: str, what: str) -> dict[str, Any]:
    if nid not in wf:
        raise InputError("preset_node_missing", (
            f"预设工作流里没有 {nid} 节点（{what}）。预设要由 text_to_asset_qc 生成，节点名不要改"),
            {"available": list(wf)})
    inputs: dict[str, Any] = wf[nid]["inputs"]
    return inputs


def apply_item(wf: dict[str, Any], item: str) -> None:
    """和编辑器里点 item 选一项一样：描述填进 brief、素材种类填进 asset_type、清空修订意见、保存的文件名改成 id。"""
    expand = node(wf, EXPAND, "提示词扩写")
    briefs_path = str(expand.get("briefs_path") or "").strip()
    if not briefs_path:
        raise InputError("no_briefs", "预设的「提示词扩写」节点没有填描述清单（briefs_path），不能用 --item")
    briefs = load_briefs(Path(resolve_repo_path(briefs_path)).expanduser())
    chosen = briefs.find(item)
    expand.update(item=chosen.id, brief=briefs.full_brief(chosen), feedback="")
    asset_type = chosen.asset_type or briefs.asset_type
    if asset_type:
        expand["asset_type"] = asset_type
    if SAVE in wf:
        wf[SAVE]["inputs"]["name"] = chosen.id


def _save_next_to(wf: dict[str, Any], png: Path) -> None:
    """新图存到候选图旁边，名字沿用它去掉编号的部分，编号由保存节点接着排。"""
    save = node(wf, SAVE, "保存资产")
    save.update(subfolder=str(png.parent), name=assets.base_name(png))


def prune_to(wf: dict[str, Any], keep: str) -> dict[str, Any]:
    """只留 keep 和它的上游（去掉「查看提示词」这类只在编辑器里看的输出节点，免得它们把扩写也跑一遍）。"""
    out: dict[str, Any] = {}
    stack = [keep]
    while stack:
        nid = stack.pop()
        if nid in out or nid not in wf:
            continue
        out[nid] = wf[nid]
        stack += [v[0] for v in wf[nid]["inputs"].values()
                  if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)]
    return {nid: wf[nid] for nid in wf if nid in out}


def prepare_fix(wf: dict[str, Any], png: Path, record: dict[str, Any], issues: list[int], notes: list[str], *,
                with_refs: bool | None = None, edit_profile: str | None = None, edit_model: str | None = None,
                quality: str | None = None, fidelity: str | None = None) -> dict[str, Any]:
    """「修这张」：只带细节类问题（最多 3 条）和补充；没写的选项沿用预设里的值。"""
    wf = copy.deepcopy(wf)
    picked = assets.pick_issues(record, issues, details_only=True)
    lines: list[str] = []
    for text in [i["text"] for i in picked] + notes:
        if text.strip() and text.strip() not in lines:
            lines.append(text.strip())
    if not lines:
        raise InputError("no_fix_notes", "没说要改什么：用 --issue 选这张图的细节类问题，或者用 --note 写上")
    qc = node(wf, QC, "质检结果")
    qc["fix"] = json.dumps({"file": str(png), "notes": lines, "issues": [i["n"] for i in picked]},
                           ensure_ascii=False)
    qc["auto_qc"] = True  # 修完自动对新图质检一次
    if with_refs is not None:
        qc["fix_with_refs"] = with_refs
    if edit_profile:
        qc["edit_profile"] = edit_profile
    if edit_model is not None:
        qc["edit_model"] = edit_model
    if quality:
        _choice("--quality", quality, FIX_LEVELS)
        qc["fix_quality"] = quality
    if fidelity:
        _choice("--fidelity", fidelity, FIDELITY_LEVELS)
        qc["fix_fidelity"] = fidelity
    _save_next_to(wf, png)
    return prune_to(wf, SAVE)


def _choice(flag: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise InputError("bad_option", f"{flag} 只能是 {' / '.join(allowed)}：{value!r}")


def regen_feedback(record: dict[str, Any], issues: list[int], notes: list[str], *, reset: bool) -> list[str]:
    """修订意见逐轮累积：这张图出图时带的意见（记录里的 feedback）+ 这次选的问题 + 补充，去重保持先后。"""
    earlier = [] if reset else [str(x) for x in record.get("feedback") or [] if isinstance(x, str)]
    picked = [i["text"] for i in assets.pick_issues(record, issues, details_only=False)]
    return feedback_lines("\n".join([*earlier, *picked, *notes]))


def prepare_regen(wf: dict[str, Any], png: Path, record: dict[str, Any], issues: list[int], notes: list[str], *,
                  reset: bool) -> dict[str, Any]:
    """「采纳并重新生成」：用这张图的描述，带上累积的修订意见整张重出。"""
    wf = copy.deepcopy(wf)
    expand = node(wf, EXPAND, "提示词扩写")
    item = str(record.get("item") or "").strip()
    if item and str(expand.get("briefs_path") or "").strip():
        apply_item(wf, item)  # 素材种类等跟着清单走
    brief = str(record.get("brief") or "").strip()
    if not brief:
        raise InputError("no_brief", f"这张图的记录里没有描述（brief），不知道该画什么：{assets.record_path(png)}")
    expand.update(brief=brief, item=item, feedback="\n".join(regen_feedback(record, issues, notes, reset=reset)))
    node(wf, QC, "质检结果")["fix"] = ""
    _save_next_to(wf, png)
    return wf


def prepare_qc(wf: dict[str, Any], png: Path) -> dict[str, Any]:
    """「质检这张」：不出图、不改图，只给这张图打分。"""
    wf = copy.deepcopy(wf)
    node(wf, QC, "质检结果")["fix"] = json.dumps({"file": str(png), "qc": True}, ensure_ascii=False)
    node(wf, SAVE, "保存资产")
    return prune_to(wf, SAVE)


def saved_files(history: dict[str, Any]) -> list[Path]:
    """「保存资产」节点这次写下的图（完整路径）。"""
    out: list[Path] = []
    for node_out in (history.get("outputs") or {}).values():
        for f in node_out.get("ga_files") or []:
            out.append(Path(f))
    return out


def qc_result(history: dict[str, Any]) -> dict[str, Any]:
    """「质检这张」那次运行里质检节点给出的结果（第一张图），格式和记录里的 qc 一样。"""
    payload = ((history.get("outputs") or {}).get(QC) or {}).get("ga_qc") or []
    results = payload[0].get("results") if payload and isinstance(payload[0], dict) else None
    if not results:
        raise InputError("no_qc_result", "ComfyUI 没有返回质检结果：确认 ComfyUI 里的节点是最新的（更新后要重启）")
    r = results[0]
    details = [d for d in r.get("issues") or [] if isinstance(d, dict)]
    return {"score": r.get("score"), "issues": [d.get("text") for d in details], "passed": r.get("passed"),
            "details": details}
