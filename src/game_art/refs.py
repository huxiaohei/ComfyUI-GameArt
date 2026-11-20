"""参考图：把"一行一个路径"的文本解析成图片文件列表并读入。"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from .errors import InputError
from .paths import resolve_repo_path

_SPLIT = re.compile(r"[\n;]")


def parse_ref_paths(text: str, base: Path | None = None) -> list[Path]:
    """每行（或用分号隔开的每段）一个路径；空行和以 # 开头的行忽略；两端的引号去掉。
    "@repo/..." 表示仓库目录下的文件，"~" 表示用户主目录；相对路径相对 base（默认当前目录）。"""
    out: list[Path] = []
    for raw in _SPLIT.split(text or ""):
        line = raw.strip().strip("\"'").strip()
        if not line or line.startswith("#"):
            continue
        p = Path(resolve_repo_path(line)).expanduser()
        out.append(p if p.is_absolute() else ((base or Path.cwd()) / p).resolve())
    return out


def load_ref_images(paths: list[Path]) -> list[Image.Image]:
    """按顺序读入参考图（统一转成 RGBA，保留各自的原始尺寸）。"""
    refs: list[Image.Image] = []
    for p in paths:
        try:
            with Image.open(p) as im:
                refs.append(im.convert("RGBA"))
        except FileNotFoundError as e:
            raise InputError("image_not_found", f"参考图不存在：{p}") from e
        except OSError as e:
            raise InputError("image_unreadable", f"无法读取参考图 {p}：{e}") from e
    return refs


def fingerprint(paths: list[Path]) -> str:
    """路径 + 修改时间 + 大小；文件被替换后指纹会变，用来判断要不要重新读图。"""
    parts: list[str] = []
    for p in paths:
        try:
            st = p.stat()
            parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{p}:missing")
    return "|".join(parts)
