"""LangGraph 批量图：generate → qc → route 逐项循环，然后 sheet → select（interrupt）→ finalize。

崩溃安全：每个 generate 只处理一个待生成项，LangGraph 在每个节点之后写 checkpoint，
进程被杀后最多重做当前这一项；提交前还会检查该次尝试是否已下载到磁盘，已下载就直接复用，
因此已完成的项不会重复花钱。
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt
from PIL import Image
from pydantic import BaseModel

from game_art.comfy.client import ComfyClient
from game_art.comfy.workflow import apply_sets, find_uploads, load_workflow
from game_art.config import Settings, load_settings
from game_art.errors import BudgetExceeded, ExecutionError, GAError, InputError
from game_art.imaging import contact_sheet
from game_art.ledger import Ledger
from game_art.providers.router import Router
from game_art.steps import QCResult, qc_score
from game_art.style import load_style_card

from .manifest import Manifest, load_manifest
from .store import BatchStore

WAIT_TIMEOUT = 900.0
Status = Literal["pending", "generated", "needs_review", "selected", "failed"]
JSON = dict[str, Any]
App = CompiledStateGraph["BatchState", Any, "BatchState", "BatchState"]
Stage = Literal["interrupted", "awaiting_budget", "awaiting_selection", "finished"]


class Candidate(TypedDict):
    path: str
    qc: JSON | None  # QCResult.model_dump()；质检前为 None


class ItemState(TypedDict):
    brief: str
    attempts: int  # 本轮（上次重跑以来）的生成次数，重试上限按它算
    offset: NotRequired[int]  # 此前各轮累计次数；全局尝试编号 = offset + attempts，跨重跑唯一（旧状态缺省 0）
    candidates: list[Candidate]
    status: Status
    error: str | None


class BatchState(TypedDict):
    items: dict[str, ItemState]  # 插入顺序 = 清单顺序
    current: str | None  # 刚生成、等待质检/路由的项
    budget: float | None  # 生效预算（恢复时可改）
    numbers: list[list[Any]]  # 编号 n → numbers[n-1] = [item_id, 候选序号(1 起)]
    selection: dict[str, list[int]] | None  # item_id → 候选序号(1 起)
    sheet_path: str | None


@dataclass
class Deps:
    settings: Settings
    client: Any  # ComfyClient 兼容对象
    ledger: Ledger
    router: Any
    qc: Callable[..., QCResult] = qc_score


def default_deps() -> Deps:
    s = load_settings()
    ledger = Ledger.from_settings(s)
    return Deps(settings=s, client=ComfyClient(s.comfy_url), ledger=ledger, router=Router(s, ledger))


class NumberEntry(BaseModel):
    number: int
    item_id: str
    label: str
    path: str
    score: float | None
    passed: bool | None


class ItemStatus(BaseModel):
    id: str
    brief: str
    status: str
    attempts: int
    candidates: int
    error: str | None
    cost: float
    picked: list[int]


class BatchStatus(BaseModel):
    batch_id: str
    batch_dir: str
    stage: Stage
    waiting: JSON | None
    sheet_path: str | None
    cost: float
    budget: float | None
    items: list[ItemStatus]
    numbers: list[NumberEntry]
    next_command: str | None
    manifest_path: str | None


# ---------------------------------------------------------------- 工作流填参
def _tag(m: Manifest, item_id: str, attempt: int) -> str:
    return f"{m.batch_id}/{item_id}/{attempt}"


def _set(key: str, value: Any) -> str:
    return f"{key}={json.dumps(value, ensure_ascii=False)}"


BRIEF_NODE = "expand"
N_NODES = ("gen", "edit")  # 候选数写到第一个存在的节点


def _n_node(wf: JSON) -> str | None:
    return next((n for n in N_NODES if n in wf), None)


def check_workflow_targets(m: Manifest, wf: JSON) -> None:
    """工作流缺少 brief / 候选数的落点时报错（而不是静默忽略）；除非每个受影响的项自己用 sets 提供了：
    缺 expand → 该项（defaults + sets）里有 *.prompt 或 *.brief；缺 gen/edit → 有 *.n。"""
    problems = []
    for item in m.items:
        keys = {k.split(".", 1)[1] for k in {**m.defaults, **item.sets}}
        if BRIEF_NODE not in wf and not keys & {"prompt", "brief"}:
            problems.append(f"项 {item.id}：工作流没有 {BRIEF_NODE} 节点，brief 无处可填"
                            f"（可在 sets 里指定 <节点>.prompt）")
        if _n_node(wf) is None and "n" not in keys:
            problems.append(f"项 {item.id}：工作流没有 {'/'.join(N_NODES)} 节点，候选数无处可填"
                            f"（可在 sets 里指定 <节点>.n）")
    if problems:
        raise InputError("workflow_missing_node", f"工作流 {m.workflow} 不适合批量：" + "；".join(problems),
                         {"problems": problems})


def build_workflow(m: Manifest, item_id: str, brief: str, attempt: int) -> JSON:
    """固定参数（节点存在才设）→ defaults → item.sets → tag/subfolder（结构性，最后强制）。"""
    wf = load_workflow(m.workflow)
    fixed: JSON = {f"{BRIEF_NODE}.brief": brief}
    if n_node := _n_node(wf):
        fixed[f"{n_node}.n"] = m.candidates
    if m.style_card:
        fixed["style.path"] = str(m.style_card)
    sets = [_set(k, v) for k, v in fixed.items() if k.split(".", 1)[0] in wf]
    sets += [_set(k, v) for k, v in m.defaults.items()]
    sets += [_set(k, v) for k, v in m.item(item_id).sets.items()]
    tag = _tag(m, item_id, attempt)
    if "save" in wf:
        sets.append(_set("save.subfolder", f"ga/{tag}"))
    wf = apply_sets(wf, sets)
    for node in wf.values():
        if "tag" in node["inputs"]:
            node["inputs"]["tag"] = tag
    return wf


# ---------------------------------------------------------------- 图
def _attempt_no(it: ItemState, k: int | None = None) -> int:
    """本轮第 k 次（缺省 = 当前次）对应的全局尝试编号：用于 tag、磁盘目录与 prompt 文件。"""
    return it.get("offset", 0) + (it["attempts"] if k is None else k)


def _halts_run(e: GAError) -> bool:
    """ComfyUI 不可达 / 等待超时 / ComfyUI 5xx：属于环境问题而非该项本身，中止运行等待续跑。"""
    if e.code in ("comfy_unreachable", "comfy_wait_timeout"):
        return True
    status = e.details.get("status")
    return e.code == "comfy_http_error" and isinstance(status, int) and status >= 500


def _pending(state: BatchState) -> str | None:
    return next((i for i, it in state["items"].items() if it["status"] == "pending"), None)


def _rerun_update(store: BatchStore, state: BatchState, ids: list[str], brief: str | None) -> JSON:
    items = dict(state["items"])
    for iid in ids:
        store.archive_item(iid)
        old = items[iid]
        items[iid] = {"brief": brief or old["brief"], "attempts": 0, "offset": _attempt_no(old),
                      "candidates": [], "status": "pending", "error": None}
    selection = {k: v for k, v in (state["selection"] or {}).items() if k not in ids}
    return {"items": items, "selection": selection or None, "current": None, "numbers": [], "sheet_path": None}


def build_graph(m: Manifest, store: BatchStore, deps: Deps) -> StateGraph[BatchState, Any, BatchState, BatchState]:
    style = load_style_card(m.style_card) if m.style_card else None
    max_retries = m.max_retries if m.max_retries is not None else deps.settings.max_retries
    prefix = f"{m.batch_id}/"

    def await_budget(budget: float | None) -> float | None:
        while True:
            try:
                deps.ledger.check_budget(prefix, budget)
                return budget
            except BudgetExceeded as e:
                value = interrupt({"reason": "budget", "spent": e.details["spent"], "budget": budget})
                budget = float(value["budget"])

    def known_prompt(item_id: str, k: int) -> str | None:
        """上次进程已提交且 ComfyUI 仍认识（在队列或历史里）的 prompt_id；先查队列再查历史，避免漏掉刚跑完的。"""
        prompt_id = store.load_prompt(item_id, k)
        if prompt_id and (deps.client.queue_contains(prompt_id) or deps.client.history(prompt_id)):
            return prompt_id
        return None

    def run_comfy(item_id: str, brief: str, k: int, prompt_id: str | None) -> list[Path]:
        if prompt_id is None:
            wf = build_workflow(m, item_id, brief, k)
            for node_id, name, path in find_uploads(wf):
                wf[node_id]["inputs"][name] = deps.client.upload(path)
            prompt_id = deps.client.submit(wf)
            store.save_prompt(item_id, k, prompt_id)  # 之后再被杀：续跑时等这个任务，不再提交
        entry = deps.client.wait(prompt_id, WAIT_TIMEOUT)
        written = deps.client.download_outputs(entry, store.reset_partial(item_id, k))
        if not any(Path(p).suffix.lower() == ".png" for p in written):
            raise ExecutionError("no_outputs", f"工作流没有产出 PNG（prompt_id {prompt_id}）")
        store.commit_attempt(item_id, k)
        return store.attempt_pngs(item_id, k)

    def generate(state: BatchState) -> JSON:
        item_id = _pending(state)
        if item_id is None:
            return {"current": None}
        it = state["items"][item_id].copy()
        k_round = it["attempts"] + 1
        k = _attempt_no(it, k_round)  # 全局编号，跨重跑唯一
        budget = state["budget"]
        pngs = store.attempt_pngs(item_id, k)  # 崩溃前已下载完：直接复用，不再提交
        if not pngs:
            try:
                prompt_id = known_prompt(item_id, k)
                if prompt_id is None:  # 续等已提交的任务不花新钱，不查预算
                    budget = await_budget(budget)
                pngs = run_comfy(item_id, it["brief"], k, prompt_id)
            except GAError as e:
                if _halts_run(e):  # 不是单项的问题：中止本次运行，ga batch resume 续跑（会续等已提交的任务）
                    e.details["resume"] = f"ga batch resume {store.root}"
                    raise
                it.update({"attempts": k_round, "candidates": [], "status": "failed", "error": e.message})
                return {"items": {**state["items"], item_id: it}, "current": None, "budget": budget}
        it.update({"attempts": k_round, "candidates": [{"path": str(p), "qc": None} for p in pngs], "error": None})
        return {"items": {**state["items"], item_id: it}, "current": item_id, "budget": budget}

    def qc(state: BatchState) -> JSON:
        item_id = state["current"]
        assert item_id is not None  # qc 只在 generate 产出了 current 之后才会被走到
        it = state["items"][item_id].copy()
        tag = _tag(m, item_id, _attempt_no(it))
        cands: list[Candidate] = []
        try:
            for c in it["candidates"]:
                with Image.open(c["path"]) as im:
                    img = im.convert("RGBA")
                r = deps.qc(deps.router, img, brief=it["brief"], style=style, threshold=m.qc_threshold, tag=tag)
                cands.append({**c, "qc": r.model_dump()})
        except (GAError, OSError) as e:  # OSError：下载的图打不开
            it.update({"status": "needs_review", "error": f"质检失败：{getattr(e, 'message', e)}"})
            return {"items": {**state["items"], item_id: it}}
        it["candidates"] = cands
        return {"items": {**state["items"], item_id: it}}

    def route(state: BatchState) -> JSON:
        item_id = state["current"]
        if item_id is None:
            return {}
        it = state["items"][item_id].copy()
        if it["status"] == "pending":
            qcs = [cast(JSON, c["qc"]) for c in it["candidates"]]  # 走到 route 时每个候选都已质检
            scores = [q["score"] for q in qcs]
            if any(q["passed"] for q in qcs):
                it["status"] = "generated"
            elif it["attempts"] > max_retries:
                it.update({"status": "needs_review",
                           "error": f"质检未通过：已生成 {it['attempts']} 次，最高 {max(scores, default=0):.1f} 分"})
        return {"items": {**state["items"], item_id: it}, "current": None}

    def sheet(state: BatchState) -> JSON:
        numbers: list[list[Any]] = [[iid, j] for iid, it in state["items"].items() if it["status"] == "generated"
                   for j in range(1, len(it["candidates"]) + 1)]
        if not numbers:
            return {"numbers": [], "sheet_path": None}
        images, labels = [], []
        for n, (iid, j) in enumerate(numbers, 1):
            with Image.open(state["items"][iid]["candidates"][j - 1]["path"]) as im:
                images.append(im.convert("RGBA"))
            labels.append(f"{iid}#{n}")
        store.sheet_path.parent.mkdir(parents=True, exist_ok=True)
        contact_sheet(images, labels).save(store.sheet_path)
        return {"numbers": numbers, "sheet_path": str(store.sheet_path)}

    def select(state: BatchState) -> JSON:
        value = interrupt({"reason": "selection", "sheet": state["sheet_path"]})
        if "rerun" in value:
            return _rerun_update(store, state, value["rerun"], value.get("brief"))
        return {"selection": {**(state["selection"] or {}), **resolve_selection(state, value)}}

    def finalize(state: BatchState) -> JSON:
        selection = state["selection"] or {}
        items = dict(state["items"])
        entries = []
        store.final_dir.mkdir(parents=True, exist_ok=True)
        for iid, orig in state["items"].items():
            it = orig
            files = []
            if iid in selection and it["status"] in ("generated", "selected"):
                store.clear_final(iid)
                picks = selection[iid]
                for k, idx in enumerate(picks, 1):
                    c = it["candidates"][idx - 1]
                    src, dst = Path(c["path"]), store.final_png(iid, k, len(picks))
                    shutil.copyfile(src, dst)
                    side = src.with_suffix(".json")
                    meta = json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {}
                    info = {"item_id": iid, "brief": it["brief"], "candidate": idx, "source": str(src),
                            "qc": c["qc"], "meta": meta}
                    dst.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                                        encoding="utf-8")
                    files.append({"png": str(dst), "json": str(dst.with_suffix(".json")), **info})
                items[iid] = it = {**it, "status": "selected"}
            entries.append({"id": iid, "brief": it["brief"], "status": it["status"], "error": it["error"],
                            "attempts": it["attempts"], "cost": deps.ledger.spent(f"{prefix}{iid}/"),
                            "files": files})
        out = {"batch_id": m.batch_id, "cost": deps.ledger.spent(prefix), "budget": state["budget"],
               "items": entries}
        store.manifest_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"items": items}

    g = StateGraph(BatchState)
    for name, fn in [("generate", generate), ("qc", qc), ("route", route), ("sheet", sheet),
                     ("select", select), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "generate")
    g.add_conditional_edges("generate", lambda s: "qc" if s["current"] else "route", ["qc", "route"])
    g.add_edge("qc", "route")
    g.add_conditional_edges("route", lambda s: "generate" if _pending(s) else "sheet", ["generate", "sheet"])
    g.add_edge("sheet", "select")
    g.add_conditional_edges("select", lambda s: "generate" if _pending(s) else "finalize",
                            ["generate", "finalize"])
    g.add_edge("finalize", END)
    return g


# ---------------------------------------------------------------- 选择校验
def resolve_selection(state: BatchState, value: Any) -> dict[str, list[int]]:
    """把 {"pick": {item: [编号]}} / {"pick_numbers": [编号]} 解析为 item → 候选序号；无效则抛 InputError。"""
    if not isinstance(value, dict) or not ({"pick", "pick_numbers"} & value.keys()):
        raise InputError("bad_selection", '选择格式应为 {"pick_numbers": [...]} 或 {"pick": {"<项>": [...]}}')
    numbers, items = state["numbers"], state["items"]
    problems: list[str] = []
    picks: dict[str, list[int]] = {}
    if "rerun" in value:
        problems.append("不能在同一次操作里既重跑又挑选")
    pick_numbers, pick = value.get("pick_numbers") or [], value.get("pick") or {}
    if not isinstance(pick_numbers, list):
        problems.append("pick_numbers 应为编号列表")
        pick_numbers = []
    if not isinstance(pick, dict):
        problems.append("pick 应为 {项: [编号...]}")
        pick = {}

    def add(n: Any, want: str | None) -> None:
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(numbers):
            problems.append(f"编号 {n} 不存在（有效范围 1–{len(numbers)}）" if numbers
                            else f"编号 {n} 不存在（没有可选候选）")
            return
        owner, idx = numbers[n - 1]
        if want is not None and owner != want:
            problems.append(f"编号 {n} 属于 {owner}，不属于 {want}")
            return
        lst = picks.setdefault(owner, [])
        if idx not in lst:
            lst.append(idx)

    for n in pick_numbers:
        add(n, None)
    for iid, ns in pick.items():
        if not isinstance(ns, list):
            problems.append(f"项 {iid} 的编号应为列表")
        elif iid not in items:
            problems.append(f"项 {iid} 不存在")
        elif items[iid]["status"] != "generated":
            problems.append(f"项 {iid} 状态为 {items[iid]['status']}，不能选（可用 ga batch rerun 重跑）")
        else:
            for n in ns:
                add(n, iid)
    missing = [i for i, it in items.items() if it["status"] == "generated" and i not in picks]
    if missing:
        problems.append(f"以下项未选：{', '.join(missing)}")
    if problems:
        raise InputError("bad_selection", "选择无效：" + "；".join(problems), {"problems": problems})
    return picks


# ---------------------------------------------------------------- 对外 API
def _config(m: Manifest, deps: Deps) -> RunnableConfig:
    retries = m.max_retries if m.max_retries is not None else deps.settings.max_retries
    return {"configurable": {"thread_id": m.batch_id},
            "recursion_limit": len(m.items) * (retries + 1) * 3 + 50}


@contextmanager
def _opened(batch_dir: Path, deps: Deps, *, lock: bool) -> Iterator[tuple[BatchStore, Manifest, App, RunnableConfig]]:
    """打开批次；lock=True 时持有批次锁（会推进状态的操作都要加锁，只读查询不加）。"""
    store = BatchStore(batch_dir)
    if not store.spec_path.is_file():
        raise InputError("not_a_batch", f"不是批次目录（缺少 batch.json）：{store.root}")
    m = store.load_spec()
    with (store.lock() if lock else nullcontext()), SqliteSaver.from_conn_string(str(store.state_db)) as saver:
        yield store, m, build_graph(m, store, deps).compile(checkpointer=saver), _config(m, deps)


def _waiting(snap: StateSnapshot) -> JSON | None:
    return dict(snap.interrupts[0].value) if snap.interrupts else None


def _stage(snap: StateSnapshot) -> Stage:
    w = _waiting(snap)
    if w:
        return "awaiting_budget" if w["reason"] == "budget" else "awaiting_selection"
    return "interrupted" if snap.next else "finished"


def _status(store: BatchStore, m: Manifest, app: App, cfg: RunnableConfig, deps: Deps) -> BatchStatus:
    snap = app.get_state(cfg)
    if not snap.values:
        raise InputError("not_a_batch", f"批次尚未开始运行：{store.root}")
    v, stage = snap.values, _stage(snap)
    prefix, d = f"{m.batch_id}/", store.root
    items = [ItemStatus(id=iid, brief=it["brief"], status=it["status"], attempts=it["attempts"],
                        candidates=len(it["candidates"]), error=it["error"],
                        cost=deps.ledger.spent(f"{prefix}{iid}/"), picked=(v["selection"] or {}).get(iid, []))
             for iid, it in v["items"].items()]
    numbers = []
    if stage == "awaiting_selection":
        for n, (iid, j) in enumerate(v["numbers"], 1):
            c = v["items"][iid]["candidates"][j - 1]
            numbers.append(NumberEntry(number=n, item_id=iid, label=f"{iid}#{n}", path=c["path"],
                                       score=(c["qc"] or {}).get("score"), passed=(c["qc"] or {}).get("passed")))
    next_cmd = {"awaiting_selection": f"ga batch select {d} <编号...>（或 --item <项>=<编号,...>；"
                                      f"不满意用 ga batch rerun {d} <项...>）",
                "awaiting_budget": f"ga batch resume {d} --budget <新预算>",
                "interrupted": f"ga batch resume {d}", "finished": None}[stage]
    return BatchStatus(
        batch_id=m.batch_id, batch_dir=str(d), stage=stage, waiting=_waiting(snap), sheet_path=v["sheet_path"],
        cost=deps.ledger.spent(prefix), budget=v["budget"], items=items, numbers=numbers, next_command=next_cmd,
        manifest_path=str(store.manifest_path) if stage == "finished" and store.manifest_path.is_file() else None)


def start_batch(manifest_path: Path, out_root: Path, *, deps: Deps | None = None) -> BatchStatus:
    m = load_manifest(manifest_path)
    check_workflow_targets(m, load_workflow(m.workflow))  # 尽早报错，免得每项都失败
    if m.style_card:
        load_style_card(m.style_card)
    store = BatchStore(Path(out_root) / m.batch_id)
    deps = deps or default_deps()
    init: BatchState = {
        "items": {i.id: {"brief": i.brief, "attempts": 0, "candidates": [], "status": "pending", "error": None}
                  for i in m.items},
        "current": None, "budget": m.budget, "numbers": [], "selection": None, "sheet_path": None}
    with store.lock():
        if store.spec_path.exists() or store.state_db.exists():
            raise InputError("batch_exists", f"批次目录已存在：{store.root}；继续请用 ga batch resume / status")
        store.save_spec(m)
        with _opened(store.root, deps, lock=False) as (store, m, app, cfg):
            app.invoke(init, cfg)
            return _status(store, m, app, cfg, deps)


def resume_batch(batch_dir: Path, resume_value: JSON | None = None, *, deps: Deps | None = None) -> BatchStatus:
    """resume_value：None（断点续跑）、{"budget": x}、{"pick": ...} / {"pick_numbers": ...}。"""
    if resume_value is not None and not isinstance(resume_value, dict):
        raise InputError("bad_resume_value", f"恢复值应为对象：{resume_value!r}")
    deps = deps or default_deps()
    with _opened(batch_dir, deps, lock=True) as (store, m, app, cfg):
        snap = app.get_state(cfg)
        stage = _stage(snap) if snap.values else None
        if resume_value is None:
            app.invoke(None, cfg)
        else:
            if "budget" in resume_value:
                if stage != "awaiting_budget":
                    raise InputError("not_waiting_budget", f"批次当前不在预算暂停（阶段：{stage}）")
                b = resume_value["budget"]
                if isinstance(b, bool) or not isinstance(b, (int, float)) or b < 0:
                    raise InputError("bad_budget", f"预算必须是非负数：{b!r}")
            else:
                if stage != "awaiting_selection":
                    raise InputError("not_waiting_selection", f"批次当前不在等待挑选（阶段：{stage}）")
                resolve_selection(cast(BatchState, snap.values), resume_value)  # 先校验，失败不推进状态
            app.invoke(Command(resume=resume_value), cfg)
        return _status(store, m, app, cfg, deps)


def rerun_batch(batch_dir: Path, item_ids: list[str], brief: str | None = None, *,
                deps: Deps | None = None) -> BatchStatus:
    """把指定项置回 pending（attempts 清零、候选清空、可改 brief、清掉其旧选择）后继续运行。"""
    deps = deps or default_deps()
    with _opened(batch_dir, deps, lock=True) as (store, m, app, cfg):
        snap = app.get_state(cfg)
        stage = _stage(snap) if snap.values else None
        unknown = [i for i in item_ids if i not in (snap.values or {}).get("items", {})]
        if not item_ids or unknown:
            raise InputError("unknown_item", f"项不存在：{', '.join(unknown) or '（未指定）'}")
        if stage == "awaiting_selection":
            app.invoke(Command(resume={"rerun": list(item_ids), "brief": brief}), cfg)
        elif stage == "finished":
            update = _rerun_update(store, cast(BatchState, snap.values), list(item_ids), brief)
            app.update_state(cfg, update, as_node="route")
            app.invoke(None, cfg)
        else:
            raise InputError("cannot_rerun", f"只能在等待挑选或已完成时重跑（当前阶段：{stage}）")
        return _status(store, m, app, cfg, deps)


def get_status(batch_dir: Path, *, deps: Deps | None = None) -> BatchStatus:
    deps = deps or default_deps()
    with _opened(batch_dir, deps, lock=False) as (store, m, app, cfg):
        return _status(store, m, app, cfg, deps)
