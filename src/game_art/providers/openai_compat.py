"""OpenAI 兼容适配器：chat / vision / generate / edit。"""
from __future__ import annotations

import base64
import binascii
import json
from fnmatch import fnmatchcase
from typing import Any

import requests
from PIL import Image, UnidentifiedImageError

from ..config import ProviderConfig
from ..errors import InputError, UpstreamError
from ..imaging import from_bytes, to_data_url, to_png_bytes
from . import gemini_image
from .base import ChatResult, ImageRequest, ImageResult, Message

_BODY_LIMIT = 2000
_REASON_LIMIT = 300


def _strip_b64(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("<b64 omitted>" if k == "b64_json" else _strip_b64(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_b64(v) for v in obj]
    return obj


def _error_reason(resp: requests.Response) -> str:
    """从上游的报错响应里取出给人看的原因（OpenAI 风格的 error.message），取不到返回空串。"""
    try:
        body = resp.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message") if isinstance(err, dict) else err
    return " ".join(msg.split())[:_REASON_LIMIT] if isinstance(msg, str) else ""


class OpenAICompatProvider:
    def __init__(self, name: str, config: ProviderConfig, api_key: str) -> None:
        self.name = name
        self.config = config
        self._api_key = api_key
        self._base = config.base_url.rstrip("/")

    # ---- HTTP ----
    def _request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                 **kw: Any) -> requests.Response:
        headers = {"Authorization": f"Bearer {self._api_key}", **(headers or {})}
        try:
            resp = requests.request(method, url, headers=headers,
                                    timeout=self.config.timeout, **kw)
        except requests.RequestException as e:
            raise UpstreamError("network_error", f"网络错误 ({self.name}): {e}",
                                {"retryable": True}) from e
        if not resp.ok:
            retryable = resp.status_code == 429 or resp.status_code >= 500
            details: dict[str, Any] = {"status": resp.status_code, "body": resp.text[:_BODY_LIMIT]}
            if retryable:
                details["retryable"] = True
            reason = _error_reason(resp)
            raise UpstreamError(f"http_{resp.status_code}",
                                f"上游返回 HTTP {resp.status_code} ({self.name})"
                                + (f"，原因：{reason}" if reason else ""), details)
        return resp

    def _json(self, resp: requests.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as e:
            raise UpstreamError("bad_response", "上游返回的不是合法 JSON",
                                {"body": resp.text[:_BODY_LIMIT]}) from e
        if not isinstance(data, dict):
            raise UpstreamError("bad_response", "上游返回的 JSON 不是对象",
                                {"body": resp.text[:_BODY_LIMIT]})
        return data

    # ---- chat ----
    def chat(self, messages: list[Message], *, model: str, json_mode: bool = False,
             params: dict[str, Any] | None = None) -> ChatResult:
        msgs = []
        for m in messages:
            if m.images:
                content: Any = [{"type": "text", "text": m.text}] + [
                    {"type": "image_url", "image_url": {"url": to_data_url(i)}} for i in m.images
                ]
            else:
                content = m.text
            msgs.append({"role": m.role, "content": content})
        body: dict[str, Any] = {"model": model, "messages": msgs}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(params or {})
        data = self._json(self._request("POST", f"{self._base}/chat/completions", json=body))
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise UpstreamError("bad_response", "chat 响应缺少 choices",
                                {"body": json.dumps(data, ensure_ascii=False)[:_BODY_LIMIT]}) from e
        usage = data.get("usage") or {}
        return ChatResult(text=text,
                          input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                          output_tokens=int(usage.get("completion_tokens", 0) or 0),
                          raw=data)

    # ---- images ----
    @staticmethod
    def _prompt(req: ImageRequest) -> str:
        return req.prompt + (f"\n避免：{req.negative}" if req.negative else "")

    def _base_fields(self, req: ImageRequest, model: str) -> dict[str, Any]:
        f: dict[str, Any] = {"model": model, "prompt": self._prompt(req), "n": req.n, "size": req.size}
        if req.transparent:
            f["background"] = "transparent"
            f["output_format"] = "png"
        f.update(req.params)
        return f

    def generate(self, req: ImageRequest, *, model: str) -> ImageResult:
        if self._is_gemini_image(model):
            return self._gemini_images(req, model)
        resp = self._request("POST", f"{self._base}/images/generations",
                             json=self._base_fields(req, model))
        return self._parse_images(self._json(resp), req)

    def edit(self, req: ImageRequest, *, model: str) -> ImageResult:
        if not req.refs:
            raise InputError("no_refs", "edit 至少需要 1 张参考图")
        if self._is_gemini_image(model):
            return self._gemini_images(req, model)
        fields = {k: (v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list))
                      else str(v).lower() if isinstance(v, bool) else str(v))
                  for k, v in self._base_fields(req, model).items()}
        files = [("image[]", (f"ref{i}.png", to_png_bytes(img), "image/png"))
                 for i, img in enumerate(req.refs)]
        if req.mask is not None:
            files.append(("mask", ("mask.png", to_png_bytes(req.mask), "image/png")))
        resp = self._request("POST", f"{self._base}/images/edits", data=fields, files=files)
        return self._parse_images(self._json(resp), req)

    # ---- Gemini 原生出图 ----
    def _is_gemini_image(self, model: str) -> bool:
        return any(fnmatchcase(model, pat) for pat in self.config.gemini_image_models)

    def _gemini_base(self) -> str:
        if self.config.gemini_base_url:
            return self.config.gemini_base_url.rstrip("/")
        return self._base.removesuffix("/v1") + "/gemini"

    def _gemini_images(self, req: ImageRequest, model: str) -> ImageResult:
        """Gemini 一次只出一张：要 n 张就请求 n 次；后面的失败时保留已出的图并标记 partial。"""
        url = f"{self._gemini_base()}/v1beta/models/{model}:generateContent"
        body = gemini_image.build_body(req, self._prompt(req))
        images: list[Image.Image] = []
        raws: list[dict[str, Any]] = []
        for _ in range(req.n):
            try:
                resp = self._request("POST", url, headers={"x-goog-api-key": self._api_key}, json=body)
                got, raw = gemini_image.parse_images(self._json(resp))
            except UpstreamError:
                if not images:
                    raise
                break
            images += got[:1]
            raws.append(raw)
        return ImageResult(images=images, partial=len(images) < req.n, seed=None, raw={"responses": raws})

    def _parse_images(self, data: dict[str, Any], req: ImageRequest) -> ImageResult:
        items = data.get("data")
        raw = _strip_b64(data)
        snippet = json.dumps(raw, ensure_ascii=False)[:_BODY_LIMIT]
        if not isinstance(items, list) or not items:
            raise UpstreamError("no_images", "上游未返回图片", {"body": snippet})
        images: list[Image.Image] = []
        for item in items:
            if not isinstance(item, dict):
                raise UpstreamError("bad_response", "data 项格式不对", {"body": snippet})
            if item.get("b64_json"):
                try:
                    blob = base64.b64decode(item["b64_json"], validate=False)
                except (binascii.Error, ValueError) as e:
                    raise UpstreamError("not_an_image", "b64_json 无法解码", {"body": snippet}) from e
            elif item.get("url"):
                blob = self._download(item["url"])
            else:
                raise UpstreamError("bad_response", "data 项既无 b64_json 也无 url",
                                    {"body": snippet})
            try:
                images.append(from_bytes(blob).convert("RGBA"))
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
                raise UpstreamError("not_an_image", "上游返回的内容不是合法图片",
                                    {"body": snippet}) from e
        return ImageResult(images=images, partial=len(images) < req.n, seed=self._sent_seed(req), raw=raw)

    @staticmethod
    def _sent_seed(req: ImageRequest) -> int | None:
        # 仅当 seed 确实随请求发给上游（经 params）才回报，否则为 None
        seed = req.params.get("seed")
        return seed if isinstance(seed, int) and not isinstance(seed, bool) else None

    def _download(self, url: str) -> bytes:
        try:
            r = requests.get(url, timeout=self.config.timeout)
        except requests.RequestException as e:
            raise UpstreamError("download_failed", f"下载结果图失败: {e}", {"retryable": True}) from e
        if not r.ok:
            raise UpstreamError("download_failed", f"下载结果图失败: HTTP {r.status_code}",
                                {"status": r.status_code, "body": r.text[:_BODY_LIMIT],
                                 "url": url})
        return r.content
