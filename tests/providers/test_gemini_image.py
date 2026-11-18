import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
import responses as rsps
from PIL import Image

from game_art.config import ProviderConfig
from game_art.errors import InputError, UpstreamError
from game_art.imaging import to_png_bytes
from game_art.providers import gemini_image
from game_art.providers.base import ImageRequest
from game_art.providers.openai_compat import OpenAICompatProvider

BASE = "https://api.example.test/v1"
URL = "https://api.example.test/gemini/v1beta/models/gemini-x-image:generateContent"


def _img(w: int = 8, h: int = 8) -> Image.Image:
    return Image.new("RGB", (w, h), (255, 0, 0))


def _b64(w: int = 8, h: int = 8) -> str:
    return base64.b64encode(to_png_bytes(_img(w, h))).decode()


def _reply(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}}]}


def _image_part(w: int = 8, h: int = 8) -> dict[str, Any]:
    return {"inlineData": {"mimeType": "image/png", "data": _b64(w, h)}}


@pytest.fixture
def mocked() -> Iterator[rsps.RequestsMock]:
    with rsps.RequestsMock() as m:
        yield m


@pytest.fixture
def prov() -> OpenAICompatProvider:
    cfg = ProviderConfig(type="openai_compatible", base_url=BASE, gemini_image_models=["gemini-*"])
    return OpenAICompatProvider("p", cfg, "k")


def _sent(mocked: rsps.RequestsMock, i: int = 0) -> dict[str, Any]:
    body = mocked.calls[i].request.body
    assert isinstance(body, bytes | str)
    data = json.loads(body)
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize(("size", "ratio"), [
    ("1024x1024", "1:1"), ("1536x1024", "3:2"), ("1024x1536", "2:3"),
    ("1920x1080", "16:9"), ("1080X1920", "9:16"), ("1280x1024", "5:4"),
])
def test_aspect_ratio_picks_nearest(size: str, ratio: str) -> None:
    assert gemini_image.aspect_ratio(size) == ratio


@pytest.mark.parametrize("size", ["", "big", "1024", "0x10", "10x-1"])
def test_aspect_ratio_rejects_bad_size(size: str) -> None:
    with pytest.raises(InputError) as e:
        gemini_image.aspect_ratio(size)
    assert e.value.code == "bad_size"


def test_generate_uses_native_endpoint(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(URL, json=_reply({"text": "好的"}, _image_part(12, 8)))
    res = prov.generate(ImageRequest(prompt="江上孤舟", negative="文字", size="1536x1024",
                                     params={"quality": "medium"}), model="gemini-x-image")
    assert [i.size for i in res.images] == [(12, 8)] and res.images[0].mode == "RGBA"
    assert res.partial is False and res.seed is None
    assert "<b64 omitted>" in json.dumps(res.raw) and _b64(12, 8) not in json.dumps(res.raw)
    body = _sent(mocked)
    assert body["contents"][0]["parts"] == [{"text": "江上孤舟\n避免：文字"}]
    assert body["generationConfig"] == {"responseModalities": ["TEXT", "IMAGE"],
                                        "imageConfig": {"aspectRatio": "3:2"}}
    assert mocked.calls[0].request.headers["x-goog-api-key"] == "k"


def test_params_override_ratio_and_resolution(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(URL, json=_reply(_image_part()))
    prov.generate(ImageRequest(prompt="p", params={"aspect_ratio": "21:9", "image_size": "2K"}),
                  model="gemini-x-image")
    assert _sent(mocked)["generationConfig"]["imageConfig"] == {"aspectRatio": "21:9", "imageSize": "2K"}


def test_n_images_means_n_requests(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(URL, json=_reply(_image_part()))
    mocked.post(URL, json=_reply(_image_part()))
    mocked.post(URL, status=500, body="boom")
    res = prov.generate(ImageRequest(prompt="p", n=3), model="gemini-x-image")
    assert len(res.images) == 2 and res.partial is True  # 第三张失败：保留前两张


def test_first_failure_raises_with_reason(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(URL, status=400, json={"error": {"message": "bad ratio"}})
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p", n=2), model="gemini-x-image")
    assert e.value.code == "http_400" and "bad ratio" in e.value.message


def test_text_only_reply_is_no_images(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(URL, json=_reply({"text": "我不能画这个"}))
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="gemini-x-image")
    assert e.value.code == "no_images" and "我不能画这个" in e.value.details["body"]


def test_thought_images_and_garbage_are_handled(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    draft = {**_image_part(4, 4), "thought": True}
    mocked.post(URL, json=_reply(draft, _image_part(8, 8)))
    assert prov.generate(ImageRequest(prompt="p"), model="gemini-x-image").images[0].size == (8, 8)
    mocked.post(URL, json=_reply({"inlineData": {"mimeType": "image/png", "data": "bm90IGFuIGltYWdl"}}))
    with pytest.raises(UpstreamError) as e:
        prov.generate(ImageRequest(prompt="p"), model="gemini-x-image")
    assert e.value.code == "not_an_image"


def test_edit_sends_reference_images(prov: OpenAICompatProvider, mocked: rsps.RequestsMock) -> None:
    mocked.post(URL, json=_reply(_image_part()))
    prov.edit(ImageRequest(prompt="改成夜晚", refs=[_img(), _img()]), model="gemini-x-image")
    parts = _sent(mocked)["contents"][0]["parts"]
    assert parts[0] == {"text": "改成夜晚"} and len(parts) == 3
    assert parts[1]["inlineData"] == {"mimeType": "image/png", "data": _b64()}


def test_mask_is_rejected_before_any_request(prov: OpenAICompatProvider) -> None:
    with pytest.raises(InputError) as e:
        prov.edit(ImageRequest(prompt="p", refs=[_img()], mask=_img()), model="gemini-x-image")
    assert e.value.code == "mask_unsupported"


def test_other_models_keep_openai_endpoint_and_base_url_override(mocked: rsps.RequestsMock) -> None:
    cfg = ProviderConfig(type="openai_compatible", base_url=BASE, gemini_image_models=["gemini-*"],
                         gemini_base_url="https://g.example.test/root/")
    prov = OpenAICompatProvider("p", cfg, "k")
    mocked.post(f"{BASE}/images/generations", json={"data": [{"b64_json": _b64()}]})
    prov.generate(ImageRequest(prompt="p"), model="gpt-image-1")
    mocked.post("https://g.example.test/root/v1beta/models/gemini-y:generateContent", json=_reply(_image_part()))
    prov.generate(ImageRequest(prompt="p"), model="gemini-y")
