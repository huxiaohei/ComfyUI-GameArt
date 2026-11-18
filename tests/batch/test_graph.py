import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from game_art.batch import graph as G
from game_art.batch.store import BatchStore
from game_art.cli import app as cli
from game_art.config import Pricing, Settings
from game_art.errors import ExecutionError, GAError, InputError, TimeoutError_, UpstreamError
from game_art.ledger import Ledger
from game_art.paths import REPO_ROOT
from game_art.steps import QCResult
from game_art.style import StyleCard

JSON = dict[str, Any]

runner = CliRunner()
WF = REPO_ROOT / "workflows" / "text_to_asset.json"
CARD = REPO_ROOT / "examples" / "style_cards" / "guochao.yaml"


class FakeClient:
    """记录提交；成功时按 tag 记账；下载时写小 PNG + sidecar。

    server 模拟 ComfyUI 端已知的任务（pid → 工作流），可在多个客户端（多个进程）间共享。
    """

    def __init__(self, ledger: Ledger, server: dict[str, JSON] | None = None) -> None:
        self.ledger = ledger
        self.server: dict[str, JSON] = {} if server is None else server
        self.submissions: list[str] = []
        self.workflows: list[JSON] = []
        self.waits: list[str] = []
        self.crash_on: int | None = None
        self.crash_on_wait: int | None = None
        self.fail_items: set[str] = set()

    def upload(self, path: Path | str) -> str:
        return Path(path).name

    def submit(self, wf: JSON) -> str:
        tag = wf["gen"]["inputs"]["tag"]
        self.submissions.append(tag)
        self.workflows.append(wf)
        if self.crash_on and len(self.submissions) == self.crash_on:
            raise KeyboardInterrupt
        pid = f"pid{len(self.server) + 1}"
        self.server[pid] = wf
        return pid

    def queue_contains(self, pid: str) -> bool:
        return pid in self.server

    def history(self, pid: str) -> JSON:
        return {}

    def wait(self, pid: str, timeout: float) -> JSON:
        self.waits.append(pid)
        if self.crash_on_wait and len(self.waits) == self.crash_on_wait:
            raise KeyboardInterrupt
        wf = self.server[pid]
        item = wf["gen"]["inputs"]["tag"].split("/")[1]
        if item in self.fail_items:
            raise ExecutionError("comfy_execution_failed", "节点 gen 执行失败：boom")
        self.ledger.record(tag=wf["gen"]["inputs"]["tag"], provider="p", model="m", kind="generate",
                           images=int(wf["gen"]["inputs"]["n"]), ok=True)  # 成功才计费
        return {"wf": wf}

    def download_outputs(self, entry: JSON, dest: Path) -> list[Path]:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for i in range(int(entry["wf"]["gen"]["inputs"]["n"])):
            png = dest / f"asset_{i:05d}.png"
            Image.new("RGBA", (8, 8), (200, 50, 50, 255)).save(png)
            js = png.with_suffix(".json")
            js.write_text(json.dumps({"tag": entry["wf"]["gen"]["inputs"]["tag"], "seed": i}))
            out += [png, js]
        return out


class FakeQC:
    def __init__(self) -> None:
        self.score: float | Callable[[str], float] = 8.0  # float，或 callable(tag) -> float
        self.calls: list[str] = []

    def __call__(self, router: Any, img: Image.Image, *, brief: str, style: StyleCard | None, threshold: float,
                 tag: str) -> QCResult:
        self.calls.append(tag)
        s = self.score(tag) if callable(self.score) else self.score
        return QCResult(score=s, issues=[] if s >= threshold else ["不合格"], passed=s >= threshold)


class Env:
    def __init__(self, tmp_path: Path, server: dict[str, JSON] | None = None) -> None:
        self.tmp = tmp_path
        self.out = tmp_path / "out"
        settings = Settings(comfy_root=tmp_path, config_dir=tmp_path, data_dir=tmp_path / "data")
        self.ledger = Ledger(tmp_path / "data" / "ledger.jsonl", {"p:m": Pricing(per_image=0.02)})
        self.client = FakeClient(self.ledger, server)
        self.qc = FakeQC()
        self.deps = G.Deps(settings=settings, client=self.client, ledger=self.ledger, router=object(),
                           qc=self.qc)

    def manifest(self, n_items: int = 3, **kw: Any) -> Path:
        data: dict[str, Any] = {"batch_id": "b1", "workflow": str(WF), "style_card": str(CARD), "candidates": 2,
                "items": [{"id": f"i{k}", "brief": f"描述{k}"} for k in range(1, n_items + 1)]}
        data.update(kw)
        p = self.tmp / "m.yaml"
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return p

    @property
    def dir(self) -> Path:
        return self.out / "b1"

    def start(self, **kw: Any) -> G.BatchStatus:
        return G.start_batch(self.manifest(**kw), self.out, deps=self.deps)

    def resume(self, value: JSON | None = None) -> G.BatchStatus:
        return G.resume_batch(self.dir, value, deps=self.deps)


@pytest.fixture
def fake_env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _items(st: G.BatchStatus) -> dict[str, G.ItemStatus]:
    return {i.id: i for i in st.items}


def test_happy_path_pauses_at_selection(fake_env: Env) -> None:
    st = fake_env.start()
    assert st.stage == "awaiting_selection"
    assert st.sheet_path and Path(st.sheet_path).is_file()
    assert [n.number for n in st.numbers] == [1, 2, 3, 4, 5, 6]
    assert [(n.item_id, n.label) for n in st.numbers[:2]] == [("i1", "i1#1"), ("i1", "i1#2")]
    assert all(i.status == "generated" and i.candidates == 2 for i in st.items)
    assert st.cost == pytest.approx(0.12)
    assert "ga batch select" in (st.next_command or "")
    wf = fake_env.client.workflows[0]
    assert wf["expand"]["inputs"]["brief"] == "描述1" and wf["gen"]["inputs"]["n"] == 2
    assert wf["save"]["inputs"]["subfolder"] == "ga/b1/i1/1"
    assert wf["expand"]["inputs"]["tag"] == "b1/i1/1" and wf["gen"]["inputs"]["tag"] == "b1/i1/1"
    assert wf["style"]["inputs"]["path"] == str(CARD)
    assert fake_env.qc.calls == ["b1/i1/1"] * 2 + ["b1/i2/1"] * 2 + ["b1/i3/1"] * 2
    # 状态查询稳定
    again = G.get_status(fake_env.dir, deps=fake_env.deps)
    assert again.numbers == st.numbers and again.stage == "awaiting_selection"


def test_defaults_then_item_sets_applied(fake_env: Env) -> None:
    fake_env.start(n_items=2, defaults={"gen.size": "512x512", "gen.seed": 1},
                   items=[{"id": "i1", "brief": "a", "sets": {"gen.seed": 7}}, {"id": "i2", "brief": "b"}])
    w1, w2 = fake_env.client.workflows
    assert w1["gen"]["inputs"]["size"] == "512x512" and w1["gen"]["inputs"]["seed"] == 7
    assert w2["gen"]["inputs"]["seed"] == 1


def test_low_qc_retries_then_needs_review(fake_env: Env) -> None:
    fake_env.qc.score = 5.0
    st = fake_env.start(n_items=1, max_retries=2)
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i1/2", "b1/i1/3"]
    it = _items(st)["i1"]
    assert it.status == "needs_review" and it.attempts == 3
    assert st.stage == "awaiting_selection" and st.numbers == []
    assert (fake_env.dir / "items" / "i1" / "3").is_dir()


def test_retry_succeeds_on_second_attempt(fake_env: Env) -> None:
    fake_env.qc.score = lambda tag: 4.0 if tag.endswith("/1") else 9.0
    st = fake_env.start(n_items=1)
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i1/2"]
    assert _items(st)["i1"].status == "generated" and _items(st)["i1"].attempts == 2


def test_one_item_failure_does_not_stop_batch(fake_env: Env) -> None:
    fake_env.client.fail_items = {"i2"}
    st = fake_env.start(items=[{"id": "i1", "brief": "a"}, {"id": "i2", "brief": "b"},
                               {"id": "i3", "brief": "c", "sets": {"nope.x": 1}}, {"id": "i4", "brief": "d"}])
    items = _items(st)
    assert items["i1"].status == "generated" and items["i4"].status == "generated"
    assert items["i2"].status == "failed" and "boom" in (items["i2"].error or "")
    assert items["i3"].status == "failed" and "nope" in (items["i3"].error or "")
    assert [s.split("/")[1] for s in fake_env.client.submissions] == ["i1", "i2", "i4"]
    assert st.stage == "awaiting_selection"
    assert [n.item_id for n in st.numbers] == ["i1", "i1", "i4", "i4"]


def _db_bytes(env: Env) -> bytes:
    return BatchStore(env.dir).state_db.read_bytes()


def test_select_invalid_number_rejected_state_unchanged(fake_env: Env) -> None:
    fake_env.start()
    before = _db_bytes(fake_env)
    with pytest.raises(InputError) as ei:
        fake_env.resume({"pick_numbers": [1, 3, 5, 99]})
    assert "99" in ei.value.message
    assert _db_bytes(fake_env) == before
    assert G.get_status(fake_env.dir, deps=fake_env.deps).stage == "awaiting_selection"


def test_select_missing_item_rejected(fake_env: Env) -> None:
    fake_env.start()
    with pytest.raises(InputError) as ei:
        fake_env.resume({"pick_numbers": [1, 3]})
    assert "i3" in ei.value.message and "未选" in ei.value.message
    with pytest.raises(InputError) as ei:
        fake_env.resume({"pick": {"i1": [1], "i2": [3], "i3": [1]}})  # 1 不属于 i3
    assert "i3" in ei.value.message
    assert G.get_status(fake_env.dir, deps=fake_env.deps).stage == "awaiting_selection"


def test_select_failed_item_rejected(fake_env: Env) -> None:
    fake_env.client.fail_items = {"i2"}
    fake_env.start()
    with pytest.raises(InputError) as ei:
        fake_env.resume({"pick": {"i1": [1], "i2": [1], "i3": [3]}})
    assert "i2" in ei.value.message


def test_wrong_resume_kind_rejected(fake_env: Env) -> None:
    fake_env.start()
    with pytest.raises(InputError):
        fake_env.resume({"budget": 5})


def test_finalize_writes_final_and_manifest(fake_env: Env) -> None:
    fake_env.client.fail_items = {"i3"}
    fake_env.start()
    st = fake_env.resume({"pick_numbers": [1, 2, 3]})
    assert st.stage == "finished"
    final = fake_env.dir / "final"
    assert sorted(p.name for p in final.glob("*.png")) == ["i1_1.png", "i1_2.png", "i2.png"]
    assert (final / "i2.json").is_file()
    side = json.loads((final / "i2.json").read_text(encoding="utf-8"))
    assert side["meta"]["tag"] == "b1/i2/1" and side["qc"]["score"] == 8.0
    m = json.loads((fake_env.dir / "manifest.json").read_text(encoding="utf-8"))
    assert m["cost"] == pytest.approx(0.08)
    by = {i["id"]: i for i in m["items"]}
    assert by["i1"]["status"] == "selected" and len(by["i1"]["files"]) == 2
    assert by["i2"]["cost"] == pytest.approx(0.04) and by["i2"]["files"][0]["qc"]["score"] == 8.0
    assert by["i3"]["status"] == "failed" and "boom" in by["i3"]["error"]
    assert st.manifest_path == str(fake_env.dir / "manifest.json")


def test_resume_after_crash_does_not_resubmit_done_items(fake_env: Env) -> None:
    fake_env.client.crash_on = 3
    with pytest.raises(KeyboardInterrupt):
        fake_env.start()
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i2/1", "b1/i3/1"]
    assert G.get_status(fake_env.dir, deps=fake_env.deps).stage == "interrupted"
    # 全新的图实例与客户端（模拟新进程）
    fresh = Env(fake_env.tmp)
    st = G.resume_batch(fresh.dir, None, deps=fresh.deps)
    assert fresh.client.submissions == ["b1/i3/1"]
    assert st.stage == "awaiting_selection" and len(st.numbers) == 6


def test_downloaded_attempt_reused_without_resubmit(fake_env: Env) -> None:
    d = fake_env.dir / "items" / "i1" / "1"
    d.mkdir(parents=True)
    Image.new("RGBA", (8, 8)).save(d / "x.png")
    st = fake_env.start(n_items=2)
    assert fake_env.client.submissions == ["b1/i2/1"]
    assert _items(st)["i1"].candidates == 1 and _items(st)["i1"].status == "generated"


def test_budget_pause_and_resume_with_new_budget(fake_env: Env) -> None:
    st = fake_env.start(budget=0.05)  # 每项 0.04：第 3 项前已花 0.08 > 0.05
    assert st.stage == "awaiting_budget"
    assert st.waiting == {"reason": "budget", "spent": pytest.approx(0.08), "budget": 0.05}
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i2/1"]
    assert "--budget" in (st.next_command or "")
    with pytest.raises(InputError):
        fake_env.resume({"pick_numbers": [1]})
    assert fake_env.resume(None).stage == "awaiting_budget"  # 不给新预算：仍暂停，不提交
    assert len(fake_env.client.submissions) == 2
    st = fake_env.resume({"budget": 1.0})
    assert st.stage == "awaiting_selection" and st.budget == 1.0
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i2/1", "b1/i3/1"]


def test_budget_prefix_does_not_match_other_batches(fake_env: Env) -> None:
    fake_env.ledger.record(tag="b10/x/1", provider="p", model="m", kind="generate", images=100, ok=True)
    assert fake_env.start(budget=0.05).stage == "awaiting_budget"  # 只算 b1/ 的 0.08
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i2/1"]


def test_rerun_item_with_new_brief(fake_env: Env) -> None:
    fake_env.qc.score = 5.0
    st = fake_env.start(n_items=2, max_retries=0)
    assert all(i.status == "needs_review" for i in st.items)
    fake_env.qc.score = 8.0
    st = G.rerun_batch(fake_env.dir, ["i2"], brief="新描述", deps=fake_env.deps)
    assert fake_env.client.submissions[-1] == "b1/i2/2"  # 重跑接着已归档的最大编号，tag 不重复
    assert fake_env.client.workflows[-1]["expand"]["inputs"]["brief"] == "新描述"
    items = _items(st)
    assert items["i2"].status == "generated" and items["i2"].brief == "新描述" and items["i2"].attempts == 1
    assert items["i1"].status == "needs_review"
    assert st.stage == "awaiting_selection" and [n.item_id for n in st.numbers] == ["i2", "i2"]
    st = fake_env.resume({"pick_numbers": [2]})
    assert st.stage == "finished"
    # 结束后还能重跑，旧选择被清掉
    st = G.rerun_batch(fake_env.dir, ["i2"], deps=fake_env.deps)
    assert st.stage == "awaiting_selection" and _items(st)["i2"].brief == "新描述"
    st = fake_env.resume({"pick_numbers": [1]})
    assert st.stage == "finished" and (fake_env.dir / "final" / "i2.png").is_file()


def test_rerun_unknown_item_rejected(fake_env: Env) -> None:
    fake_env.start(n_items=1)
    with pytest.raises(InputError):
        G.rerun_batch(fake_env.dir, ["zzz"], deps=fake_env.deps)


def test_start_twice_rejected(fake_env: Env) -> None:
    fake_env.start(n_items=1)
    with pytest.raises(InputError):
        fake_env.start(n_items=1)


# ---------------------------------------------------------------- CLI
@pytest.fixture
def cli_env(fake_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(G, "default_deps", lambda: fake_env.deps)
    return fake_env


def test_cli_select_numbers(cli_env: Env) -> None:
    m = cli_env.manifest()
    r = runner.invoke(cli.app, ["batch", "start", str(m), "--out", str(cli_env.out), "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.stdout)
    assert d["stage"] == "awaiting_selection" and "ga batch select" in d["next_command"]
    r = runner.invoke(cli.app, ["batch", "select", str(cli_env.dir), "1", "99", "--json"])
    assert r.exit_code == 3 and json.loads(r.stdout)["ok"] is False
    r = runner.invoke(cli.app, ["batch", "select", str(cli_env.dir), "1", "3", "--item", "i3=5,6", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["stage"] == "finished"
    assert sorted(p.name for p in (cli_env.dir / "final").glob("*.png")) == ["i1.png", "i2.png", "i3_1.png", "i3_2.png"]


def test_cli_budget_pause_exit_9_then_resume(cli_env: Env) -> None:
    m = cli_env.manifest(budget=0.05)
    r = runner.invoke(cli.app, ["batch", "start", str(m), "--out", str(cli_env.out)])
    assert r.exit_code == 9
    assert "awaiting_budget" in r.stdout and "--budget" in r.stdout
    r = runner.invoke(cli.app, ["batch", "status", str(cli_env.dir), "--json"])
    assert r.exit_code == 9 and json.loads(r.stdout)["stage"] == "awaiting_budget"
    r = runner.invoke(cli.app, ["batch", "resume", str(cli_env.dir), "--budget", "1", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["stage"] == "awaiting_selection"


def test_cli_rerun_and_bad_item_arg(cli_env: Env) -> None:
    m = cli_env.manifest(n_items=1)
    runner.invoke(cli.app, ["batch", "start", str(m), "--out", str(cli_env.out)])
    r = runner.invoke(cli.app, ["batch", "select", str(cli_env.dir), "--item", "bad"])
    assert r.exit_code == 3
    r = runner.invoke(cli.app, ["batch", "rerun", str(cli_env.dir), "i1", "--brief", "改", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["stage"] == "awaiting_selection"
    assert cli_env.client.workflows[-1]["expand"]["inputs"]["brief"] == "改"


def test_comfy_unreachable_aborts_run_resumable(fake_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from game_art.errors import UpstreamError
    orig, state = fake_env.client.submit, {"down": True}

    def down(wf: JSON) -> str:
        if len(fake_env.client.submissions) == 1 and state.pop("down", False):
            raise UpstreamError("comfy_unreachable", "ComfyUI 未启动或地址错误")
        return orig(wf)

    monkeypatch.setattr(fake_env.client, "submit", down)
    with pytest.raises(UpstreamError):
        fake_env.start()
    st = G.get_status(fake_env.dir, deps=fake_env.deps)
    assert st.stage == "interrupted" and _items(st)["i2"].status == "pending"
    st = fake_env.resume()
    assert st.stage == "awaiting_selection" and all(i.status == "generated" for i in st.items)
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i2/1", "b1/i3/1"]


def test_unreadable_candidate_goes_to_needs_review(fake_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    orig = fake_env.client.download_outputs

    def broken(entry: JSON, dest: Path) -> list[Path]:
        out = orig(entry, dest)
        out[0].write_bytes(b"not a png")
        return out

    monkeypatch.setattr(fake_env.client, "download_outputs", broken)
    st = fake_env.start(n_items=1)
    it = _items(st)["i1"]
    assert it.status == "needs_review" and "质检失败" in (it.error or "")


def test_crash_during_wait_does_not_resubmit(fake_env: Env) -> None:
    fake_env.client.crash_on_wait = 2  # i2 已提交、正在等待时进程被杀
    with pytest.raises(KeyboardInterrupt):
        fake_env.start()
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i2/1"]
    fresh = Env(fake_env.tmp, server=fake_env.client.server)  # 新进程，ComfyUI 仍记得该任务
    st = G.resume_batch(fresh.dir, None, deps=fresh.deps)
    assert fresh.client.submissions == ["b1/i3/1"]  # i2 不再提交，只等待原任务
    assert fresh.client.waits[0] == "pid2"
    assert st.stage == "awaiting_selection" and all(i.status == "generated" for i in st.items)
    assert fake_env.ledger.spent("b1/i2/") == pytest.approx(0.04)


def test_crash_during_wait_resubmits_if_comfy_forgot(fake_env: Env) -> None:
    fake_env.client.crash_on_wait = 2
    with pytest.raises(KeyboardInterrupt):
        fake_env.start()
    fresh = Env(fake_env.tmp)  # ComfyUI 重启过，不认识旧 prompt_id
    st = G.resume_batch(fresh.dir, None, deps=fresh.deps)
    assert fresh.client.submissions == ["b1/i2/1", "b1/i3/1"]
    assert st.stage == "awaiting_selection"


def test_batch_lock_rejects_concurrent_run(fake_env: Env) -> None:
    fake_env.start(n_items=1)
    with BatchStore(fake_env.dir).lock():
        with pytest.raises(InputError) as ei:
            fake_env.resume({"pick_numbers": [1]})
        assert ei.value.code == "batch_locked"
        with pytest.raises(InputError):
            G.rerun_batch(fake_env.dir, ["i1"], deps=fake_env.deps)
        assert G.get_status(fake_env.dir, deps=fake_env.deps).stage == "awaiting_selection"  # 只读不加锁
    assert fake_env.resume({"pick_numbers": [1]}).stage == "finished"


@pytest.mark.parametrize("value", [
    {"pick_numbers": "1"},
    {"pick_numbers": [1, "2"]},
    {"pick": ["i1"]},
    {"pick": {"i1": 1}},
    {"pick": {"i1": [1.5]}},
    {"pick": {"i1": [1]}, "pick_numbers": [3, 5], "rerun": ["i1"]},
    {"rerun": ["i1"], "pick_numbers": [1, 3, 5]},
])
def test_select_bad_shapes_rejected(fake_env: Env, value: JSON) -> None:
    fake_env.start()
    with pytest.raises(InputError):
        fake_env.resume(value)
    assert G.get_status(fake_env.dir, deps=fake_env.deps).stage == "awaiting_selection"


def test_rerun_attempt_numbers_unique_across_reruns(fake_env: Env) -> None:
    fake_env.qc.score = lambda tag: 5.0 if tag.endswith("/1") else 8.0
    st = fake_env.start(n_items=1, max_retries=1)  # 第 1 次未过、第 2 次通过
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i1/2"] and _items(st)["i1"].attempts == 2
    st = G.rerun_batch(fake_env.dir, ["i1"], deps=fake_env.deps)
    st = G.rerun_batch(fake_env.dir, ["i1"], deps=fake_env.deps)
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i1/2", "b1/i1/3", "b1/i1/4"]
    assert len(set(fake_env.client.submissions)) == 4
    it = _items(st)["i1"]
    assert it.attempts == 1 and it.status == "generated"  # 状态里的次数仍从 0 计（重试上限按本轮算）
    assert fake_env.qc.calls[-2:] == ["b1/i1/4", "b1/i1/4"]  # 质检记账用同一个全局编号
    # 磁盘：当前轮在 items/i1/4，旧的都已归档
    assert BatchStore(fake_env.dir).attempt_pngs("i1", 4)
    assert not (fake_env.dir / "items" / "i1" / "1").exists()
    assert sorted(p.name for p in (fake_env.dir / "archive" / "i1").iterdir()) == ["1", "2"]


def test_rerun_crash_during_wait_reuses_unique_attempt(fake_env: Env) -> None:
    fake_env.start(n_items=1)
    fake_env.client.crash_on_wait = 2
    with pytest.raises(KeyboardInterrupt):
        G.rerun_batch(fake_env.dir, ["i1"], deps=fake_env.deps)
    fresh = Env(fake_env.tmp, server=fake_env.client.server)
    st = G.resume_batch(fresh.dir, None, deps=fresh.deps)
    assert fresh.client.submissions == [] and fresh.client.waits == ["pid2"]
    assert st.stage == "awaiting_selection" and _items(st)["i1"].status == "generated"


@pytest.mark.parametrize("err", [
    lambda: TimeoutError_("comfy_wait_timeout", "等待超时，任务仍在 ComfyUI 中运行（未取消）：pid2"),
    lambda: UpstreamError("comfy_http_error", "ComfyUI 读取历史 失败：HTTP 502", {"status": 502}),
], ids=["timeout", "http_502"])
def test_wait_timeout_or_comfy_5xx_halts_run_resumable(
        fake_env: Env, err: Callable[[], GAError], monkeypatch: pytest.MonkeyPatch) -> None:
    orig, state = fake_env.client.wait, {"armed": True}

    def flaky(pid: str, timeout: float) -> JSON:
        if pid == "pid2" and state.pop("armed", False):
            raise err()
        return orig(pid, timeout)

    monkeypatch.setattr(fake_env.client, "wait", flaky)
    with pytest.raises(GAError) as ei:
        fake_env.start()
    assert ei.value.details["resume"] == f"ga batch resume {fake_env.dir}"
    st = G.get_status(fake_env.dir, deps=fake_env.deps)
    assert st.stage == "interrupted" and _items(st)["i2"].status == "pending" and _items(st)["i2"].attempts == 0
    st = fake_env.resume()
    assert fake_env.client.submissions == ["b1/i1/1", "b1/i2/1", "b1/i3/1"]  # i2 不重复提交
    assert fake_env.client.waits.count("pid2") == 1  # 续跑时等的是原任务
    assert st.stage == "awaiting_selection" and all(i.status == "generated" for i in st.items)


def test_comfy_4xx_still_fails_only_that_item(fake_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from game_art.errors import UpstreamError
    orig = fake_env.client.wait

    def bad(pid: str, timeout: float) -> JSON:
        if pid == "pid2":
            raise UpstreamError("comfy_http_error", "HTTP 404", {"status": 404})
        return orig(pid, timeout)

    monkeypatch.setattr(fake_env.client, "wait", bad)
    st = fake_env.start()
    assert _items(st)["i2"].status == "failed" and _items(st)["i1"].status == "generated"


def _wf_without(tmp_path: Path, *drop: str, rename: tuple[str, str] | None = None) -> Path:
    wf = json.loads(WF.read_text(encoding="utf-8"))
    for nid in drop:
        wf.pop(nid)
    for node in wf.values():  # 指向被删节点的连线改成空字符串
        node["inputs"] = {k: ("" if isinstance(v, list) and len(v) == 2 and v[0] in drop else v)
                          for k, v in node["inputs"].items()}
    if rename:
        old, new = rename
        wf[new] = wf.pop(old)
        for node in wf.values():
            for k, v in node["inputs"].items():
                if isinstance(v, list) and len(v) == 2 and v[0] == old:
                    node["inputs"][k] = [new, v[1]]
    p = tmp_path / "wf.json"
    p.write_text(json.dumps(wf, ensure_ascii=False), encoding="utf-8")
    return p


def test_start_rejects_workflow_without_brief_target(fake_env: Env) -> None:
    wf = _wf_without(fake_env.tmp, "expand")
    with pytest.raises(InputError) as ei:
        fake_env.start(workflow=str(wf))
    assert ei.value.code == "workflow_missing_node" and "expand" in ei.value.message
    assert not fake_env.dir.exists() or not (fake_env.dir / "state.sqlite").exists()
    assert fake_env.client.submissions == []


def test_start_rejects_workflow_without_n_target(fake_env: Env) -> None:
    wf = _wf_without(fake_env.tmp, rename=("gen", "g2"))
    with pytest.raises(InputError) as ei:
        fake_env.start(workflow=str(wf))
    assert ei.value.code == "workflow_missing_node" and "gen" in ei.value.message


def test_start_without_expand_ok_when_items_set_prompt(fake_env: Env) -> None:
    wf = _wf_without(fake_env.tmp, "expand")
    st = fake_env.start(workflow=str(wf), n_items=1,
                        items=[{"id": "i1", "brief": "龙", "sets": {"gen.prompt": "一条龙"}}])
    assert st.stage == "awaiting_selection"
    assert fake_env.client.workflows[0]["gen"]["inputs"]["prompt"] == "一条龙"


def test_candidates_applied_to_edit_node(tmp_path: Path) -> None:
    from game_art.batch.manifest import Item, Manifest
    m = Manifest(batch_id="b", workflow=REPO_ROOT / "workflows" / "ref_to_asset.json", candidates=3,
                 items=[Item(id="x", brief="灯笼")], max_retries=None, budget=None)
    wf = G.build_workflow(m, "x", "灯笼", 1)
    assert wf["edit"]["inputs"]["n"] == 3 and wf["expand"]["inputs"]["brief"] == "灯笼"
