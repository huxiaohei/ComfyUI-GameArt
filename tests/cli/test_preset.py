"""ga run --preset / fix / regen / qc / final：命令行逐张出图和修图。ComfyUI 用替身，不联网、不花钱。"""
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from game_art.cli import app as cli
from game_art.paths import REPO_ROOT
from tests.conftest import write_cfg

runner = CliRunner()
QC_DETAILS = [
    {"text": "支脚外撇，底架上窄下宽", "level": "must", "kind": "layout"},
    {"text": "斜撑线太粗", "level": "must", "kind": "detail"},
    {"text": "旗子偏橙", "level": "suggest", "kind": "detail"},
]


class FakeComfy:
    """按提交的工作流模拟 ComfyUI：正常/修图/重出时在 save.subfolder 里存下一个编号的图；只质检时只回质检结果。"""
    instances: ClassVar[list["FakeComfy"]] = []
    qc_issues: ClassVar[list[dict[str, Any]]] = QC_DETAILS

    def __init__(self, base_url: str, *a: Any, **k: Any) -> None:
        self.submitted: dict[str, Any] = {}
        FakeComfy.instances.append(self)

    def object_info(self) -> dict[str, Any]:
        return {}

    def upload(self, path: Path) -> str:
        return path.name

    def submit(self, wf: dict[str, Any]) -> str:
        self.submitted = wf
        return "pid"

    def wait(self, pid: str, timeout: float) -> dict[str, Any]:
        wf = self.submitted
        qc = wf["qc"]["inputs"]
        job = json.loads(qc["fix"]) if qc["fix"] else None
        tag = qc["tag"]
        ledger = Path.home() / ".game_art" / "ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"tag": tag, "cost": 0.01}) + "\n")
        result = {"score": 6.0, "passed": False, "issues": FakeComfy.qc_issues}
        if job and job.get("qc"):
            return {"outputs": {"qc": {"ga_qc": [{"mode": "qc", "results": [result]}]}}}
        save = wf["save"]["inputs"]
        folder, name = Path(save["subfolder"]), save["name"]
        folder.mkdir(parents=True, exist_ok=True)
        n = 1
        while (folder / f"{name}_{n:05d}.png").exists():
            n += 1
        png = folder / f"{name}_{n:05d}.png"
        png.write_bytes(b"png")
        record: dict[str, Any] = {"tag": tag, "cost": 0.05, "file": png.name,
                                  "qc": {"score": 6.0, "passed": False, "issues": [], "details": FakeComfy.qc_issues}}
        if job:  # 修图：记录沿用原图的，加上修了什么（上游的扩写不在提交的工作流里）
            source = json.loads(Path(job["file"]).with_suffix(".json").read_text(encoding="utf-8"))
            record = {**source, **record, "fix": {"notes": job["notes"], "from": Path(job["file"]).name,
                                                  "issues": job["issues"]}}
        else:
            expand = wf["expand"]["inputs"]
            record.update(brief=expand["brief"], item=expand["item"])
            if expand["feedback"]:
                record["feedback"] = expand["feedback"].split("\n")
        png.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return {"outputs": {"save": {"images": [], "ga_files": [str(png)]}}}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个小项目：描述清单 + 预设（API 格式也能用作预设）+ 保存位置。"""
    cfg = write_cfg(tmp_path, comfy_root=str(tmp_path / "comfy"),
                    profiles={"image": ["p:img"], "image_edit": ["p:edit"], "text": ["p:txt"], "vision": ["p:vis"]},
                    pricing={"p:img": {"per_image": 0.04}, "p:edit": {"per_image": 0.05}})
    monkeypatch.setenv("GAME_ART_CONFIG", str(cfg))
    (tmp_path / "briefs.yaml").write_text(
        "asset_type: prop\nprefix: '俯视。'\nitems:\n  - id: archer_lv1\n    brief: 木箭楼\n  - id: sword\n"
        "    brief: 一把剑\n    asset_type: icon\n", encoding="utf-8")
    wf = json.loads((REPO_ROOT / "workflows" / "text_to_asset_qc.json").read_text(encoding="utf-8"))
    wf["expand"]["inputs"]["briefs_path"] = str(tmp_path / "briefs.yaml")
    wf["expand"]["inputs"]["feedback"] = "上一项留下的意见"
    wf["save"]["inputs"].update(subfolder=str(tmp_path / "imgs"), name="tower")
    (tmp_path / "tower.json").write_text(json.dumps(wf, ensure_ascii=False), encoding="utf-8")
    FakeComfy.instances = []
    monkeypatch.setattr(cli, "ComfyClient", FakeComfy)
    return tmp_path


def _run(*args: str) -> dict[str, Any]:
    r = runner.invoke(cli.app, [*args, "--json"])
    assert r.exit_code == 0, r.output
    out: dict[str, Any] = json.loads(r.stdout)
    return out


def _record(p: Path) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
    return out


def test_run_preset_item(project: Path) -> None:
    d = _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1")
    wf = FakeComfy.instances[-1].submitted
    # 和编辑器里选一项一样：描述（含公共文字）、种类、清空旧意见、保存的文件名
    assert wf["expand"]["inputs"]["brief"] == "俯视。木箭楼" and wf["expand"]["inputs"]["asset_type"] == "prop"
    assert wf["expand"]["inputs"]["feedback"] == "" and wf["save"]["inputs"]["name"] == "archer_lv1"
    assert wf["gen"]["inputs"]["tag"].startswith("run/") and "show_prompt" in wf  # 整条流程照原样
    png = project / "imgs" / "archer_lv1_00001.png"
    (img,) = d["images"]
    assert img["path"] == str(png) and img["score"] == 6.0 and img["passed"] is False and img["threshold"] == 7.0
    assert img["issues"][0] == {"n": 1, "text": "支脚外撇，底架上窄下宽", "level": "must", "kind": "layout"}
    assert d["cost"] == 0.01 and img["image_cost"] == 0.05
    assert _record(png)["preset"] == str((project / "tower.json").resolve())  # 之后 fix / regen 不用再写 --preset
    # 第二张接着编号
    _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1")
    assert (project / "imgs" / "archer_lv1_00002.png").is_file()


def test_run_item_with_sets(project: Path) -> None:
    """--set 先生效（可以指定清单），--item 再选项，手写的 --set 最后覆盖 --item 填的值。"""
    wf = json.loads((project / "tower.json").read_text(encoding="utf-8"))
    wf["expand"]["inputs"]["briefs_path"] = ""
    (project / "bare.json").write_text(json.dumps(wf), encoding="utf-8")
    _run("run", "--preset", str(project / "bare.json"), "--item", "sword",
         "--set", f"expand.briefs_path={project / 'briefs.yaml'}", "--set", "save.name=sword_try")
    expand = FakeComfy.instances[-1].submitted["expand"]["inputs"]
    assert expand["brief"] == "俯视。一把剑" and expand["asset_type"] == "icon"
    assert FakeComfy.instances[-1].submitted["save"]["inputs"]["name"] == "sword_try"
    r = runner.invoke(cli.app, ["run", "--preset", str(project / "bare.json"), "--item", "sword", "--json"])
    assert r.exit_code == 3 and json.loads(r.stdout)["error"]["code"] == "no_briefs"


def test_run_needs_exactly_one_source(project: Path) -> None:
    assert runner.invoke(cli.app, ["run"]).exit_code == 2
    r = runner.invoke(cli.app, ["run", "--preset", str(project / "tower.json"), "--item", "nope", "--json"])
    assert r.exit_code == 3 and json.loads(r.stdout)["error"]["code"] == "unknown_brief_item"


def test_fix(project: Path) -> None:
    _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1")
    cand = project / "imgs" / "archer_lv1_00001.png"
    d = _run("fix", str(cand), "--issue", "3", "--issue", "2", "--note", "旗子改成草黄色", "--quality", "medium")
    wf = FakeComfy.instances[-1].submitted
    job = json.loads(wf["qc"]["inputs"]["fix"])
    assert job == {"file": str(cand), "notes": ["旗子偏橙", "斜撑线太粗", "旗子改成草黄色"], "issues": [3, 2]}
    assert wf["qc"]["inputs"]["auto_qc"] is True and wf["qc"]["inputs"]["fix_quality"] == "medium"
    assert wf["qc"]["inputs"]["fix_with_refs"] is True  # 默认带风格参考图
    assert "show_prompt" not in wf and wf["qc"]["inputs"]["tag"].startswith("fix/")
    new = project / "imgs" / "archer_lv1_00002.png"
    assert d["images"][0]["path"] == str(new) and d["from"] == str(cand)
    assert _record(new)["fix"]["from"] == cand.name and _record(new)["preset"]
    assert cand.read_bytes() == b"png"  # 原图不动
    # --no-refs、指定模型
    _run("fix", str(new), "--issue", "2", "--no-refs", "--edit-model", "p:other")
    qc = FakeComfy.instances[-1].submitted["qc"]["inputs"]
    assert qc["fix_with_refs"] is False and qc["edit_model"] == "p:other"


@pytest.mark.parametrize(("args", "code"), [
    (["--issue", "1"], "layout_issue"),  # 构图类要用 regen
    ([], "no_fix_notes"),
    (["--issue", "7"], "bad_issue"),
    (["--note", "x", "--quality", "ultra"], "bad_option"),
])
def test_fix_refuses(project: Path, args: list[str], code: str) -> None:
    _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1")
    r = runner.invoke(cli.app, ["fix", str(project / "imgs" / "archer_lv1_00001.png"), *args, "--json"])
    assert r.exit_code == 3 and json.loads(r.stdout)["error"]["code"] == code
    if code == "layout_issue":
        assert "ga regen" in r.stderr
    assert all(c.submitted == {} for c in FakeComfy.instances[1:])  # 没有提交


def test_fix_needs_preset_for_editor_made_images(project: Path) -> None:
    cand = project / "imgs" / "archer_lv1_00001.png"
    cand.parent.mkdir()
    cand.write_bytes(b"png")
    cand.with_suffix(".json").write_text(json.dumps({"brief": "木箭楼", "qc": {"details": QC_DETAILS}}))
    r = runner.invoke(cli.app, ["fix", str(cand), "--issue", "2", "--json"])
    assert r.exit_code == 3 and "--preset" in r.stderr
    _run("fix", str(cand), "--issue", "2", "--preset", str(project / "tower.json"))
    assert (project / "imgs" / "archer_lv1_00002.png").is_file()


def test_regen_accumulates_feedback(project: Path) -> None:
    _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1")
    first = project / "imgs" / "archer_lv1_00001.png"
    d = _run("regen", str(first), "--issue", "1", "--note", "支脚垂直")
    wf = FakeComfy.instances[-1].submitted
    assert wf["expand"]["inputs"]["brief"] == "俯视。木箭楼" and wf["expand"]["inputs"]["item"] == "archer_lv1"
    assert wf["expand"]["inputs"]["feedback"] == "支脚外撇，底架上窄下宽\n支脚垂直" and wf["qc"]["inputs"]["fix"] == ""
    assert d["feedback"] == ["支脚外撇，底架上窄下宽", "支脚垂直"]
    second = project / "imgs" / "archer_lv1_00002.png"
    assert _record(second)["feedback"] == ["支脚外撇，底架上窄下宽", "支脚垂直"]
    # 下一轮在上一轮的基础上累积（重复的不加）
    _run("regen", str(second), "--issue", "2", "--issue", "1")
    feedback = FakeComfy.instances[-1].submitted["expand"]["inputs"]["feedback"]
    assert feedback == "支脚外撇，底架上窄下宽\n支脚垂直\n斜撑线太粗"
    _run("regen", str(second), "--issue", "3", "--reset")
    assert FakeComfy.instances[-1].submitted["expand"]["inputs"]["feedback"] == "旗子偏橙"


def test_qc_writes_back(project: Path) -> None:
    _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1")
    png = project / "imgs" / "archer_lv1_00001.png"
    record = _record(png)
    record.pop("qc")
    png.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")  # 比如手动修过、还没质检
    FakeComfy.qc_issues = [{"text": "线稍粗", "level": "suggest", "kind": "detail"}]
    try:
        d = _run("qc", str(png))
    finally:
        FakeComfy.qc_issues = QC_DETAILS
    wf = FakeComfy.instances[-1].submitted
    assert json.loads(wf["qc"]["inputs"]["fix"]) == {"file": str(png), "qc": True} and "show_prompt" not in wf
    assert _record(png)["qc"] == {"score": 6.0, "issues": ["线稍粗"], "passed": False,
                                  "details": [{"text": "线稍粗", "level": "suggest", "kind": "detail"}]}
    assert d["images"][0]["issues"] == [{"n": 1, "text": "线稍粗", "level": "suggest", "kind": "detail"}]
    assert d["cost"] == 0.01


def test_max_cost_blocks_submit(project: Path) -> None:
    r = runner.invoke(cli.app, ["run", "--preset", str(project / "tower.json"), "--item", "archer_lv1",
                                "--max-cost", "0.01", "--json"])
    assert r.exit_code == 9
    err = json.loads(r.stdout)["error"]
    assert err["code"] == "over_max_cost" and err["details"]["estimate"] == 0.05
    assert FakeComfy.instances[-1].submitted == {}  # 没有提交
    _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1", "--max-cost", "0.1")


def test_dry_run_shows_patched_workflow(project: Path) -> None:
    _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1")
    r = runner.invoke(cli.app, ["fix", str(project / "imgs" / "archer_lv1_00001.png"), "--note", "x", "--dry-run"])
    assert r.exit_code == 0 and json.loads(r.stdout)["qc"]["inputs"]["fix"]
    assert len(FakeComfy.instances) == 2 and FakeComfy.instances[-1].submitted == {}


def test_final(project: Path) -> None:
    _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1")
    _run("run", "--preset", str(project / "tower.json"), "--item", "archer_lv1")
    imgs = project / "imgs"
    d = _run("final", str(imgs / "archer_lv1_00002.png"))  # 名字默认去掉编号
    assert d["path"] == str((imgs / "archer_lv1.png").resolve()) and d["backup"] is None
    assert _record(imgs / "archer_lv1.png")["file"] == "archer_lv1.png"
    r = runner.invoke(cli.app, ["final", str(imgs / "archer_lv1_00001.png"), "--name", "archer_lv1", "--json"])
    assert r.exit_code == 3 and "--force" in r.stderr
    d = _run("final", str(imgs / "archer_lv1_00001.png"), "--name", "archer_lv1", "--force")
    assert Path(d["backup"]).is_file() and (imgs / "archer_lv1_00001.png").is_file()
