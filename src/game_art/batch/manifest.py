"""批次清单模型与加载。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from game_art.errors import InputError
from game_art.paths import resolve_repo_path

_ID = re.compile(r"^[A-Za-z0-9_\-一-鿿][A-Za-z0-9_.\-一-鿿]*$")


def _check_id(v: str, what: str) -> str:
    if not _ID.match(v):
        raise ValueError(f"{what} 只能含字母、数字、汉字、_ - .，且不能以 . 开头：{v!r}")
    return v


def _check_sets(v: dict[str, Any]) -> dict[str, Any]:
    bad = [k for k in v if "." not in k]
    if bad:
        raise ValueError(f"参数键应为 <节点>.<输入>：{', '.join(bad)}")
    return v


class Item(BaseModel):
    id: str
    brief: str
    sets: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if re.search(r"_\d+$", v):
            raise ValueError(f"项 id 不能以 _<数字> 结尾（会与多选定稿文件 <项>_<k>.png 重名）：{v!r}")
        return _check_id(v, "项 id")

    @field_validator("sets")
    @classmethod
    def _sets(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _check_sets(v)


class Manifest(BaseModel):
    batch_id: str
    workflow: Path
    style_card: Path | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)
    candidates: int = Field(2, ge=1)
    qc_threshold: float = 6.0
    max_retries: int | None = Field(None, ge=0)
    budget: float | None = Field(None, ge=0)
    items: list[Item] = Field(min_length=1)

    @field_validator("batch_id")
    @classmethod
    def _bid(cls, v: str) -> str:
        return _check_id(v, "batch_id")

    @field_validator("defaults")
    @classmethod
    def _defaults(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _check_sets(v)

    @model_validator(mode="after")
    def _unique(self) -> Manifest:
        ids = [i.id for i in self.items]
        dup = sorted({x for x in ids if ids.count(x) > 1})
        if dup:
            raise ValueError(f"项 id 重复：{', '.join(dup)}")
        return self

    def item(self, item_id: str) -> Item:
        return next(i for i in self.items if i.id == item_id)


def _resolve(value: Any, base: Path) -> Path | None:
    if value is None:
        return None
    p = Path(resolve_repo_path(str(value))).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load_manifest(path: Path) -> Manifest:
    path = Path(path).expanduser()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise InputError("bad_manifest", f"读取批次清单失败：{path}（{e}）") from e
    if not isinstance(raw, dict):
        raise InputError("bad_manifest", f"批次清单顶层必须是映射：{path}")
    base = path.resolve().parent
    for key in ("workflow", "style_card"):
        if key in raw:
            raw[key] = _resolve(raw[key], base)
    try:
        return Manifest.model_validate(raw)
    except ValidationError as e:
        msgs = "；".join(f"{'.'.join(map(str, err['loc'])) or '清单'}: {err['msg']}" for err in e.errors())
        raise InputError("bad_manifest", f"批次清单不合法：{path}：{msgs}") from e
