"""ComfyUI HTTP 客户端。"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from game_art.errors import ExecutionError, InputError, TimeoutError_, UpstreamError


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 60, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self.client_id = str(uuid.uuid4())

    # ---- 底层 ----
    def _request(self, method: str, path: str, **kw: Any) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        try:
            return requests.request(method, f"{self.base_url}{path}", **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise UpstreamError(
                "comfy_unreachable", f"ComfyUI 未启动或地址错误：{self.base_url}", {"cause": str(e)[:200]}
            ) from e

    @staticmethod
    def _check(r: requests.Response, what: str) -> None:
        if r.status_code >= 400:
            raise UpstreamError(
                "comfy_http_error", f"ComfyUI {what} 失败：HTTP {r.status_code} {r.text[:300]}",
                {"status": r.status_code},
            )

    @staticmethod
    def _json(r: requests.Response, what: str) -> Any:
        try:
            return r.json()
        except ValueError as e:
            raise UpstreamError(
                "comfy_bad_response", f"ComfyUI {what} 返回的不是合法 JSON：{r.text[:200]}",
                {"raw": r.text[:500]},
            ) from e

    # ---- API ----
    def ping(self) -> bool:
        try:
            return self._request("GET", "/api/system_stats").status_code == 200
        except UpstreamError:
            return False

    def object_info(self) -> dict[str, Any]:
        r = self._request("GET", "/api/object_info")
        self._check(r, "object_info")
        info: dict[str, Any] = self._json(r, "object_info")
        return info

    def upload(self, path: Path) -> str:
        path = Path(path).expanduser()
        if not path.is_file():
            raise InputError("upload_not_found", f"找不到要上传的文件：{path}")
        with path.open("rb") as f:
            r = self._request(
                "POST", "/api/upload/image", files={"image": (path.name, f)}, data={"type": "input"}
            )
        self._check(r, "上传图片")
        body = self._json(r, "上传图片")
        if not isinstance(body, dict) or not body.get("name"):
            raise UpstreamError("comfy_bad_response", f"ComfyUI 上传图片的响应缺少 name 字段：{str(body)[:200]}",
                                {"raw": str(body)[:500]})
        name, sub = body["name"], body.get("subfolder") or ""
        return f"{sub}/{name}" if sub else name

    def submit(self, wf: dict[str, Any]) -> str:
        prompt_id = str(uuid.uuid4())
        r = self._request(
            "POST", "/api/prompt",
            json={"prompt": wf, "prompt_id": prompt_id, "client_id": self.client_id},
        )
        if r.status_code == 400:
            try:
                body = r.json()
            except ValueError:
                body = {"raw": r.text[:500]}
            raise InputError("workflow_invalid", "ComfyUI 拒绝了该工作流，请检查节点/输入", details=body)
        self._check(r, "提交工作流")
        self._json(r, "提交工作流")  # 只校验响应可解码；prompt_id 由我们自己指定
        return prompt_id

    def queue_contains(self, prompt_id: str) -> bool:
        """任务是否仍在 ComfyUI 队列中（运行中或排队中）。"""
        r = self._request("GET", "/api/queue")
        self._check(r, "读取队列")
        body = self._json(r, "读取队列")
        if not isinstance(body, dict):
            raise UpstreamError("comfy_bad_response", f"ComfyUI 读取队列的响应不是对象：{str(body)[:200]}")
        for key in ("queue_running", "queue_pending"):
            for entry in body.get(key) or []:
                if isinstance(entry, (list, tuple)) and len(entry) > 1 and entry[1] == prompt_id:
                    return True
        return False

    def history(self, prompt_id: str) -> dict[str, Any]:
        r = self._request("GET", f"/api/history/{prompt_id}")
        self._check(r, "读取历史")
        body = self._json(r, "读取历史")
        if not isinstance(body, dict):
            raise UpstreamError("comfy_bad_response", f"ComfyUI 读取历史的响应不是对象：{str(body)[:200]}")
        return body.get(prompt_id) or {}

    def wait(self, prompt_id: str, timeout: float, poll: float = 1.0) -> dict[str, Any]:
        deadline = self._clock() + timeout
        while True:
            entry = self.history(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    node, msg = _execution_error(status)
                    raise ExecutionError(
                        "comfy_execution_failed",
                        f"节点 {node} 执行失败：{msg}",
                        {"prompt_id": prompt_id, "node_id": node, "exception_message": msg},
                    )
                if status.get("completed") or (entry.get("outputs") and not status):
                    return entry
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError_(
                    "comfy_wait_timeout",
                    f"等待超时，任务仍在 ComfyUI 中运行（未取消）：{prompt_id}",
                    {"prompt_id": prompt_id, "resume": f"ga comfy wait {prompt_id}"},
                )
            self._sleep(min(poll, remaining))

    def _view(self, filename: str, subfolder: str) -> requests.Response:
        return self._request(
            "GET", "/api/view", params={"filename": filename, "subfolder": subfolder, "type": "output"}
        )

    def download_outputs(self, history: dict[str, Any], dest: Path) -> list[Path]:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def target(name: str, sub: str) -> Path:
            p = dest / name
            if p.exists() or p in written:
                p = dest / f"{sub.replace('/', '_')}_{name}" if sub else p
            return p

        for node_out in (history.get("outputs") or {}).values():
            for img in node_out.get("images", []) or []:
                if img.get("type") != "output":
                    continue
                name, sub = img["filename"], img.get("subfolder") or ""
                r = self._view(name, sub)
                self._check(r, f"下载 {name}")
                png = target(name, sub)
                png.write_bytes(r.content)
                written.append(png)
                if name.lower().endswith(".png"):
                    side = self._view(name[:-4] + ".json", sub)
                    if side.status_code == 404:
                        continue
                    self._check(side, f"下载 {name[:-4]}.json")
                    sp = png.with_suffix(".json")
                    sp.write_bytes(side.content)
                    written.append(sp)
        return written


def _execution_error(status: dict[str, Any]) -> tuple[str, str]:
    for msg in status.get("messages") or []:
        if isinstance(msg, (list, tuple)) and len(msg) == 2 and msg[0] == "execution_error":
            d = msg[1] or {}
            return str(d.get("node_id", "?")), str(d.get("exception_message", "")).strip()
    return "?", "未知错误"
