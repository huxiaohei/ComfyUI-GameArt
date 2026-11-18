import base64
import json
import pathlib
from collections.abc import Iterator
from typing import NoReturn

import pytest
import responses as rsps
from PIL import Image

from game_art.config import ProviderConfig, Settings
from game_art.errors import InputError, UpstreamError
from game_art.imaging import to_png_bytes
from game_art.providers.base import ImageRequest, Message
from game_art.providers.openai_compat import OpenAICompatProvider
from game_art.providers.registry import get_provider

BASE = "https://api.example.test/v1"


def png_bytes(w: int = 8, h: int = 8, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    return to_png_bytes(Image.new("RGB", (w, h), color))


def png_b64(w: int = 8, h: int = 8) -> str:
    return base64.b64encode(png_bytes(w, h)).decode()


def _body_bytes(mocked: rsps.RequestsMock, i: int) -> bytes:
    body = mocked.calls[i].request.body
    assert isinstance(body, bytes | str)
    return body.encode() if isinstance(body, str) else body


@pytest.fixture
def mocked() -> Iterator[rsps.RequestsMock]:
    with rsps.RequestsMock() as m:
        yield m


@pytest.fixture
def prov() -> OpenAICompatProvider:
    cfg = ProviderConfig(type="openai_compatible", base_url=BASE, api_key_env="K", timeout=5)
    return OpenAICompatProvider("p", cfg, "k")


def test_generate_b64(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations", json={"data": [{"b64_json": png_b64()}]})
    r = prov.generate(ImageRequest(prompt="p", transparent=True), model="m")
    assert r.images[0].mode == "RGBA" and not r.partial
    body = json.loads(_body_bytes(mocked, 0))
    assert body["background"] == "transparent" and body["model"] == "m"
    assert body["output_format"] == "png"
    assert "b64" not in json.dumps(r.raw) or "omitted" in json.dumps(r.raw)
    assert png_b64() not in json.dumps(r.raw)


def test_generate_url_is_downloaded(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations", json={"data": [{"url": "https://cdn.test/a.png"}]})
    mocked.get("https://cdn.test/a.png", body=png_bytes(), content_type="image/png")
    r = prov.generate(ImageRequest(prompt="p"), model="m")
    assert r.images[0].size == (8, 8)


def test_url_download_failure_raises(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations", json={"data": [{"url": "https://cdn.test/a.png"}]})
    mocked.get("https://cdn.test/a.png", status=404, body="nope")
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert e.value.code == "download_failed"


def test_generate_fewer_than_requested_is_partial(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations",
                json={"data": [{"b64_json": png_b64()}, {"b64_json": png_b64()}]})
    r = prov.generate(ImageRequest(prompt="p", n=3), model="m")
    assert r.partial is True and len(r.images) == 2


def test_non_image_payload_raises_upstream(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations",
                json={"data": [{"b64_json": base64.b64encode(b"hello").decode()}]})
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert e.value.code == "not_an_image" and "body" in e.value.details


def test_429_marked_retryable(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations", status=429, json={"error": "rate"})
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert e.value.details["retryable"] is True and e.value.details["status"] == 429
    assert e.value.code == "http_429"


def test_500_retryable_400_not(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations", status=502, body="bad gw")
    mocked.post(f"{BASE}/images/generations", status=400, body="bad req")
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert e.value.details["retryable"] is True
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert "retryable" not in e.value.details and e.value.details["body"] == "bad req"


def test_body_truncated_to_2000(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations", status=400, body="x" * 5000)
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert len(e.value.details["body"]) == 2000


def test_connection_error_retryable(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    import requests
    mocked.post(f"{BASE}/images/generations", body=requests.ConnectionError("boom"))
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert e.value.details["retryable"] is True and e.value.code == "network_error"


def test_negative_appended_and_params_merged(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations", json={"data": [{"b64_json": png_b64()}]})
    prov.generate(ImageRequest(prompt="cat", negative="blur", params={"quality": "high"}),
                  model="m")
    body = json.loads(_body_bytes(mocked, 0))
    assert body["prompt"] == "cat\n避免：blur" and body["quality"] == "high"


def test_edit_sends_multipart_with_refs(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/edits", json={"data": [{"b64_json": png_b64()}]})
    refs = [Image.new("RGB", (4, 4)), Image.new("RGB", (5, 5))]
    r = prov.edit(ImageRequest(prompt="p", refs=refs, mask=Image.new("L", (4, 4))), model="m")
    assert r.images[0].mode == "RGBA"
    req = mocked.calls[0].request
    assert req.headers["Content-Type"].startswith("multipart/form-data")
    raw = _body_bytes(mocked, 0)
    assert raw.count(b'name="image[]"') == 2
    assert b'name="mask"' in raw and b'name="model"' in raw


def test_edit_requires_refs(prov: OpenAICompatProvider) -> None:
    with pytest.raises(InputError):
        prov.edit(ImageRequest(prompt="p"), model="m")


def test_chat_with_image_uses_data_url(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/chat/completions", json={
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4}})
    r = prov.chat([Message(role="user", text="look", images=[Image.new("RGB", (4, 4))])],
                  model="m", params={"temperature": 0})
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi", 3, 4)
    body = json.loads(_body_bytes(mocked, 0))
    content = body["messages"][0]["content"]
    img = next(c for c in content if c["type"] == "image_url")
    assert img["image_url"]["url"].startswith("data:image/png;base64,")
    assert body["temperature"] == 0


def test_chat_json_mode_sets_response_format(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/chat/completions", json={"choices": [{"message": {"content": "{}"}}]})
    r = prov.chat([Message(role="user", text="x")], model="m", json_mode=True, params={})
    assert json.loads(_body_bytes(mocked, 0))["response_format"] == {"type": "json_object"}
    assert r.input_tokens == 0


def test_api_key_in_auth_header(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/chat/completions", json={"choices": [{"message": {"content": "x"}}]})
    prov.chat([Message(role="user", text="x")], model="m", params={})
    assert mocked.calls[0].request.headers["Authorization"] == "Bearer k"


def _settings(tmp_path: pathlib.Path, type_: str = "openai_compatible") -> Settings:
    return Settings(providers={"p": ProviderConfig(type=type_, base_url=BASE, api_key_env="GA_T3_K")},
                    comfy_root=tmp_path, config_dir=tmp_path)


def test_registry(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GA_T3_K", "sekret")
    p = get_provider(_settings(tmp_path), "p")
    assert isinstance(p, OpenAICompatProvider) and p.name == "p"
    with pytest.raises(InputError):
        get_provider(_settings(tmp_path, "weird"), "p")
    with pytest.raises(InputError):
        get_provider(_settings(tmp_path), "nope")


def test_generic_request_exception_is_retryable_network_error(
        prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    import requests
    mocked.post(f"{BASE}/images/generations", body=requests.exceptions.ChunkedEncodingError("x"))
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert e.value.code == "network_error" and e.value.details["retryable"] is True


def test_download_generic_request_exception(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    import requests
    mocked.post(f"{BASE}/images/generations", json={"data": [{"url": "https://cdn.test/a.png"}]})
    mocked.get("https://cdn.test/a.png", body=requests.exceptions.ContentDecodingError("x"))
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert e.value.code == "download_failed" and e.value.details["retryable"] is True


def test_decompression_bomb_is_not_an_image(
        prov: OpenAICompatProvider, mocked: rsps.RequestsMock, monkeypatch: pytest.MonkeyPatch) -> None:
    mocked.post(f"{BASE}/images/generations", json={"data": [{"b64_json": png_b64()}]})

    def boom(_: bytes) -> NoReturn:
        raise Image.DecompressionBombError("too big")
    monkeypatch.setattr("game_art.providers.openai_compat.from_bytes", boom)
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert e.value.code == "not_an_image"


def test_seed_none_unless_sent_upstream(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations", json={"data": [{"b64_json": png_b64()}]})
    mocked.post(f"{BASE}/images/generations", json={"data": [{"b64_json": png_b64()}]})
    r = prov.generate(ImageRequest(prompt="p", seed=42), model="m")
    assert r.seed is None and "seed" not in json.loads(_body_bytes(mocked, 0))
    r = prov.generate(ImageRequest(prompt="p", seed=42, params={"seed": 7}), model="m")
    assert r.seed == 7 and json.loads(_body_bytes(mocked, 1))["seed"] == 7


def test_registry_uses_base_url_from_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GA_T3_K", "k")
    monkeypatch.setenv("GA_T3_BASE", "https://proxy.test/v1/")
    s = Settings(providers={"p": ProviderConfig(type="openai_compatible", base_url_env="GA_T3_BASE",
                                                api_key_env="GA_T3_K")},
                 comfy_root=tmp_path, config_dir=tmp_path)
    p = get_provider(s, "p")
    assert isinstance(p, OpenAICompatProvider)
    assert p._base == "https://proxy.test/v1"


def test_error_message_carries_upstream_reason(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(f"{BASE}/images/generations", status=400,
                json={"error": {"message": "Unknown name \"prompt\":\nCannot find field." + "x" * 500}})
    mocked.post(f"{BASE}/images/generations", status=429, json={"error": "rate"})
    mocked.post(f"{BASE}/images/generations", status=400, body="bad req")
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert '原因：Unknown name "prompt": Cannot find field.' in e.value.message
    assert len(e.value.message) < 400
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert e.value.message.endswith("原因：rate")
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="m")
    assert "原因" not in e.value.message
