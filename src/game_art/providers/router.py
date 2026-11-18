"""Router：按 profile 顺序尝试目标，失败切换，每次尝试写账本。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from ..config import Settings, Target
from ..errors import GAError, InputError, UpstreamError
from ..ledger import Ledger
from .base import ChatResult, ImageRequest, ImageResult, Message, Provider
from .registry import get_provider


class Router:
    def __init__(self, settings: Settings, ledger: Ledger,
                 provider_factory: Callable[[Settings, str], Provider] = get_provider) -> None:
        self.settings = settings
        self.ledger = ledger
        self._factory = provider_factory

    @classmethod
    def from_settings(cls, s: Settings) -> Router:
        return cls(s, Ledger.from_settings(s))

    # ---- 解析 ----
    def resolve(self, profile: str) -> list[Target]:
        targets = self.settings.profiles.get(profile)
        if targets is None:
            if ":" in profile:
                try:
                    return [Target.model_validate(profile)]
                except ValueError as e:
                    raise InputError("bad_target", f"目标格式不对: {profile!r}（应为 provider:model）") from e
            avail = ", ".join(sorted(self.settings.profiles)) or "（无）"
            raise InputError("unknown_profile", f"未知 profile: {profile}，可用: {avail}",
                             {"available": sorted(self.settings.profiles)})
        if not targets:
            raise InputError("empty_profile", f"profile {profile} 没有配置任何目标")
        return targets

    # ---- 通用执行 ----
    def _run(self, profile: str, kind: Literal["chat", "generate", "edit"], tag: str,
             call: Callable[[Provider, Target], Any],
             tally: Callable[[Any], dict[str, Any]]) -> tuple[Any, Target]:
        attempts: list[dict[str, Any]] = []
        local: list[bool] = []  # 每次尝试是否为本地输入错（缺密钥等）
        for target in self.resolve(profile):
            label = f"{target.provider}:{target.model}"
            try:
                result = call(self._factory(self.settings, target.provider), target)
            except GAError as e:
                self.ledger.record(tag=tag, provider=target.provider, model=target.model,
                                   kind=kind, ok=False, error=f"{e.code}: {e.message}")
                attempts.append({"target": label, "code": e.code, "message": e.message})
                local.append(isinstance(e, InputError))
                continue
            self.ledger.record(tag=tag, provider=target.provider, model=target.model,
                               kind=kind, ok=True, **tally(result))
            return result, target
        summary = "; ".join(f"{a['target']} → {a['code']}: {a['message']}" for a in attempts)
        if local and all(local):
            # 全是本地问题（如缺密钥）：按输入错退出（3），第一条提示放最前
            first = attempts[0]
            raise InputError(first["code"],
                             f"{first['message']}（profile {profile} 的所有目标都失败了: {summary}）",
                             {"attempts": attempts})
        raise UpstreamError("all_targets_failed",
                            f"profile {profile} 的所有目标都失败了: {summary}",
                            {"attempts": attempts})

    @staticmethod
    def _merged(req: ImageRequest, target: Target) -> ImageRequest:
        # 请求级参数优先于目标级
        return req.model_copy(update={"params": {**target.params, **req.params}})

    def chat(self, profile: str, messages: list[Message], *, tag: str,
             json_mode: bool = False) -> tuple[ChatResult, Target]:
        return self._run(
            profile, "chat", tag,
            lambda p, t: p.chat(messages, model=t.model, json_mode=json_mode,
                                params=dict(t.params)),
            lambda r: {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens})

    @staticmethod
    def _image_tally(r: ImageResult) -> dict[str, Any]:
        """张数 + 上游 usage 里的 token 数（有则记，便于和账单核对）。"""
        usage = r.raw.get("usage") if isinstance(r.raw, dict) else None
        usage = usage if isinstance(usage, dict) else {}

        def tok(key: str) -> int:
            v = usage.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0

        return {"images": len(r.images), "input_tokens": tok("input_tokens"),
                "output_tokens": tok("output_tokens")}

    def generate(self, profile: str, req: ImageRequest, *, tag: str) -> tuple[ImageResult, Target]:
        return self._run(profile, "generate", tag,
                         lambda p, t: p.generate(self._merged(req, t), model=t.model),
                         self._image_tally)

    def edit(self, profile: str, req: ImageRequest, *, tag: str) -> tuple[ImageResult, Target]:
        return self._run(profile, "edit", tag,
                         lambda p, t: p.edit(self._merged(req, t), model=t.model),
                         self._image_tally)
