"""ga 命令行入口：config check / comfy install / comfy wait / run / fix / regen / qc / final / fit / batch。

约定：stdout 只放结果（--json 时恰好一个 JSON 对象），进度与错误说明走 stderr；
退出码见全局约束（0/2/3/4/5/6/9/130）。
"""
from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import typer
from PIL import Image

from game_art import assets, imaging
from game_art.cli import preset
from game_art.comfy.client import ComfyClient
from game_art.comfy.editor import to_editor_graph
from game_art.comfy.workflow import apply_sets, find_uploads, load_workflow
from game_art.config import load_settings
from game_art.errors import BudgetExceeded, GAError, InputError
from game_art.estimate import estimate_cost
from game_art.ledger import Ledger
from game_art.models import probe_models
from game_art.paths import REPO_ROOT
from game_art.steps import feedback_lines

if TYPE_CHECKING:
    from game_art.batch.graph import BatchStatus

app = typer.Typer(help="game_art 命令行：配置检查、ComfyUI 节点安装、单次工作流运行、批量生成。",
                  no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
config_app = typer.Typer(help="配置相关命令", no_args_is_help=True)
comfy_app = typer.Typer(help="ComfyUI 相关命令", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(comfy_app, name="comfy")
batch_app = typer.Typer(help="批量生成：清单 → 候选 → 质检 → 挑选 → 定稿", no_args_is_help=True)
app.add_typer(batch_app, name="batch")

EDITOR_DIR = "editor"  # workflows/editor/：由 API 格式生成的编辑器（图）格式，供 ComfyUI 侧边栏打开
NODES_LINK = "game_art_nodes"  # 不能叫 game_art：ComfyUI 按目录名导入，会遮蔽核心包
GA_NODE_MARKER = "GACloudGenerate"


# ---------------------------------------------------------------- 公共
def _err(msg: str) -> None:
    typer.echo(msg, err=True)


def _emit_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _guarded(as_json: bool, fn: Callable[[], None]) -> None:
    """统一错误出口：GAError 按 exit_code 退出，--json 时 stdout 为错误对象。"""
    try:
        try:
            fn()
        except OSError as e:
            path = getattr(e, "filename", None)
            where = f"（路径：{path}）" if path else ""
            raise InputError("io_error", f"文件系统操作失败：{e.strerror or e}{where}",
                             {"path": str(path) if path else None, "cause": type(e).__name__}) from e
    except GAError as e:
        _err(f"错误 [{e.code}] {e.message}")
        resume = e.details.get("resume") if isinstance(e.details, dict) else None
        if resume:
            _err(f"可继续等待：{resume}")
        if as_json:
            _emit_json({"ok": False, "error": {"code": e.code, "message": e.message, "details": e.details}})
        raise typer.Exit(e.exit_code) from e
    except KeyboardInterrupt as e:
        _err("已中断（ComfyUI 中已提交的任务不会被取消）")
        if as_json:
            _emit_json({"ok": False, "error": {"code": "interrupted", "message": "已中断", "details": {}}})
        raise typer.Exit(130) from e


def _default_out(tag: str) -> Path:
    return Path("outputs") / datetime.now().strftime("%Y-%m-%d") / tag


def _collect(written: list[Path]) -> tuple[list[str], list[dict[str, Any]]]:
    files = [str(p) for p in written if p.suffix.lower() != ".json"]
    meta: list[dict[str, Any]] = []
    for p in written:
        if p.suffix.lower() == ".json":
            try:
                meta.append(json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                meta.append({"file": p.name, "error": "sidecar 不是合法 JSON"})
    return files, meta


def _report_outputs(prompt_id: str, written: list[Path], as_json: bool) -> None:
    files, meta = _collect(written)
    if as_json:
        _emit_json({"ok": True, "prompt_id": prompt_id, "files": files, "meta": meta})
    else:
        for f in files:
            typer.echo(f)


# ---------------------------------------------------------------- config check
@config_app.command("check")
def config_check(as_json: bool = typer.Option(False, "--json", help="stdout 输出单个 JSON 对象")) -> None:
    """校验配置并探活。退出码：0 全部通过；3 存在本地问题（缺密钥/密钥被拒/节点未安装）；
    4 仅存在上游不可达（provider 或 ComfyUI 连不上）。"""
    def body() -> None:
        settings = load_settings()
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, msg: str, fix: str = "", kind: str = "local") -> None:
            checks.append({"name": name, "ok": ok, "message": msg, "fix": fix, "kind": kind})

        add("配置文件", True, f"已加载，配置目录 {settings.config_dir}")
        for name, cfg in settings.providers.items():
            label = f"provider {name}"
            try:
                base_url = settings.base_url(name)
                key = settings.api_key(name)
            except InputError as e:
                var = cfg.base_url_env if e.code == "missing_base_url" else cfg.api_key_env
                add(label, False, e.message, f"设置环境变量 {var}，或写入 {settings.config_dir / '.env'}")
                continue
            kind, msg, upstream_models = probe_models(base_url, key)
            if kind == "ok":
                wanted = sorted({t.model for ts in settings.profiles.values() for t in ts if t.provider == name})
                declared = [m for m in wanted if m not in upstream_models and m in cfg.unlisted_models]
                missing = [m for m in wanted if m not in upstream_models and m not in cfg.unlisted_models]
                if missing:
                    add(label, False, f"上游模型列表里没有：{', '.join(missing)}",
                        f"用 GET {base_url}/models 核对模型名，修改 config.yaml 的 profiles 与 pricing；"
                        "若确认接口可用只是未列出，加入该 provider 的 unlisted_models")
                else:
                    note = f"（其中 {', '.join(declared)} 按 unlisted_models 声明放行）" if declared else ""
                    add(label, True, f"{base_url}/models 可访问（{msg}），配置的 {len(wanted)} 个模型都可用{note}")
            elif kind == "bad_response":
                add(label, False, f"{base_url}/models {msg}",
                    "检查 base_url：OpenAI 兼容接口一般以 /v1 结尾（如 https://aihubmix.com/v1）")
            elif kind == "auth":
                add(label, False, msg, f"检查 {cfg.api_key_env} 的值是否正确")
            else:
                add(label, False, f"{base_url}/models {msg}", "检查网络与 base_url", kind="upstream")
        client = ComfyClient(settings.comfy_url, timeout=10)
        if not client.ping():
            add("ComfyUI", False, f"无法连接 {settings.comfy_url}", "启动 ComfyUI，或修改配置中的 comfy_url",
                kind="upstream")
        else:
            add("ComfyUI", True, f"{settings.comfy_url} 可访问")
            try:
                loaded = GA_NODE_MARKER in client.object_info()
            except GAError as e:
                add("GA 节点", False, e.message, "确认 ComfyUI 运行正常", kind="upstream")
            else:
                if loaded:
                    add("GA 节点", True, f"ComfyUI 已加载 {GA_NODE_MARKER}")
                else:
                    add("GA 节点", False, f"ComfyUI 未加载 {GA_NODE_MARKER}", "运行 ga comfy install 后重启 ComfyUI")
        ok = all(c["ok"] for c in checks)
        code = 0 if ok else (3 if any(not c["ok"] and c["kind"] == "local" for c in checks) else 4)
        if as_json:
            _emit_json({"ok": ok, "checks": checks})
        else:
            for c in checks:
                typer.echo(f"{'✓' if c['ok'] else '✗'} {c['name']}：{c['message']}")
                if not c["ok"] and c["fix"]:
                    typer.echo(f"    修复：{c['fix']}")
        if code:
            raise typer.Exit(code)

    _guarded(as_json, body)


# ---------------------------------------------------------------- comfy install
def _link_state(link: Path, target: Path, legacy: Path | None = None) -> str:
    """missing | ok | legacy（指向本仓库的旧目标，可安全改指）；指向别处或不是链接则抛 InputError。"""
    if not link.is_symlink():
        if link.exists():
            raise InputError("link_conflict", f"{link} 已存在且不是符号链接，不会覆盖；请手动处理后重试",
                             {"path": str(link)})
        return "missing"
    if link.resolve() == target.resolve():
        return "ok"
    if legacy is not None and link.resolve() == legacy.resolve():
        return "legacy"
    raise InputError("link_conflict", f"{link} 已是指向 {link.resolve()} 的符号链接，不会覆盖；请手动处理后重试",
                     {"path": str(link), "points_to": str(link.resolve())})


@comfy_app.command("install")
def comfy_install(
    comfy_root: Path | None = typer.Option(None, "--comfy-root", help="ComfyUI 根目录，默认取配置中的 comfy_root"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """把 GA 节点包与工作流目录以符号链接装入 ComfyUI（幂等）。"""
    def body() -> None:
        root = (comfy_root.expanduser() if comfy_root else load_settings().comfy_root)
        if not root.is_dir():
            raise InputError("comfy_root_not_found", f"ComfyUI 根目录不存在：{root}（可用 --comfy-root 指定）")
        editor_dir = REPO_ROOT / "workflows" / EDITOR_DIR
        editor_dir.mkdir(parents=True, exist_ok=True)
        # 编辑器只能打开图格式，所以链接到 workflows/editor/；早期版本链接的是 workflows/（API 格式），自动改指
        plan: list[tuple[Path, Path, Path | None]] = [
            (root / "custom_nodes" / NODES_LINK, REPO_ROOT / "comfy_nodes", None),
            (root / "user" / "default" / "workflows" / "game_art", editor_dir, REPO_ROOT / "workflows"),
        ]
        states = [_link_state(link, target, legacy) for link, target, legacy in plan]  # 先全部检查，再动手
        labels = {"ok": "exists", "missing": "created", "legacy": "updated"}
        results: list[dict[str, str]] = []
        for (link, target, _), st in zip(plan, states, strict=True):
            if st == "legacy":
                link.unlink()
            if st != "ok":
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
            results.append({"link": str(link), "target": str(target), "status": labels[st]})
        if as_json:
            _emit_json({"ok": True, "links": results})
        else:
            for r in results:
                word = {"exists": "已存在", "created": "已创建", "updated": "已改指"}[r["status"]]
                typer.echo(f"{word}：{r['link']} -> {r['target']}")
        _err("请重启 ComfyUI 使节点生效。")

    _guarded(as_json, body)


# ---------------------------------------------------------------- comfy export-editor
@comfy_app.command("export-editor")
def comfy_export_editor(
    src: Path | None = typer.Option(None, "--src", help="API 格式工作流目录，默认仓库 workflows/"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """把 API 格式工作流转换成编辑器能打开的图格式，写入 <src>/editor/（需要 ComfyUI 在运行）。"""
    def body() -> None:
        src_dir = src.expanduser() if src else REPO_ROOT / "workflows"
        files = sorted(src_dir.glob("*.json"))
        if not files:
            raise InputError("workflow_not_found", f"{src_dir} 下没有 .json 工作流")
        info = ComfyClient(load_settings().comfy_url).object_info()
        out_dir = src_dir / EDITOR_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for f in files:
            load_workflow(f)  # 校验是 API 格式
            raw: dict[str, Any] = json.loads(f.read_text(encoding="utf-8"))  # 保留 @repo/ 前缀，节点会自行解析
            graph = to_editor_graph(raw, info, f.stem)
            dest = out_dir / f.name
            dest.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written.append(str(dest))
        if as_json:
            _emit_json({"ok": True, "written": written})
        else:
            for w in written:
                typer.echo(f"已生成：{w}")
        _err("在编辑器里刷新工作流列表即可打开；改了 API 格式工作流后请重新运行本命令。")

    _guarded(as_json, body)


# ---------------------------------------------------------------- comfy wait
@comfy_app.command("wait")
def comfy_wait(
    prompt_id: str = typer.Argument(..., help="已提交任务的 prompt_id"),
    timeout: float = typer.Option(600, "--timeout", help="最长等待秒数"),
    out: Path | None = typer.Option(None, "--out", help="输出目录，默认 ./outputs/<日期>/<prompt_id>"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """等待 ComfyUI 中已有的任务并下载其输出。"""
    def body() -> None:
        client = ComfyClient(load_settings().comfy_url)
        _err(f"等待任务 {prompt_id} …")
        entry = client.wait(prompt_id, timeout)
        written = client.download_outputs(entry, out or _default_out(prompt_id))
        _report_outputs(prompt_id, written, as_json)

    _guarded(as_json, body)


# ---------------------------------------------------------------- run
def _fill_tags(wf: dict[str, Any], tag: str) -> None:
    for node in wf.values():
        inputs = node.get("inputs", {})
        if "tag" in inputs and inputs["tag"] == "":
            inputs["tag"] = tag


def _check_cost(wf: dict[str, Any], max_cost: float | None) -> float:
    """按配置估算这次的费用；超过 --max-cost 就不提交（退出码 9）。"""
    est = estimate_cost(wf, load_settings())
    if max_cost is not None and est > max_cost:
        raise BudgetExceeded("over_max_cost", f"估算这次要花 {est:.4f} 美元，超过了 --max-cost {max_cost:g}，没有提交",
                             {"estimate": est, "max_cost": max_cost})
    return est


def _submit(client: ComfyClient, wf: dict[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
    for node_id, name, path in find_uploads(wf):
        _err(f"上传 {path} …")
        wf[node_id]["inputs"][name] = client.upload(path)
    prompt_id = client.submit(wf)
    _err(f"已提交 {prompt_id}，等待中（超时 {timeout:g}s）…")
    try:
        return prompt_id, client.wait(prompt_id, timeout)
    except KeyboardInterrupt:
        _err(f"中断；任务仍在 ComfyUI 中运行，可用 ga comfy wait {prompt_id} 继续")
        raise


def _image_row(png: Path, threshold: float | None = None) -> dict[str, Any]:
    """一张图的结果：路径、质检分数、是否及格、问题列表（带序号、必须改/建议改、细节/构图）。"""
    try:
        record = assets.read_record(png)
    except InputError:
        record = {}
    qc = assets.qc_of(record)
    return {"path": str(png), "record": str(assets.record_path(png)),
            "score": qc.get("score") if qc else None, "passed": qc.get("passed") if qc else None,
            "threshold": threshold, "issues": assets.issues_of(record), "image_cost": record.get("cost")}


def _spent(tag: str) -> float:
    return round(Ledger.from_settings(load_settings()).spent(tag), 4)


def _threshold(wf: dict[str, Any]) -> float | None:
    value = wf.get(preset.QC, {}).get("inputs", {}).get("threshold")
    return float(value) if isinstance(value, (int, float)) else None


def _print_rows(rows: list[dict[str, Any]], cost: float) -> None:
    for r in rows:
        passed = "及格" if r["passed"] else "不及格"
        verdict = "未质检" if r["score"] is None else f"{r['score']:.1f} 分，{passed}"
        typer.echo(f"{r['path']}（{verdict}）")
        for i in r["issues"]:
            level = "必须改" if i["level"] == "must" else "建议改"
            kind = "构图" if i["kind"] == "layout" else "细节"
            typer.echo(f"  {i['n']}. [{level}·{kind}] {i['text']}")
    typer.echo(f"本次估算花费 {cost:.4f} 美元")


def _report_images(prompt_id: str, tag: str, pngs: list[Path], wf: dict[str, Any], as_json: bool,
                   extra: dict[str, Any] | None = None) -> None:
    rows = [_image_row(p, _threshold(wf)) for p in pngs]
    cost = _spent(tag)
    if as_json:
        _emit_json({"ok": True, "prompt_id": prompt_id, "tag": tag, "cost": cost, "images": rows, **(extra or {})})
    else:
        _print_rows(rows, cost)


def _mark_preset(pngs: list[Path], preset_path: Path) -> None:
    """在新图的记录里记下用的是哪个预设，之后 fix / regen / qc 不用再写 --preset。"""
    for png in pngs:
        try:
            record = assets.read_record(png)
        except InputError:
            continue
        record["preset"] = str(preset_path)
        assets.write_record(png, record)


def _resolve_preset(given: Path | None, record: dict[str, Any], png: Path) -> Path:
    if given is not None:
        return given.expanduser().resolve()
    if isinstance(record.get("preset"), str) and record["preset"]:
        return Path(record["preset"])
    raise InputError("no_preset", (
        f"不知道 {png.name} 是用哪个预设出的（记录里没有 preset，可能是在编辑器里出的）："
        "请加 --preset <预设.json>"))


def _run_preset(client: ComfyClient, wf: dict[str, Any], preset_path: Path, *, tag: str, timeout: float,
                max_cost: float | None, dry_run: bool, as_json: bool, extra: dict[str, Any] | None = None) -> None:
    """预设类命令的公共部分：补 tag → 估算费用 → 提交 → 等待 → 记下预设 → 报告新图。"""
    _fill_tags(wf, tag)
    _err(f"估算费用 {_check_cost(wf, max_cost):.4f} 美元")
    if dry_run:
        _emit_json(wf)
        return
    prompt_id, entry = _submit(client, wf, timeout)
    pngs = preset.saved_files(entry)
    if not pngs:
        raise InputError("nothing_saved", (
            "这次运行没有保存任何图片：确认预设里有「保存资产」节点、ComfyUI 里的节点是最新的"))
    _mark_preset(pngs, preset_path)
    _report_images(prompt_id, tag, pngs, wf, as_json, extra)


def _with_item(wf: dict[str, Any], item: str | None, sets: list[str]) -> dict[str, Any]:
    """先套 --set（可能设了 briefs_path），再按 --item 选一项，最后再套一遍 --set，让手写的值优先。"""
    wf = apply_sets(wf, sets)
    if not item:
        return wf
    preset.apply_item(wf, item)
    return apply_sets(wf, sets)


def _new_tag(kind: str) -> str:
    return f"{kind}/{uuid.uuid4().hex[:8]}"


@app.command("run")
def run(
    workflow: Path | None = typer.Argument(None, help="API 格式工作流 JSON；用 --preset 时不写"),
    preset_path: Path | None = typer.Option(None, "--preset", help=(
        "项目的预设工作流（编辑器里保存的 .json）。出来的图和在编辑器里点运行一样，存到预设里的保存位置")),
    item: str | None = typer.Option(None, "--item", help="描述清单里的哪一项，和编辑器里点 item 选一项一样"),
    sets: list[str] = typer.Option([], "--set", help="<节点>.<输入>=<值>，可重复"),
    out: Path | None = typer.Option(None, "--out", help="输出目录，默认 ./outputs/<日期>/<tag>（--preset 时不用）"),
    timeout: float = typer.Option(600, "--timeout", help="最长等待秒数（超时不取消任务）"),
    tag: str | None = typer.Option(None, "--tag", help="自动填入工作流中空的 tag 输入，默认 run/<8位随机>"),
    max_cost: float | None = typer.Option(None, "--max-cost", help="估算费用超过这么多美元就不提交（退出码 9）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印补丁后的工作流，不提交、不花钱"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """运行单个工作流：上传 @upload{} → 补 tag → 提交 → 等待 → 下载。用 --preset 时直接存到预设的保存位置。"""
    if (workflow is None) == (preset_path is None):
        raise typer.BadParameter("要么写 API 格式工作流的路径，要么用 --preset 指定预设，二选一")

    def body() -> None:
        run_tag = tag or _new_tag("run")
        if preset_path is not None:
            client = ComfyClient(load_settings().comfy_url)
            wf = _with_item(preset.load_preset(preset_path, client.object_info()), item, sets)
            _run_preset(client, wf, preset_path.expanduser().resolve(), tag=run_tag, timeout=timeout,
                        max_cost=max_cost, dry_run=dry_run, as_json=as_json)
            return
        assert workflow is not None
        wf = _with_item(load_workflow(workflow), item, sets)
        _fill_tags(wf, run_tag)
        _err(f"估算费用 {_check_cost(wf, max_cost):.4f} 美元")
        if dry_run:
            _emit_json(wf)
            return
        client = ComfyClient(load_settings().comfy_url)
        prompt_id, entry = _submit(client, wf, timeout)
        written = client.download_outputs(entry, out or _default_out(run_tag))
        files, meta = _collect(written)
        rows = [_image_row(Path(f), _threshold(wf)) for f in files if f.lower().endswith(".png")]
        cost = _spent(run_tag)
        if as_json:
            _emit_json({"ok": True, "prompt_id": prompt_id, "tag": run_tag, "cost": cost, "files": files,
                        "meta": meta, "images": rows})
        else:
            for f in files:
                typer.echo(f)

    _guarded(as_json, body)


# ---------------------------------------------------------------- fix / regen / qc / final（逐张出图和修图）
PRESET_OPT = typer.Option(None, "--preset", help=(
    "预设工作流；不写时用这张图记录里的 preset（ga run --preset 出的图都有）"))
ISSUE_OPT = typer.Option([], "--issue", help=(
    "选这张图质检结果里的第几条问题（就是 .json 里 qc.details 的顺序，从 1 数），可重复"))
NOTE_OPT = typer.Option([], "--note", help="我的补充：质检没看出来、但想改的地方，可重复")
TIMEOUT_OPT = typer.Option(600, "--timeout", help="最长等待秒数（超时不取消任务）")
MAX_COST_OPT = typer.Option(None, "--max-cost", help="估算费用超过这么多美元就不提交（退出码 9）")
DRY_RUN_OPT = typer.Option(False, "--dry-run", help="只打印要提交的工作流，不提交、不花钱")


def _load_for(image: Path, preset_opt: Path | None) -> tuple[Path, dict[str, Any], Path, ComfyClient,
                                                             dict[str, Any]]:
    png = assets.checked_asset(image)
    record = assets.read_record(png)
    preset_path = _resolve_preset(preset_opt, record, png)
    client = ComfyClient(load_settings().comfy_url)
    wf = preset.load_preset(preset_path, client.object_info())
    return png, record, preset_path, client, wf


@app.command("fix")
def fix(
    image: Path = typer.Argument(..., help="要修的候选图（.png，旁边要有同名 .json）"),
    issues: list[int] = ISSUE_OPT,
    notes: list[str] = NOTE_OPT,
    preset_path: Path | None = PRESET_OPT,
    refs: bool | None = typer.Option(None, "--refs/--no-refs", help=(
        "带不带预设里的风格参考图作画风对照，默认按预设（一般是带）")),
    edit_profile: str | None = typer.Option(None, "--edit-profile", help="改图用的模型组，默认按预设"),
    edit_model: str | None = typer.Option(None, "--edit-model", help="直接指定改图模型（服务商:模型名），默认按预设"),
    quality: str | None = typer.Option(None, "--quality", help="画质档 high / medium / low / 跟随配置，默认按预设"),
    fidelity: str | None = typer.Option(None, "--fidelity", help="保真度 high / low / 跟随配置，默认按预设"),
    timeout: float = TIMEOUT_OPT,
    max_cost: float | None = MAX_COST_OPT,
    dry_run: bool = DRY_RUN_OPT,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """在原图上修（对应编辑器的「修这张」）：只修选中的细节类问题（最多 3 条）和补充，其余基本保留。
    结果存为下一个编号的新候选，原图不动；修完自动质检一次。"""
    def body() -> None:
        png, record, preset_path_, client, wf = _load_for(image, preset_path)
        wf = preset.prepare_fix(wf, png, record, issues, notes, with_refs=refs, edit_profile=edit_profile,
                                edit_model=edit_model, quality=quality, fidelity=fidelity)
        _run_preset(client, wf, preset_path_, tag=_new_tag("fix"), timeout=timeout, max_cost=max_cost,
                    dry_run=dry_run, as_json=as_json, extra={"from": str(png)})

    _guarded(as_json, body)


@app.command("regen")
def regen(
    image: Path = typer.Argument(..., help="参照的候选图（.png，旁边要有同名 .json）"),
    issues: list[int] = ISSUE_OPT,
    notes: list[str] = NOTE_OPT,
    reset: bool = typer.Option(False, "--reset", help="清空之前累积的修订意见，只用这次选的"),
    preset_path: Path | None = PRESET_OPT,
    timeout: float = TIMEOUT_OPT,
    max_cost: float | None = MAX_COST_OPT,
    dry_run: bool = DRY_RUN_OPT,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """整张重出（对应编辑器的「采纳并重新生成」）：用这张图的描述，带上选中的问题（细节类、构图类都行）重新出图并质检。
    修订意见逐轮累积，新图 .json 的 feedback 里能看到累积了哪些。"""
    def body() -> None:
        png, record, preset_path_, client, wf = _load_for(image, preset_path)
        wf = preset.prepare_regen(wf, png, record, issues, notes, reset=reset)
        feedback = feedback_lines(wf[preset.EXPAND]["inputs"]["feedback"])
        _run_preset(client, wf, preset_path_, tag=_new_tag("regen"), timeout=timeout, max_cost=max_cost,
                    dry_run=dry_run, as_json=as_json, extra={"from": str(png), "feedback": feedback})

    _guarded(as_json, body)


@app.command("qc")
def qc(
    image: Path = typer.Argument(..., help="要质检的图（.png，旁边要有同名 .json；手动修过的也可以）"),
    preset_path: Path | None = PRESET_OPT,
    timeout: float = TIMEOUT_OPT,
    max_cost: float | None = MAX_COST_OPT,
    dry_run: bool = DRY_RUN_OPT,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """按这张图 .json 里的描述和预设的风格卡重新质检，结果写回 .json 的 qc。不出图、不改图。"""
    def body() -> None:
        png, record, preset_path_, client, wf = _load_for(image, preset_path)
        wf = preset.prepare_qc(wf, png)
        tag = _new_tag("qc")
        _fill_tags(wf, tag)
        _err(f"估算费用 {_check_cost(wf, max_cost):.4f} 美元")
        if dry_run:
            _emit_json(wf)
            return
        prompt_id, entry = _submit(client, wf, timeout)
        record = assets.read_record(png)  # 等待期间可能被别处改过，写回前重新读
        record["qc"] = preset.qc_result(entry)
        record.setdefault("preset", str(preset_path_))
        assets.write_record(png, record)
        _report_images(prompt_id, tag, [png], wf, as_json)

    _guarded(as_json, body)


@app.command("final")
def final(
    image: Path = typer.Argument(..., help="选定的候选图（.png，旁边要有同名 .json）"),
    name: str | None = typer.Option(None, "--name", help=(
        "定稿名，默认是候选图去掉编号的部分（archer_lv1_00002 → archer_lv1）")),
    force: bool = typer.Option(False, "--force", help="定稿已存在时替换它（旧定稿先备份到 _final_backup/）"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """定稿（对应编辑器的「保存为定稿」）：把图和 .json 拷成同目录下的 <name>.png / <name>.json。候选图不删。不花钱。"""
    def body() -> None:
        png = assets.checked_asset(image)
        dest, backup = assets.finalize(png, name or assets.base_name(png), force=force)
        if as_json:
            _emit_json({"ok": True, "path": str(dest), "record": str(assets.record_path(dest)), "from": str(png),
                        "backup": str(backup) if backup else None, "cost": 0.0})
        else:
            typer.echo(str(dest))
            if backup:
                _err(f"旧定稿已备份到 {backup}")

    _guarded(as_json, body)


# ---------------------------------------------------------------- fit
FIT_BACKUP = "_fit_backup"


def _fit_targets(paths: list[Path]) -> list[Path]:
    """文件照原样收下；文件夹取里面的 PNG（不往子文件夹里找）。"""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() == ".png" and f.is_file())
        elif p.is_file():
            out.append(p)
        else:
            raise InputError("not_found", f"找不到：{p}")
    return out


def fit_file(path: Path, ratio: float, tolerance: float, backup: bool, dry_run: bool) -> dict[str, Any]:
    """量一张图的主体占比，超出允许范围就在原位置改掉它。返回这张图的处理结果。"""
    with Image.open(path) as opened:
        img = opened.copy()
    before = imaging.subject_ratio(img)
    row: dict[str, Any] = {"file": str(path), "before": before, "after": before, "action": "skip"}
    if before is None:
        row["reason"] = "没有透明底，量不出主体"
        return row
    goal = imaging.subject_target(img, ratio) or ratio
    if abs(before - goal) <= tolerance:
        row["reason"] = "已经合适" if goal >= ratio - 0.005 else "主体较宽，左右已经快到画面边缘"
        return row
    fitted = imaging.fit_subject(img, ratio)
    row.update(after=imaging.subject_ratio(fitted), action="would_fit" if dry_run else "fit")
    if dry_run:
        return row
    record = path.with_suffix(".json")
    if backup:
        keep = path.parent / FIT_BACKUP
        keep.mkdir(exist_ok=True)
        for f in (path, record):
            if f.is_file() and not (keep / f.name).exists():  # 已经有备份（之前调过一次）：留着最早的那份
                shutil.copy2(f, keep / f.name)
    fitted.save(path, format="PNG")
    if record.is_file():
        try:
            meta = json.loads(record.read_text(encoding="utf-8"))
        except ValueError:
            meta = None
        if isinstance(meta, dict):
            meta["resize"] = {"from": round(before, 3), "to": round(row["after"] or 0, 3)}
            record.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return row


@app.command("fit")
def fit(
    paths: list[Path] = typer.Argument(..., help="PNG 文件或文件夹，可以写多个；文件夹只处理里面的 PNG"),
    ratio: float = typer.Option(imaging.DEFAULT_SUBJECT_RATIO, "--ratio", min=0.3, max=0.95,
                                help="主体应该占画面高度的几成"),
    tolerance: float = typer.Option(imaging.SUBJECT_TOLERANCE, "--tolerance", min=0.0, max=0.3,
                                    help="离目标差多少以内不动（缩放会损失一点清晰度，差得不多就不值得改）"),
    backup: bool = typer.Option(True, "--backup/--no-backup",
                                help=f"改之前把原图和记录复制到同目录的 {FIT_BACKUP}/"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只量、只列出会改哪些，不动文件"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """统一主体大小：把透明底图片的主体等比例缩放到占画面高度的 ratio 并居中。本机处理，不调模型、不花钱。"""
    def body() -> None:
        rows = [fit_file(p, ratio, tolerance, backup, dry_run) for p in _fit_targets(paths)]
        if as_json:
            _emit_json({"ratio": ratio, "tolerance": tolerance, "dry_run": dry_run, "files": rows})
            return
        pct: Callable[[Any], str] = lambda v: "  —" if v is None else f"{v:4.0%}"  # noqa: E731
        for r in rows:
            done = {"fit": "已调整", "would_fit": "会调整", "skip": "不动"}[r["action"]]
            typer.echo(f"{pct(r['before'])} → {pct(r['after'])}  {done}  {r['file']}"
                       + (f"（{r['reason']}）" if r.get("reason") else ""))
        changed = sum(r["action"] != "skip" for r in rows)
        _err(f"共 {len(rows)} 张，{'会调整' if dry_run else '调整了'} {changed} 张，目标 {ratio:.0%} ± {tolerance:.0%}")

    _guarded(as_json, body)


# ---------------------------------------------------------------- batch
def _batch_graph() -> ModuleType:
    from game_art.batch import graph  # 延迟导入：langgraph 只在批量命令里用到
    return graph


_STAGE_TEXT = {"awaiting_selection": "等待挑选", "awaiting_budget": "预算超限，已暂停",
               "interrupted": "中断（可继续）", "finished": "已完成"}


def _batch_call(as_json: bool, batch_dir: Path | None, fn: Callable[[], BatchStatus]) -> None:
    """运行批量操作并输出状态；预算暂停时退出码 9。"""
    def body() -> None:
        try:
            st = fn()
        except KeyboardInterrupt:
            if batch_dir is not None:
                _err(f"可用 ga batch resume {batch_dir} 从断点继续")
            raise
        if as_json:
            _emit_json({"ok": True, **st.model_dump()})
        else:
            typer.echo(f"批次 {st.batch_id}：{_STAGE_TEXT[st.stage]}（stage={st.stage}）")
            typer.echo(f"目录：{st.batch_dir}")
            budget = f" / 预算 {st.budget:.4f}" if st.budget is not None else ""
            typer.echo(f"累计费用：{st.cost:.4f}{budget}")
            for it in st.items:
                err = f"  错误：{it.error}" if it.error else ""
                typer.echo(f"  {it.id}  {it.status}  尝试 {it.attempts} 次  候选 {it.candidates}{err}")
            if st.numbers:
                typer.echo(f"拼图：{st.sheet_path}")
                for n in st.numbers:
                    score = f"{n.score:.1f}" if n.score is not None else "-"
                    typer.echo(f"  #{n.number}  {n.item_id}  质检 {score}  {n.path}")
            if st.manifest_path:
                typer.echo(f"最终清单：{st.manifest_path}")
            if st.next_command:
                typer.echo(f"下一步：{st.next_command}")
        if st.stage == "awaiting_budget":
            raise typer.Exit(9)

    _guarded(as_json, body)


@batch_app.command("start")
def batch_start(
    manifest: Path = typer.Argument(..., help="批次清单 YAML"),
    out: Path = typer.Option(Path("outputs") / "batches", "--out", help="批次根目录，批次目录为 <out>/<batch_id>"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """开始一个批次：逐项生成候选并自动质检/重跑，然后暂停等待挑选。"""
    _batch_call(as_json, None, lambda: _batch_graph().start_batch(manifest, out))


@batch_app.command("status")
def batch_status(batch_dir: Path = typer.Argument(..., help="批次目录"),
                 as_json: bool = typer.Option(False, "--json")) -> None:
    """查看批次阶段、各项状态、候选编号与费用。"""
    _batch_call(as_json, batch_dir, lambda: _batch_graph().get_status(batch_dir))


def _parse_item_picks(specs: list[str]) -> dict[str, list[int]]:
    pick: dict[str, list[int]] = {}
    for spec in specs:
        item_id, sep, nums = spec.partition("=")
        try:
            values = [int(x) for x in nums.split(",") if x.strip()]
        except ValueError:
            values = None
        if not sep or not item_id or not values:
            raise InputError("bad_item_pick", f"--item 格式应为 <项>=<编号,...>（如 coin=1,2）：{spec!r}")
        pick.setdefault(item_id, []).extend(values)
    return pick


@batch_app.command("select")
def batch_select(
    batch_dir: Path = typer.Argument(..., help="批次目录"),
    numbers: list[int] | None = typer.Argument(None, help="拼图上的全局编号，可多个"),
    items: list[str] = typer.Option([], "--item", help="按项挑选：<项>=<编号,...>，可重复"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """按编号挑选候选并定稿；编号无效或有项未选时拒绝且不改变状态。"""
    def run() -> BatchStatus:
        value: dict[str, Any] = {"pick_numbers": list(numbers or [])}
        pick = _parse_item_picks(items)
        if pick:
            value["pick"] = pick
        result: BatchStatus = _batch_graph().resume_batch(batch_dir, value)
        return result

    _batch_call(as_json, batch_dir, run)


@batch_app.command("rerun")
def batch_rerun(
    batch_dir: Path = typer.Argument(..., help="批次目录"),
    item_ids: list[str] = typer.Argument(..., help="要重跑的项 id"),
    brief: str | None = typer.Option(None, "--brief", help="新的描述（应用到所有指定项）"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """把指定项重置为待生成（尝试次数清零）后继续运行；旧图移到 archive/。"""
    _batch_call(as_json, batch_dir, lambda: _batch_graph().rerun_batch(batch_dir, item_ids, brief))


@batch_app.command("resume")
def batch_resume(
    batch_dir: Path = typer.Argument(..., help="批次目录"),
    budget: float | None = typer.Option(None, "--budget", help="预算暂停时设置新的预算上限"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """从断点继续（进程被杀后），或在预算暂停时用 --budget 提高预算后继续。"""
    value = {"budget": budget} if budget is not None else None
    _batch_call(as_json, batch_dir, lambda: _batch_graph().resume_batch(batch_dir, value))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
