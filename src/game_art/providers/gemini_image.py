"""Gemini 原生出图接口（generateContent）的请求体与响应解析。

Gemini 图片模型不走 OpenAI 的 /images/generations：没有 size / n，画幅用宽高比表示，
一次只出一张图，参考图和提示词一起放在 contents 里。
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from PIL import Image, UnidentifiedImageError

from ..errors import InputError, UpstreamError
from ..imaging import from_bytes, to_png_bytes
from .base import ImageRequest

ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
_BODY_LIMIT = 2000


def aspect_ratio(size: str) -> str:
    """把 "宽x高" 换算成 Gemini 支持的最接近的宽高比。"""
    w, sep, h = size.lower().partition("x")
    if not sep or not w.strip().isdigit() or not h.strip().isdigit() or int(w) <= 0 or int(h) <= 0:
        raise InputError("bad_size", f"尺寸应为 宽x高（如 1536x1024）: {size!r}")
    want = int(w) / int(h)

    def distance(ratio: str) -> float:
        a, b = ratio.split(":")
        return abs(int(a) / int(b) - want)

    return min(ASPECT_RATIOS, key=distance)


def build_body(req: ImageRequest, prompt: str) -> dict[str, Any]:
    """generateContent 的请求体。req.params 里可用 aspect_ratio / image_size 覆盖画幅和清晰度（1K/2K/4K）。"""
    if req.mask is not None:
        raise InputError("mask_unsupported", "Gemini 图片模型不支持遮罩局部重绘，请换用 gpt-image-1 或去掉遮罩")
    params = dict(req.params)
    image_config: dict[str, Any] = {"aspectRatio": str(params.pop("aspect_ratio", None) or aspect_ratio(req.size))}
    image_size = params.pop("image_size", None)
    if image_size:
        image_config["imageSize"] = str(image_size)
    params.pop("quality", None)  # 这是 OpenAI 图片接口的参数，Gemini 不认
    params.pop("input_fidelity", None)  # 同上：OpenAI 改图接口的"保真度"
    parts: list[dict[str, Any]] = [{"text": prompt}]
    parts += [{"inlineData": {"mimeType": "image/png",
                              "data": base64.b64encode(to_png_bytes(img)).decode("ascii")}}
              for img in req.refs]
    return {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": image_config, **params}}


def _strip_data(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("<b64 omitted>" if k == "data" and isinstance(v, str) else _strip_data(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_data(v) for v in obj]
    return obj


def parse_images(data: dict[str, Any]) -> tuple[list[Image.Image], dict[str, Any]]:
    """从 generateContent 的响应里取出图片，返回 (图片列表, 去掉图片数据后的原始响应)。"""
    raw = _strip_data(data)
    assert isinstance(raw, dict)
    snippet = json.dumps(raw, ensure_ascii=False)[:_BODY_LIMIT]
    images: list[Image.Image] = []
    candidates = data.get("candidates")
    for cand in candidates if isinstance(candidates, list) else []:
        content = cand.get("content") if isinstance(cand, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        for part in parts if isinstance(parts, list) else []:
            inline = (part.get("inlineData") or part.get("inline_data")) if isinstance(part, dict) else None
            if not isinstance(inline, dict) or not isinstance(inline.get("data"), str):
                continue
            if part.get("thought"):
                continue  # 思考过程里的草稿图不是成品
            try:
                images.append(from_bytes(base64.b64decode(inline["data"], validate=False)).convert("RGBA"))
            except (binascii.Error, UnidentifiedImageError, OSError, ValueError,
                    Image.DecompressionBombError) as e:
                raise UpstreamError("not_an_image", "上游返回的内容不是合法图片", {"body": snippet}) from e
    if not images:
        raise UpstreamError("no_images", "上游未返回图片（可能被内容安全策略拦截，或模型只回了文字）",
                            {"body": snippet})
    return images, raw
