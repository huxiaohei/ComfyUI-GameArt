"""编辑器用的小接口：让「风格卡」节点能把文件内容读进编辑框。只在 ComfyUI 进程里注册。"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from game_art.errors import GAError, InputError
from game_art.paths import resolve_repo_path
from game_art.style import load_style_card

ROUTE = "/game_art/style_card"


def style_card_text(path: str) -> str:
    """返回风格卡文件的原文。只读 .yaml/.yml，且必须是一份合法的风格卡（这个接口不是通用的读文件口）。"""
    path = path.strip()
    if not path:
        raise InputError("bad_style_card", "没有填写风格卡文件路径")
    file = Path(resolve_repo_path(path)).expanduser()
    if file.suffix.lower() not in {".yaml", ".yml"}:
        raise InputError("bad_style_card", f"风格卡必须是 .yaml 文件：{file}")
    load_style_card(file)
    return file.read_text(encoding="utf-8")


def register() -> bool:
    """在 ComfyUI 里注册接口；不在 ComfyUI 里（如单元测试）时什么都不做。"""
    try:
        server: Any = importlib.import_module("server")
        web: Any = importlib.import_module("aiohttp.web")
        instance: Any = server.PromptServer.instance
    except (ImportError, AttributeError):
        return False

    async def handler(request: Any) -> Any:
        try:
            return web.json_response({"text": style_card_text(request.query.get("path", ""))})
        except GAError as e:
            return web.json_response({"error": e.message}, status=400)

    instance.routes.get(ROUTE)(handler)
    return True
