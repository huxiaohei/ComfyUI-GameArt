"""Provider 协议与请求/结果数据类。"""
from __future__ import annotations

from typing import Any, Literal, Protocol

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    role: Literal["system", "user", "assistant"]
    text: str
    images: list[Image.Image] = Field(default_factory=list)


class ChatResult(BaseModel):
    text: str
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any]


class ImageRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    prompt: str
    negative: str = ""
    size: str = "1024x1024"
    n: int = 1
    transparent: bool = False
    seed: int | None = None
    refs: list[Image.Image] = Field(default_factory=list)
    mask: Image.Image | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ImageResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    images: list[Image.Image]
    partial: bool
    seed: int | None
    raw: dict[str, Any]


class Provider(Protocol):
    name: str

    def chat(self, messages: list[Message], *, model: str, json_mode: bool = False,
             params: dict[str, Any]) -> ChatResult: ...

    def generate(self, req: ImageRequest, *, model: str) -> ImageResult: ...

    def edit(self, req: ImageRequest, *, model: str) -> ImageResult: ...
