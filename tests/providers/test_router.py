import json
import pathlib
from typing import Any, cast

import pytest
from PIL import Image

from game_art.config import Pricing, Settings, Target
from game_art.errors import InputError, UpstreamError
from game_art.ledger import Ledger
from game_art.providers.base import ChatResult, ImageRequest, ImageResult, Message
from game_art.providers.router import Router


class Fake:
    def __init__(self, name: str, behaviors: list[Any]) -> None:
        self.name = name
        self.behaviors = list(behaviors)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _next(self, kind: str, **kw: Any) -> Any:
        self.calls.append((kind, kw))
        b = self.behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b

    def chat(self, messages: list[Message], *, model: str, json_mode: bool = False,
             params: dict[str, Any]) -> ChatResult:
        return cast(ChatResult, self._next("chat", model=model, json_mode=json_mode, params=params))

    def generate(self, req: ImageRequest, *, model: str) -> ImageResult:
        return cast(ImageResult, self._next("generate", req=req, model=model))

    def edit(self, req: ImageRequest, *, model: str) -> ImageResult:
        return cast(ImageResult, self._next("edit", req=req, model=model))


def ok_img(n: int = 1) -> ImageResult:
    return ImageResult(images=[Image.new("RGBA", (2, 2)) for _ in range(n)],
                       partial=False, seed=None, raw={})


def e429() -> UpstreamError:
    return UpstreamError("http_429", "限流", {"retryable": True, "status": 429})


def e400() -> UpstreamError:
    return UpstreamError("http_400", "参数错", {"status": 400})


def make(tmp_path: pathlib.Path, fakes: dict[str, Fake],
         profiles: dict[str, list[Target]] | None = None,
         pricing: dict[str, Pricing] | None = None) -> tuple[Router, Ledger]:
    s = Settings(
        profiles=profiles or {"image": [Target(provider=n, model=f"m-{n}") for n in fakes]},
        pricing=pricing or {}, comfy_root=tmp_path, config_dir=tmp_path, data_dir=tmp_path)
    ledger = Ledger(tmp_path / "ledger.jsonl", s.pricing)
    return Router(s, ledger, provider_factory=lambda _s, name: fakes[name]), ledger


def entries(tmp_path: pathlib.Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]


def test_fallback_on_429_and_both_recorded(tmp_path: pathlib.Path) -> None:
    fakes = {"first": Fake("first", [e429()]), "second": Fake("second", [ok_img()])}
    r, _ = make(tmp_path, fakes, pricing={"second:m-second": Pricing(per_image=0.1)})
    res, tgt = r.generate("image", ImageRequest(prompt="p"), tag="b/i/0")
    assert tgt.provider == "second" and len(res.images) == 1
    es = entries(tmp_path)
    assert [e["ok"] for e in es] == [False, True]
    assert es[0]["provider"] == "first" and "http_429" in es[0]["error"]
    assert es[1]["images"] == 1 and es[1]["cost"] == pytest.approx(0.1)
    assert es[1]["tag"] == "b/i/0" and es[1]["kind"] == "generate"


def test_non_retryable_400_also_falls_back(tmp_path: pathlib.Path) -> None:
    fakes = {"first": Fake("first", [e400()]), "second": Fake("second", [ok_img()])}
    r, _ = make(tmp_path, fakes)
    _, tgt = r.generate("image", ImageRequest(prompt="p"), tag="t")
    assert tgt.provider == "second"


def test_all_failed_lists_attempts(tmp_path: pathlib.Path) -> None:
    fakes = {"first": Fake("first", [e429()]), "second": Fake("second", [e400()])}
    r, _ = make(tmp_path, fakes)
    with pytest.raises(UpstreamError) as e:
        r.generate("image", ImageRequest(prompt="p"), tag="t")
    assert e.value.code == "all_targets_failed"
    att = e.value.details["attempts"]
    assert len(att) == 2
    assert att[0] == {"target": "first:m-first", "code": "http_429", "message": "限流"}
    assert att[1]["code"] == "http_400"
    assert [x["ok"] for x in entries(tmp_path)] == [False, False]


def test_non_gaerror_propagates(tmp_path: pathlib.Path) -> None:
    fakes = {"first": Fake("first", [KeyError("bug")]), "second": Fake("second", [ok_img()])}
    r, _ = make(tmp_path, fakes)
    with pytest.raises(KeyError):
        r.generate("image", ImageRequest(prompt="p"), tag="t")
    assert fakes["second"].calls == []


def test_unknown_profile_lists_available(tmp_path: pathlib.Path) -> None:
    r, _ = make(tmp_path, {"first": Fake("first", [])})
    with pytest.raises(InputError) as e:
        r.generate("nope", ImageRequest(prompt="p"), tag="t")
    assert "image" in str(e.value)


def test_inline_target_string(tmp_path: pathlib.Path) -> None:
    fakes = {"first": Fake("first", [ok_img()])}
    r, _ = make(tmp_path, fakes)
    _, tgt = r.generate("first:m9", ImageRequest(prompt="p"), tag="t")
    assert (tgt.provider, tgt.model) == ("first", "m9")
    assert fakes["first"].calls[0][1]["model"] == "m9"


def test_configured_profile_name_wins_over_inline(tmp_path: pathlib.Path) -> None:
    fakes = {"first": Fake("first", [ok_img()])}
    r, _ = make(tmp_path, fakes, profiles={"a:b": [Target(provider="first", model="real")]})
    _, tgt = r.generate("a:b", ImageRequest(prompt="p"), tag="t")
    assert tgt.model == "real"


def test_target_params_merged_request_wins(tmp_path: pathlib.Path) -> None:
    fakes = {"first": Fake("first", [ok_img()])}
    r, _ = make(tmp_path, fakes, profiles={
        "image": [Target(provider="first", model="m", params={"quality": "high", "x": 1})]})
    req = ImageRequest(prompt="p", params={"x": 2})
    r.generate("image", req, tag="t")
    got = fakes["first"].calls[0][1]["req"]
    assert got.params == {"quality": "high", "x": 2}
    assert req.params == {"x": 2}  # 调用方对象不被修改


def test_edit_routes_and_records(tmp_path: pathlib.Path) -> None:
    fakes = {"first": Fake("first", [ok_img(2)])}
    r, _ = make(tmp_path, fakes)
    _, _ = r.edit("image", ImageRequest(prompt="p", refs=[Image.new("RGB", (2, 2))]), tag="t")
    assert fakes["first"].calls[0][0] == "edit"
    e = entries(tmp_path)[0]
    assert e["kind"] == "edit" and e["images"] == 2


def test_chat_records_tokens_and_merges_params(tmp_path: pathlib.Path) -> None:
    cr = ChatResult(text="hi", input_tokens=11, output_tokens=7, raw={})
    fakes = {"first": Fake("first", [cr])}
    r, _ = make(tmp_path, fakes, profiles={
        "text": [Target(provider="first", model="m", params={"temperature": 0})]})
    res, _ = r.chat("text", [Message(role="user", text="yo")], tag="t", json_mode=True)
    assert res.text == "hi"
    kw = fakes["first"].calls[0][1]
    assert kw["json_mode"] is True and kw["params"] == {"temperature": 0}
    e = entries(tmp_path)[0]
    assert (e["kind"], e["input_tokens"], e["output_tokens"], e["ok"]) == ("chat", 11, 7, True)


def test_factory_error_counts_as_failed_attempt(tmp_path: pathlib.Path) -> None:
    fakes = {"second": Fake("second", [ok_img()])}

    def factory(_s: Settings, name: str) -> Fake:
        if name == "first":
            raise InputError("missing_api_key", "缺少环境变量 K")
        return fakes[name]

    s = Settings(profiles={"image": [Target(provider="first", model="a"),
                                     Target(provider="second", model="b")]},
                 comfy_root=tmp_path, config_dir=tmp_path, data_dir=tmp_path)
    r = Router(s, Ledger(tmp_path / "ledger.jsonl", {}), provider_factory=factory)
    _, tgt = r.generate("image", ImageRequest(prompt="p"), tag="t")
    assert tgt.provider == "second"
    assert [x["ok"] for x in entries(tmp_path)] == [False, True]


def test_from_settings(tmp_path: pathlib.Path) -> None:
    s = Settings(comfy_root=tmp_path, config_dir=tmp_path, data_dir=tmp_path)
    r = Router.from_settings(s)
    assert r.ledger.path == tmp_path / "ledger.jsonl"


def test_all_failed_message_includes_code_and_message(tmp_path: pathlib.Path) -> None:
    fakes = {"first": Fake("first", [e429()]), "second": Fake("second", [e400()])}
    r, _ = make(tmp_path, fakes)
    with pytest.raises(UpstreamError) as e:
        r.generate("image", ImageRequest(prompt="p"), tag="t")
    assert "http_429: 限流" in e.value.message and "http_400: 参数错" in e.value.message
    assert e.value.exit_code == 4


def test_all_local_input_errors_reraise_input_error(tmp_path: pathlib.Path) -> None:
    def factory(_s: Settings, name: str) -> Fake:
        raise InputError("missing_api_key", f"缺少环境变量 {name.upper()}_KEY，可写入 /x/.env")

    s = Settings(profiles={"image": [Target(provider="first", model="a"),
                                     Target(provider="second", model="b")]},
                 comfy_root=tmp_path, config_dir=tmp_path, data_dir=tmp_path)
    r = Router(s, Ledger(tmp_path / "ledger.jsonl", {}), provider_factory=factory)
    with pytest.raises(InputError) as e:
        r.generate("image", ImageRequest(prompt="p"), tag="t")
    assert e.value.exit_code == 3 and e.value.code == "missing_api_key"
    assert e.value.message.startswith("缺少环境变量 FIRST_KEY，可写入 /x/.env")
    assert "SECOND_KEY" in e.value.message
    assert [a["code"] for a in e.value.details["attempts"]] == ["missing_api_key", "missing_api_key"]


def test_mixed_input_and_upstream_errors_stay_upstream(tmp_path: pathlib.Path) -> None:
    fakes = {"second": Fake("second", [e429()])}

    def factory(_s: Settings, name: str) -> Fake:
        if name == "first":
            raise InputError("missing_api_key", "缺少环境变量 K")
        return fakes[name]

    s = Settings(profiles={"image": [Target(provider="first", model="a"),
                                     Target(provider="second", model="b")]},
                 comfy_root=tmp_path, config_dir=tmp_path, data_dir=tmp_path)
    r = Router(s, Ledger(tmp_path / "ledger.jsonl", {}), provider_factory=factory)
    with pytest.raises(UpstreamError) as e:
        r.generate("image", ImageRequest(prompt="p"), tag="t")
    assert "missing_api_key: 缺少环境变量 K" in e.value.message


def test_image_usage_tokens_recorded_in_ledger(tmp_path: pathlib.Path) -> None:
    res = ImageResult(images=[Image.new("RGBA", (2, 2))], partial=False, seed=None,
                      raw={"data": [{}], "usage": {"input_tokens": 50, "output_tokens": 1056, "total_tokens": 1106}})
    fakes = {"first": Fake("first", [res, ok_img()])}
    r, _ = make(tmp_path, fakes, pricing={"first:m-first": Pricing(per_image=0.04)})
    r.generate("image", ImageRequest(prompt="p"), tag="t")
    r.generate("image", ImageRequest(prompt="p"), tag="t")  # 无 usage：记 0
    e = entries(tmp_path)
    assert (e[0]["input_tokens"], e[0]["output_tokens"], e[0]["images"]) == (50, 1056, 1)
    assert e[0]["cost"] == pytest.approx(0.04)  # 只配了 per_image：费用不变
    assert (e[1]["input_tokens"], e[1]["output_tokens"]) == (0, 0)
