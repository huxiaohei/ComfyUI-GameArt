"""上游模型列表：请求 {base_url}/models，带内存 + 磁盘缓存，供节点的"模型"下拉使用。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from .config import Settings, model_choices
from .errors import GAError

CACHE_FILE = "models_cache.json"
TTL = 3600.0          # 列表多久重新请求一次（秒）
RETRY_AFTER = 60.0    # 请求失败后多久再试（秒），期间用旧列表
NODE_TIMEOUT = 5.0    # 节点下拉用的请求超时：宁可少列，也不能卡住界面

# provider 名 -> (有效期截止时间戳, 模型 id 列表)
_MEM: dict[str, tuple[float, list[str]]] = {}


def probe_models(base_url: str, key: str, timeout: float = 10) -> tuple[str, str, set[str]]:
    """请求 {base_url}/models，返回 (kind, 说明, 上游模型 id 集合)。

    kind: ok | auth | upstream | bad_response。只有拿到 OpenAI 兼容的模型列表
    （JSON 且含 data 数组）才算 ok：base_url 写错（如少了 /v1）时官网页面也可能返回 200。
    """
    try:
        r = requests.get(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"},
                         timeout=timeout)
    except requests.RequestException as e:
        return "upstream", f"无法连接：{type(e).__name__}", set()
    if r.status_code in (401, 403):
        return "auth", f"密钥被拒绝（HTTP {r.status_code}）", set()
    if r.status_code >= 400:
        return "upstream", f"HTTP {r.status_code}", set()
    try:
        body = r.json()
    except ValueError:
        body = None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        ctype = r.headers.get("Content-Type", "?").split(";")[0]
        return "bad_response", f"返回的不是模型列表（HTTP {r.status_code}，{ctype}）", set()
    ids = {str(m["id"]) for m in data if isinstance(m, dict) and "id" in m}
    return "ok", f"HTTP {r.status_code}，{len(ids)} 个模型", ids


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cached_entry(cache: dict[str, Any], provider: str) -> tuple[float, list[str]]:
    entry = cache.get(provider)
    if not isinstance(entry, dict):
        return 0.0, []
    at, models = entry.get("fetched_at"), entry.get("models")
    if not isinstance(at, (int, float)) or not isinstance(models, list):
        return 0.0, []
    return float(at), [str(m) for m in models]


def provider_models(settings: Settings, provider: str, *, refresh: bool = False) -> list[str]:
    """某个 provider 当前提供的全部模型 id（已排序）。

    一小时内复用缓存（内存 + data_dir/models_cache.json）；请求失败时退回上次成功的列表，
    从没成功过就返回空列表。不抛异常：缺密钥、断网都只是"列不出来"。
    """
    now = time.time()
    hit = _MEM.get(provider)
    if hit and hit[0] > now and not refresh:
        return hit[1]
    path = settings.data_dir / CACHE_FILE
    cache = _read_cache(path)
    at, models = _cached_entry(cache, provider)
    if models and now - at < TTL and not refresh:
        _MEM[provider] = (at + TTL, models)
        return models
    try:
        base_url, key = settings.base_url(provider), settings.api_key(provider)
    except GAError:
        return models
    kind, _, ids = probe_models(base_url, key, timeout=NODE_TIMEOUT)
    if kind != "ok":
        _MEM[provider] = (now + RETRY_AFTER, models)
        return models
    models = sorted(ids)
    cache[provider] = {"fetched_at": now, "models": models}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 缓存写不进去不影响使用，只是下次还要再请求
    _MEM[provider] = (now + TTL, models)
    return models


def all_model_choices(settings: Settings) -> list[str]:
    """"模型"下拉的全部选项：配置里的（模型组 + 用到的模型）在前，各服务商提供的全部模型在后。"""
    out = model_choices(settings)
    for name, cfg in settings.providers.items():
        listed = provider_models(settings, name)
        out += [f"{name}:{m}" for m in sorted({*listed, *cfg.unlisted_models})]
    return list(dict.fromkeys(out))
