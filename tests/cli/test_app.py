import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
import requests
import responses
from typer.testing import CliRunner

from game_art.cli import app as cli
from game_art.errors import ExecutionError, TimeoutError_
from game_art.paths import REPO_ROOT
from tests.conftest import write_cfg

runner = CliRunner()


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = write_cfg(tmp_path, comfy_root=str(tmp_path / "comfy"), comfy_url="http://comfy.test:8188")
    monkeypatch.setenv("GAME_ART_CONFIG", str(p))
    return p


@pytest.fixture
def tmp_wf(tmp_path: Path) -> Path:
    wf = {
        "gen": {"class_type": "GACloudGenerate", "inputs": {"n": 1, "prompt": "x", "tag": ""}},
        "ref": {"class_type": "LoadImage", "inputs": {"image": f"@upload{{{tmp_path / 'r.png'}}}"}},
        "save": {"class_type": "GASaveAsset", "inputs": {"tag": "keep"}},
    }
    (tmp_path / "r.png").write_bytes(b"png")
    p = tmp_path / "wf.json"
    p.write_text(json.dumps(wf))
    return p


class StubComfy:
    instances: ClassVar[list["StubComfy"]] = []
    wait_exc: ClassVar[BaseException | None] = None

    def __init__(self, base_url: str, *a: Any, **k: Any) -> None:
        self.calls: list[str] = []
        self.submitted: dict[str, Any] = {}
        StubComfy.instances.append(self)

    def upload(self, path: Path) -> str:
        self.calls.append("upload")
        return "server_r.png"

    def submit(self, wf: dict[str, Any]) -> str:
        self.calls.append("submit")
        self.submitted = wf
        return "pid123"

    def object_info(self) -> dict[str, Any]:
        self.calls.append("object_info")
        return {"X": {"input": {"required": {"v": ["STRING", {"default": ""}]}}, "output": ["STRING"],
                      "output_name": ["out"]}}

    def wait(self, pid: str, timeout: float) -> dict[str, Any]:
        self.calls.append("wait")
        if StubComfy.wait_exc:
            raise StubComfy.wait_exc
        return {"outputs": {}}

    def download_outputs(self, entry: dict[str, Any], dest: Path) -> list[Path]:
        self.calls.append("download")
        dest.mkdir(parents=True, exist_ok=True)
        png, js = dest / "a_00001.png", dest / "a_00001.json"
        png.write_bytes(b"x")
        js.write_text(json.dumps({"tag": "t", "provider": "p"}))
        self.dest = dest
        return [png, js]


@pytest.fixture
def stub_comfy(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> type[StubComfy]:
    StubComfy.instances, StubComfy.wait_exc = [], None
    monkeypatch.setattr(cli, "ComfyClient", StubComfy)
    return StubComfy


# ---- run
def test_run_dry_run_prints_patched(tmp_wf: Path, tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["run", str(tmp_wf), "--set", "gen.n=4", "--tag", "T1", "--dry-run"])
    assert r.exit_code == 0, r.output
    wf = json.loads(r.stdout)
    assert wf["gen"]["inputs"]["n"] == 4
    assert wf["gen"]["inputs"]["tag"] == "T1" and wf["save"]["inputs"]["tag"] == "keep"


def test_run_dry_run_auto_tag(tmp_wf: Path) -> None:
    r = runner.invoke(cli.app, ["run", str(tmp_wf), "--dry-run"])
    tag = json.loads(r.stdout)["gen"]["inputs"]["tag"]
    assert tag.startswith("run/") and len(tag) == len("run/") + 8


def test_run_bad_set_exit_3_lists_keys(tmp_wf: Path) -> None:
    r = runner.invoke(cli.app, ["run", str(tmp_wf), "--set", "gen.zzz=1", "--dry-run"])
    assert r.exit_code == 3
    assert "n" in r.stderr and "prompt" in r.stderr


def test_run_uploads_then_submits(stub_comfy: type[StubComfy], tmp_wf: Path, tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["run", str(tmp_wf), "--out", str(tmp_path / "o"), "--tag", "T"])
    assert r.exit_code == 0, r.output
    c = stub_comfy.instances[-1]
    assert c.calls == ["upload", "submit", "wait", "download"]
    assert c.submitted["ref"]["inputs"]["image"] == "server_r.png"
    assert c.submitted["gen"]["inputs"]["tag"] == "T"
    assert r.stdout.strip().splitlines() == [str(tmp_path / "o" / "a_00001.png")]
    assert "pid123" in r.stderr


def test_run_json_success_shape(stub_comfy: type[StubComfy], tmp_wf: Path, tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["run", str(tmp_wf), "--out", str(tmp_path / "o"), "--json"])
    assert r.exit_code == 0
    d = json.loads(r.stdout)
    assert d == {"ok": True, "prompt_id": "pid123", "files": [str(tmp_path / "o" / "a_00001.png")],
                 "meta": [{"tag": "t", "provider": "p"}]}


def test_run_default_out_uses_date_and_tag(
        stub_comfy: type[StubComfy], tmp_wf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(cli.app, ["run", str(tmp_wf), "--tag", "mytag"])
    assert r.exit_code == 0
    parts = stub_comfy.instances[-1].dest.parts
    assert parts[0] == "outputs" and parts[-1] == "mytag" and len(parts[1]) == 10


def test_run_timeout_exit_5_mentions_prompt_id(stub_comfy: type[StubComfy], tmp_wf: Path) -> None:
    stub_comfy.wait_exc = TimeoutError_("comfy_wait_timeout", "等待超时：pid123",
                                        {"prompt_id": "pid123", "resume": "ga comfy wait pid123"})
    r = runner.invoke(cli.app, ["run", str(tmp_wf), "--json"])
    assert r.exit_code == 5
    assert "pid123" in r.stderr and "ga comfy wait pid123" in r.stderr
    d = json.loads(r.stdout)
    assert d["ok"] is False and d["error"]["details"]["prompt_id"] == "pid123"


def test_run_execution_error_exit_6(stub_comfy: type[StubComfy], tmp_wf: Path) -> None:
    stub_comfy.wait_exc = ExecutionError("comfy_execution_failed", "节点 gen 执行失败")
    assert runner.invoke(cli.app, ["run", str(tmp_wf)]).exit_code == 6


def test_run_interrupt_exit_130(stub_comfy: type[StubComfy], tmp_wf: Path) -> None:
    stub_comfy.wait_exc = KeyboardInterrupt()
    r = runner.invoke(cli.app, ["run", str(tmp_wf)])
    assert r.exit_code == 130 and "ga comfy wait pid123" in r.stderr


def test_json_error_shape(tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["run", str(tmp_path / "nope.json"), "--json"])
    assert r.exit_code == 3
    d = json.loads(r.stdout)
    assert d["ok"] is False and set(d["error"]) == {"code", "message", "details"}
    assert d["error"]["code"] == "workflow_not_found"


def test_usage_error_exit_2() -> None:
    assert runner.invoke(cli.app, ["run"]).exit_code == 2


# ---- comfy wait
def test_comfy_wait_downloads(stub_comfy: type[StubComfy], tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["comfy", "wait", "pidX", "--out", str(tmp_path / "w"), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["prompt_id"] == "pidX"
    assert stub_comfy.instances[-1].calls == ["wait", "download"]


# ---- comfy install
def test_comfy_install_creates_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    r = runner.invoke(cli.app, ["comfy", "install", "--comfy-root", str(root)])
    assert r.exit_code == 0, r.output
    nodes = root / "custom_nodes" / "game_art_nodes"
    wfs = root / "user" / "default" / "workflows" / "game_art"
    assert nodes.is_symlink() and nodes.resolve() == (REPO_ROOT / "comfy_nodes").resolve()
    assert wfs.is_symlink() and wfs.resolve() == (REPO_ROOT / "workflows" / "editor").resolve()
    assert not (root / "custom_nodes" / "game_art").exists()
    assert "重启" in r.stderr
    # 幂等
    r2 = runner.invoke(cli.app, ["comfy", "install", "--comfy-root", str(root), "--json"])
    assert r2.exit_code == 0
    assert [x["status"] for x in json.loads(r2.stdout)["links"]] == ["exists", "exists"]


def test_comfy_install_repoints_legacy_workflow_link(tmp_path: Path) -> None:
    # 早期版本把编辑器目录链到 workflows/（API 格式，编辑器打不开）；重装时自动改指 workflows/editor/
    root = tmp_path / "ComfyUI"
    wfs = root / "user" / "default" / "workflows" / "game_art"
    wfs.parent.mkdir(parents=True)
    wfs.symlink_to(REPO_ROOT / "workflows", target_is_directory=True)
    r = runner.invoke(cli.app, ["comfy", "install", "--comfy-root", str(root), "--json"])
    assert r.exit_code == 0, r.output
    assert [x["status"] for x in json.loads(r.stdout)["links"]] == ["created", "updated"]
    assert wfs.resolve() == (REPO_ROOT / "workflows" / "editor").resolve()


def test_comfy_install_refuses_foreign_symlink(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    (root / "custom_nodes").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    (root / "custom_nodes" / "game_art_nodes").symlink_to(other)
    r = runner.invoke(cli.app, ["comfy", "install", "--comfy-root", str(root)])
    assert r.exit_code == 3 and "不会覆盖" in r.stderr
    assert (root / "custom_nodes" / "game_art_nodes").resolve() == other.resolve()
    assert not (root / "user").exists()  # 先检查后动手：另一条链接也没建


def test_comfy_install_refuses_real_dir_and_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    (root / "custom_nodes" / "game_art_nodes").mkdir(parents=True)
    assert runner.invoke(cli.app, ["comfy", "install", "--comfy-root", str(root)]).exit_code == 3
    assert runner.invoke(cli.app, ["comfy", "install", "--comfy-root", str(tmp_path / "no")]).exit_code == 3


# ---- comfy export-editor
def test_comfy_export_editor_writes_graph_files(stub_comfy: type[StubComfy], tmp_path: Path) -> None:
    src = tmp_path / "wfs"
    src.mkdir()
    (src / "a.json").write_text(json.dumps({"n1": {"class_type": "X", "inputs": {"v": "@repo/q"}}}))
    r = runner.invoke(cli.app, ["comfy", "export-editor", "--src", str(src), "--json"])
    assert r.exit_code == 0, r.output
    dest = src / "editor" / "a.json"
    assert json.loads(r.stdout)["written"] == [str(dest)]
    graph = json.loads(dest.read_text(encoding="utf-8"))
    assert graph["nodes"][0]["type"] == "X" and graph["nodes"][0]["widgets_values"] == ["@repo/q"]
    assert graph["links"] == []
    # 重复导出不会把 editor/ 里的产物当成输入
    assert runner.invoke(cli.app, ["comfy", "export-editor", "--src", str(src)]).exit_code == 0
    assert sorted(p.name for p in (src / "editor").iterdir()) == ["a.json"]


def test_comfy_export_editor_empty_dir(stub_comfy: type[StubComfy], tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["comfy", "export-editor", "--src", str(tmp_path)])
    assert r.exit_code == 3 and "没有" in r.stderr


# ---- config check
CB = "http://comfy.test:8188"


@responses.activate
def test_config_check_all_ok(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "sk-secret")
    responses.get("https://example.test/v1/models", json={"data": [{"id": "img-model-a"}, {"id": "txt-model"}]})
    responses.get(f"{CB}/api/system_stats", json={})
    responses.get(f"{CB}/api/object_info", json={"GACloudGenerate": {}})
    r = runner.invoke(cli.app, ["config", "check"])
    assert r.exit_code == 0, r.output
    assert "✗" not in r.stdout and r.stdout.count("✓") == 4
    assert responses.calls[0].request.headers["Authorization"] == "Bearer sk-secret"
    assert "sk-secret" not in r.stdout + r.stderr


@responses.activate
def test_config_check_reports_missing_key(cfg: Path) -> None:
    responses.get(f"{CB}/api/system_stats", json={})
    responses.get(f"{CB}/api/object_info", json={"GACloudGenerate": {}})
    r = runner.invoke(cli.app, ["config", "check"])
    assert r.exit_code == 3
    line = next(x for x in r.stdout.splitlines() if x.startswith("✗"))
    assert "COMFYUI_LLM_BASE_KEY" in line
    assert ".env" in r.stdout
    assert not any("example.test" in (c.request.url or "") for c in responses.calls)  # 无密钥不探活


@responses.activate
def test_config_check_base_url_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = write_cfg(tmp_path, comfy_url="http://comfy.test:8188",
                  providers={"aihubmix": {"type": "openai_compatible", "base_url_env": "COMFYUI_LLM_BASE_URL",
                                          "api_key_env": "COMFYUI_LLM_BASE_KEY"}})
    monkeypatch.setenv("GAME_ART_CONFIG", str(p))
    (tmp_path / ".env").write_text("COMFYUI_LLM_BASE_KEY=k\nCOMFYUI_LLM_BASE_URL=https://proxy.test/v1\n")
    responses.get("https://proxy.test/v1/models", json={"data": [{"id": "img-model-a"}, {"id": "txt-model"}]})
    responses.get(f"{CB}/api/system_stats", json={})
    responses.get(f"{CB}/api/object_info", json={"GACloudGenerate": {}})
    r = runner.invoke(cli.app, ["config", "check"])
    assert r.exit_code == 0, r.output
    assert "proxy.test" in r.stdout


@responses.activate
def test_config_check_reports_missing_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = write_cfg(tmp_path, comfy_url="http://comfy.test:8188",
                  providers={"aihubmix": {"type": "openai_compatible", "base_url_env": "COMFYUI_LLM_BASE_URL",
                                          "api_key_env": "COMFYUI_LLM_BASE_KEY"}})
    monkeypatch.setenv("GAME_ART_CONFIG", str(p))
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "k")
    responses.get(f"{CB}/api/system_stats", json={})
    responses.get(f"{CB}/api/object_info", json={"GACloudGenerate": {}})
    r = runner.invoke(cli.app, ["config", "check"])
    assert r.exit_code == 3
    line = next(x for x in r.stdout.splitlines() if x.startswith("✗"))
    assert "COMFYUI_LLM_BASE_URL" in line


def _ok_comfy() -> None:
    responses.get(f"{CB}/api/system_stats", json={})
    responses.get(f"{CB}/api/object_info", json={"GACloudGenerate": {}})


@responses.activate
def test_config_check_rejects_non_model_list_response(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # base_url 少了 /v1 时，官网 /models 页面也会返回 200（HTML），不能算通过
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "k")
    responses.get("https://example.test/v1/models", body="<html>models page</html>",
                  content_type="text/html")
    _ok_comfy()
    r = runner.invoke(cli.app, ["config", "check"])
    assert r.exit_code == 3
    line = next(x for x in r.stdout.splitlines() if x.startswith("✗"))
    assert "模型列表" in line
    assert "/v1" in r.stdout  # 修复建议提示检查 /v1


@responses.activate
def test_config_check_rejects_json_without_data_list(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "k")
    responses.get("https://example.test/v1/models", json={"message": "hello"})
    _ok_comfy()
    r = runner.invoke(cli.app, ["config", "check"])
    assert r.exit_code == 3


@responses.activate
def test_config_check_reports_profile_models_missing_upstream(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "k")
    responses.get("https://example.test/v1/models", json={"data": [{"id": "txt-model"}]})
    _ok_comfy()
    r = runner.invoke(cli.app, ["config", "check", "--json"])
    assert r.exit_code == 3
    d = json.loads(r.stdout)
    bad = [c for c in d["checks"] if not c["ok"]]
    assert len(bad) == 1 and "img-model-a" in bad[0]["message"] and "txt-model" not in bad[0]["message"]


@responses.activate
def test_config_check_accepts_declared_unlisted_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 有的厂商 /models 不列出出图模型，但接口实际可用：在 provider 里声明 unlisted_models 即视为可用
    p = write_cfg(tmp_path, comfy_url="http://comfy.test:8188",
                  providers={"aihubmix": {"type": "openai_compatible", "base_url": "https://example.test/v1",
                                          "api_key_env": "COMFYUI_LLM_BASE_KEY", "unlisted_models": ["img-model-a"]}})
    monkeypatch.setenv("GAME_ART_CONFIG", str(p))
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "k")
    responses.get("https://example.test/v1/models", json={"data": [{"id": "txt-model"}]})
    _ok_comfy()
    r = runner.invoke(cli.app, ["config", "check"])
    assert r.exit_code == 0, r.output
    assert "img-model-a" in r.stdout  # 输出里说明哪些模型是按声明放行的


@responses.activate
def test_config_check_upstream_unreachable_exit_4(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "k")
    responses.get("https://example.test/v1/models", body=requests.ConnectionError("down"))
    responses.get(f"{CB}/api/system_stats", body=requests.ConnectionError("down"))
    r = runner.invoke(cli.app, ["config", "check", "--json"])
    assert r.exit_code == 4
    d = json.loads(r.stdout)
    assert d["ok"] is False and [c["ok"] for c in d["checks"]] == [True, False, False]


@responses.activate
def test_config_check_nodes_missing_and_auth_rejected(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "k")
    responses.get("https://example.test/v1/models", status=401)
    responses.get(f"{CB}/api/system_stats", json={})
    responses.get(f"{CB}/api/object_info", json={"Other": {}})
    r = runner.invoke(cli.app, ["config", "check"])
    assert r.exit_code == 3
    assert "ga comfy install" in r.stdout and "密钥被拒绝" in r.stdout


# ---- 折入修复
def test_entry_point_help() -> None:
    r = runner.invoke(cli.app, ["--help"])
    assert r.exit_code == 0 and "config" in r.stdout and "comfy" in r.stdout


def test_install_oserror_maps_to_io_error_exit_3(tmp_path: Path) -> None:
    root = tmp_path / "ComfyUI"
    (root / "user" / "default").mkdir(parents=True)
    (root / "user" / "default" / "workflows").write_text("i am a file")
    r = runner.invoke(cli.app, ["comfy", "install", "--comfy-root", str(root), "--json"])
    assert r.exit_code == 3
    assert json.loads(r.stdout)["error"]["code"] == "io_error"
    assert "Traceback" not in r.stderr


def test_run_out_unwritable_exit_3(
        stub_comfy: type[StubComfy], tmp_wf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: StubComfy, entry: dict[str, Any], dest: Path) -> list[Path]:
        raise PermissionError(13, "Permission denied", str(dest))
    monkeypatch.setattr(StubComfy, "download_outputs", boom)
    r = runner.invoke(cli.app, ["run", str(tmp_wf), "--out", str(tmp_path / "o"), "--json"])
    assert r.exit_code == 3 and json.loads(r.stdout)["error"]["code"] == "io_error"


def test_interrupt_json_emits_error_object(stub_comfy: type[StubComfy], tmp_wf: Path) -> None:
    stub_comfy.wait_exc = KeyboardInterrupt()
    r = runner.invoke(cli.app, ["run", str(tmp_wf), "--json"])
    assert r.exit_code == 130 and json.loads(r.stdout)["error"]["code"] == "interrupted"
