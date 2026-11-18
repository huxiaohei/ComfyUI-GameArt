"""按配置实例化 provider。"""
from __future__ import annotations

from ..config import Settings
from ..errors import InputError
from .base import Provider
from .openai_compat import OpenAICompatProvider


def get_provider(settings: Settings, name: str) -> Provider:
    cfg = settings.providers.get(name)
    if cfg is None:
        raise InputError("unknown_provider", f"未配置的 provider: {name}")
    if cfg.type == "openai_compatible":
        resolved = cfg.model_copy(update={"base_url": settings.base_url(name)})
        return OpenAICompatProvider(name, resolved, settings.api_key(name))
    raise InputError("unknown_provider_type",
                     f"provider {name} 的 type 不支持: {cfg.type}（可用: openai_compatible）")
