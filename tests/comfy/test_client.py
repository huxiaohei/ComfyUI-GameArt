import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import requests
import responses

from game_art.comfy.client import ComfyClient
from game_art.errors import ExecutionError, InputError, TimeoutError_, UpstreamError

BASE = "http://comfy.test:8188"


@pytest.fixture
def client() -> ComfyClient:
    return ComfyClient(BASE, sleep=lambda s: None)


@responses.activate
def test_upload_and_submit(client: ComfyClient, tmp_path: Path) -> None:
    f = tmp_path / "r.png"
    f.write_bytes(b"png")
    responses.post(f"{BASE}/api/upload/image", json={"name": "r.png", "subfolder": "", "type": "input"})
    assert client.upload(f) == "r.png"
    responses.post(f"{BASE}/api/prompt", json={"prompt_id": "x"})
    pid = client.submit({"a": {"class_type": "X", "inputs": {}}})
    body = json.loads(cast(str, responses.calls[-1].request.body))
    assert body["prompt_id"] == pid and "client_id" in body
    # ComfyUI 0.44+ 只接受标准小写带连字符的 UUID（uuid4().hex 会被 400 invalid_prompt_id 拒绝）
    assert str(uuid.UUID(pid)) == pid


@responses.activate
def test_submit_400_is_workflow_invalid(client: ComfyClient) -> None:
    responses.post(f"{BASE}/api/prompt", status=400, json={"node_errors": {"gen": {"errors": ["bad"]}}})
    with pytest.raises(InputError) as e:
        client.submit({})
    assert e.value.code == "workflow_invalid" and "node_errors" in e.value.details


@responses.activate
def test_wait_success_returns_history(client: ComfyClient) -> None:
    entry = {"outputs": {"s": {"images": []}}, "status": {"status_str": "success", "completed": True}}
    responses.get(f"{BASE}/api/history/p1", json={})
    responses.get(f"{BASE}/api/history/p1", json={"p1": entry})
    assert client.wait("p1", timeout=10) == entry


@responses.activate
def test_wait_execution_error(client: ComfyClient) -> None:
    entry = {"outputs": {}, "status": {"status_str": "error", "completed": False, "messages": [
        ["execution_start", {}],
        ["execution_error", {"node_id": "gen", "exception_message": "boom"}]]}}
    responses.get(f"{BASE}/api/history/p1", json={"p1": entry})
    with pytest.raises(ExecutionError) as e:
        client.wait("p1", timeout=10)
    assert "gen" in e.value.message and "boom" in e.value.message


@responses.activate
def test_wait_timeout_keeps_job(client: ComfyClient) -> None:
    responses.get(f"{BASE}/api/history/p1", json={})
    t = [0.0]
    c = ComfyClient(BASE, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    with pytest.raises(TimeoutError_) as e:
        c.wait("p1", timeout=3, poll=1)
    assert e.value.details["prompt_id"] == "p1" and "p1" in e.value.details["resume"]
    assert not any("interrupt" in (call.request.url or "") for call in responses.calls)


@responses.activate
def test_unreachable(client: ComfyClient) -> None:
    responses.get(f"{BASE}/api/object_info", body=requests.ConnectionError("no"))
    with pytest.raises(UpstreamError) as e:
        client.object_info()
    assert e.value.code == "comfy_unreachable" and BASE in e.value.message
    responses.get(f"{BASE}/api/system_stats", body=requests.ConnectionError("no"))
    assert client.ping() is False


@responses.activate
def test_download_outputs_fetches_png_and_sidecar(client: ComfyClient, tmp_path: Path) -> None:
    hist = {"outputs": {"s": {"images": [
        {"filename": "a.png", "subfolder": "ga", "type": "output"},
        {"filename": "b.png", "subfolder": "ga", "type": "output"},
        {"filename": "t.png", "subfolder": "", "type": "temp"}]}}}
    responses.get(f"{BASE}/api/view", body=b"PNG-A", match=[responses.matchers.query_param_matcher(
        {"filename": "a.png", "subfolder": "ga", "type": "output"})])
    responses.get(f"{BASE}/api/view", body=b'{"k":1}', match=[responses.matchers.query_param_matcher(
        {"filename": "a.json", "subfolder": "ga", "type": "output"})])
    responses.get(f"{BASE}/api/view", body=b"PNG-B", match=[responses.matchers.query_param_matcher(
        {"filename": "b.png", "subfolder": "ga", "type": "output"})])
    responses.get(f"{BASE}/api/view", status=404, match=[responses.matchers.query_param_matcher(
        {"filename": "b.json", "subfolder": "ga", "type": "output"})])
    paths = client.download_outputs(hist, tmp_path / "out")
    assert sorted(p.name for p in paths) == ["a.json", "a.png", "b.png"]
    assert (tmp_path / "out" / "a.png").read_bytes() == b"PNG-A"


@responses.activate
def test_download_name_clash_prefixes_subfolder(client: ComfyClient, tmp_path: Path) -> None:
    hist = {"outputs": {"s": {"images": [
        {"filename": "a.png", "subfolder": "", "type": "output"},
        {"filename": "a.png", "subfolder": "x/y", "type": "output"}]}}}
    def cb(req: Any) -> tuple[int, dict[str, str], str | bytes]:
        return (404, {}, "") if req.params["filename"].endswith(".json") else (200, {}, b"P")
    responses.add_callback(responses.GET, f"{BASE}/api/view", callback=cb)
    paths = client.download_outputs(hist, tmp_path)
    assert {p.name for p in paths} >= {"a.png", "x_y_a.png"}


@responses.activate
@pytest.mark.parametrize("call", ["object_info", "history", "upload", "submit"])
def test_non_json_response_is_comfy_bad_response(client: ComfyClient, tmp_path: Path, call: str) -> None:
    f = tmp_path / "r.png"
    f.write_bytes(b"png")
    for m, path in (("GET", "/api/object_info"), ("GET", "/api/history/p"),
                    ("POST", "/api/upload/image"), ("POST", "/api/prompt")):
        responses.add(m, f"{BASE}{path}", body="<html>oops</html>", status=200)
    fn: Callable[[], Any] = {"object_info": client.object_info, "history": lambda: client.history("p"),
          "upload": lambda: client.upload(f), "submit": lambda: client.submit({})}[call]
    with pytest.raises(UpstreamError) as e:
        fn()
    assert e.value.code == "comfy_bad_response" and "oops" in e.value.message


@responses.activate
def test_upload_body_missing_name(client: ComfyClient, tmp_path: Path) -> None:
    f = tmp_path / "r.png"
    f.write_bytes(b"png")
    responses.post(f"{BASE}/api/upload/image", json={"subfolder": ""})
    with pytest.raises(UpstreamError) as e:
        client.upload(f)
    assert e.value.code == "comfy_bad_response" and "name" in e.value.message


@responses.activate
def test_queue_contains(client: ComfyClient) -> None:
    responses.get(f"{BASE}/api/queue", json={"queue_running": [[0, "run1", {}, {}, []]],
                                             "queue_pending": [[1, "pend1", {}, {}, []]]})
    assert client.queue_contains("run1") and client.queue_contains("pend1")
    assert not client.queue_contains("other")
