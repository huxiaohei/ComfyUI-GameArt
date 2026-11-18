"""配置模型、加载与 .env 解析。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .errors import InputError
from .paths import REPO_ROOT

EXAMPLE_CONFIG = REPO_ROOT / "config" / "config.example.yaml"


class ProviderConfig(BaseModel):
    type: str
    # 接口地址：写死在 base_url，或用 base_url_env 指向环境变量 / .env 里的键（优先，缺失时回退 base_url）
    base_url: str = ""
    base_url_env: str | None = None
    # 密钥所在的环境变量名；本机模型服务（如 Ollama）不需要密钥时可以不写
    api_key_env: str | None = None
    # 额外出现在节点"模型"下拉里的模型名（profiles 里用到的模型会自动出现，不必重复写）
    models: list[str] = Field(default_factory=list)
    # /models 不列出、但接口实际可用的模型（如部分厂商的出图模型）；ga config check 对它们放行
    unlisted_models: list[str] = Field(default_factory=list)
    # 走 Gemini 原生出图接口的模型名（支持通配符，如 "gemini-*"）。这些模型不认 OpenAI 的图片接口
    gemini_image_models: list[str] = Field(default_factory=list)
    # Gemini 原生接口的地址；不写时由 base_url 推出（把结尾的 /v1 换成 /gemini）
    gemini_base_url: str = ""
    timeout: float = 120
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _need_base_url(self) -> Self:
        if not self.base_url and not self.base_url_env:
            raise ValueError("provider 需要 base_url 或 base_url_env 之一")
        return self


class Target(BaseModel):
    provider: str
    model: str
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, v: Any) -> Any:
        if isinstance(v, str):
            provider, sep, model = v.partition(":")
            if not sep or not provider or not model:
                raise ValueError(f"目标应为 'provider:model' 格式: {v!r}")
            return {"provider": provider, "model": model}
        return v


class Pricing(BaseModel):
    per_image: float = 0
    per_1k_input_tokens: float = 0
    per_1k_output_tokens: float = 0


class Settings(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    profiles: dict[str, list[Target]] = Field(default_factory=dict)
    pricing: dict[str, Pricing] = Field(default_factory=dict)
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_root: Path
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".game_art")
    max_retries: int = 2
    # 节点"尺寸"下拉里的选项（宽x高）。能用哪些尺寸由出图模型决定，换模型时改这里
    image_sizes: list[str] = Field(default_factory=lambda: ["1024x1024", "1536x1024", "1024x1536"])
    config_dir: Path

    @field_validator("comfy_root", "data_dir", "config_dir", mode="before")
    @classmethod
    def _expand(cls, v: Any) -> Any:
        return Path(os.path.expanduser(str(v))) if v is not None else v

    def _provider(self, provider: str) -> ProviderConfig:
        cfg = self.providers.get(provider)
        if cfg is None:
            raise InputError("unknown_provider", f"未配置的 provider: {provider}")
        return cfg

    def base_url(self, provider: str) -> str:
        cfg = self._provider(provider)
        url = os.environ.get(cfg.base_url_env) if cfg.base_url_env else None
        url = url or cfg.base_url
        if not url:
            raise InputError(
                "missing_base_url",
                f"缺少环境变量 {cfg.base_url_env}（接口地址），可写入 {self.config_dir / '.env'}",
                {"env": cfg.base_url_env, "dotenv": str(self.config_dir / ".env")},
            )
        return url

    def api_key(self, provider: str) -> str:
        cfg = self._provider(provider)
        if cfg.api_key_env is None:
            return "not-needed"
        key = os.environ.get(cfg.api_key_env)
        if not key:
            raise InputError(
                "missing_api_key",
                f"缺少环境变量 {cfg.api_key_env}，可写入 {self.config_dir / '.env'}",
                {"env": cfg.api_key_env, "dotenv": str(self.config_dir / ".env")},
            )
        return key


def model_choices(settings: Settings) -> list[str]:
    """节点"模型"下拉的选项：先是各 profile 名，再是配置里出现过的 provider:model。"""
    out = list(settings.profiles)
    for targets in settings.profiles.values():
        out += [f"{t.provider}:{t.model}" for t in targets]
    for name, cfg in settings.providers.items():
        out += [f"{name}:{m}" for m in cfg.models]
    return list(dict.fromkeys(out))


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def find_config_path() -> Path:
    env = os.environ.get("GAME_ART_CONFIG")
    if env:
        return Path(os.path.expanduser(env))
    user = Path.home() / ".game_art" / "config.yaml"
    if user.is_file():
        return user
    return EXAMPLE_CONFIG


def load_settings(path: Path | None = None) -> Settings:
    path = Path(os.path.expanduser(str(path))) if path else find_config_path()
    if not path.is_file():
        raise InputError("config_not_found", f"找不到配置文件: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise InputError("config_invalid", f"配置文件不是合法 YAML: {path}: {e}") from e
    if not isinstance(raw, dict):
        raise InputError("config_invalid", f"配置文件顶层必须是映射: {path}")
    config_dir = path.resolve().parent
    raw["config_dir"] = config_dir
    raw.setdefault("comfy_root", "~/Documents/ComfyUI")
    try:
        settings = Settings(**raw)
    except Exception as e:
        raise InputError("config_invalid", f"配置不合法 ({path}): {e}") from e
    _apply_dotenv(config_dir / ".env")
    return settings


# 本进程从 .env 写入 os.environ 的键 → 写入的值。ComfyUI 常驻进程重载配置时，
# 这些键按新的 .env 覆盖（或在 .env 删掉后撤销）；真实 shell 环境变量（未由我们写入，
# 或已被改成别的值）始终优先。
_FROM_DOTENV: dict[str, str] = {}


def _apply_dotenv(dotenv: Path) -> None:
    values = _parse_dotenv(dotenv) if dotenv.is_file() else {}
    ours = {k for k, v in _FROM_DOTENV.items() if os.environ.get(k) == v}
    for k in list(_FROM_DOTENV):
        if k not in ours:  # 已被别处改掉/删掉：不再归 .env 管
            del _FROM_DOTENV[k]
        elif k not in values:  # .env 里删掉了
            del os.environ[k]
            del _FROM_DOTENV[k]
    for k, v in values.items():
        if k not in os.environ or k in ours:
            os.environ[k] = v
            _FROM_DOTENV[k] = v
