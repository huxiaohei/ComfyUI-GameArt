"""仓库路径辅助：把 "@repo/..." 占位符解析为仓库绝对路径（CLI 与 ComfyUI 节点共用）。"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PREFIX = "@repo/"


def resolve_repo_path(value: str) -> str:
    """开头的 "@repo/" 替换为仓库根目录；其他字符串原样返回。"""
    if isinstance(value, str) and value.startswith(REPO_PREFIX):
        return str(REPO_ROOT / value[len(REPO_PREFIX):])
    return value
