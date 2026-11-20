"""编辑器用的小接口，只在 ComfyUI 进程里注册：
- 让「风格卡」节点能把文件内容读进编辑框；
- 弹出系统自带的"选择文件夹"窗口（网页拿不到硬盘上的完整路径，只能让本机的 ComfyUI 进程代劳）。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

from game_art.errors import GAError, InputError
from game_art.paths import resolve_repo_path
from game_art.style import load_style_card

ROUTE = "/game_art/style_card"
CHOOSE_FOLDER_ROUTE = "/game_art/choose_folder"
_TK_SCRIPT = (
    "import tkinter, tkinter.filedialog as f\n"
    "r = tkinter.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
    "print(f.askdirectory(title=__import__('sys').argv[1]) or '')\n")


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


def choose_folder_command(prompt: str, platform: str = sys.platform) -> list[str]:
    """弹出"选择文件夹"窗口的命令；选中的路径打印到标准输出，取消时输出为空或退出码非 0。
    macOS 用系统自带的 osascript；其他系统用当前 Python 的 tkinter。"""
    if platform == "darwin":
        safe = prompt.replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", "activate", "-e", f'POSIX path of (choose folder with prompt "{safe}")']
    return [sys.executable, "-c", _TK_SCRIPT, prompt]


def parse_chosen_folder(returncode: int, stdout: str) -> str:
    """命令的结果 → 文件夹路径；用户点了取消返回空字符串。去掉 osascript 加在末尾的斜杠。"""
    path = stdout.strip()
    if returncode != 0 or not path:
        return ""
    return path if path == "/" else path.rstrip("/")


def as_save_location(chosen: str, output_dir: str | None) -> str:
    """选中的文件夹 →「保存资产」subfolder 里该填什么：在输出目录里面的写成相对路径（换台机器也能用），
    其余写完整路径。"""
    if not chosen or not output_dir:
        return chosen
    root, target = Path(output_dir).resolve(), Path(chosen).resolve()
    if target == root:
        return ""
    return target.relative_to(root).as_posix() if root in target.parents else chosen


async def choose_folder(prompt: str = "选择保存文件夹") -> str:
    """在运行 ComfyUI 的这台机器上弹出选择窗口，等用户选完。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *choose_folder_command(prompt), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise InputError("choose_folder_unavailable", f"无法弹出选择文件夹的窗口：{e}") from e
    stdout, stderr = await proc.communicate()
    code = proc.returncode or 0
    err = stderr.decode("utf-8", "replace")
    # osascript 在用户点"取消"时退出码是 1、错误信息里带 -128；其他非 0 才是真的出错
    if code != 0 and "-128" not in err and err.strip():
        raise InputError("choose_folder_failed", f"选择文件夹的窗口出错了：{err.strip()[:300]}")
    return parse_chosen_folder(code, stdout.decode("utf-8", "replace"))


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

    async def choose_folder_handler(_request: Any) -> Any:
        try:
            path = await choose_folder()
            try:
                output_dir: str | None = importlib.import_module("folder_paths").get_output_directory()
            except (ImportError, AttributeError):
                output_dir = None
            return web.json_response({"path": path, "value": as_save_location(path, output_dir)})
        except GAError as e:
            return web.json_response({"error": e.message}, status=500)

    instance.routes.get(ROUTE)(handler)
    instance.routes.post(CHOOSE_FOLDER_ROUTE)(choose_folder_handler)
    return True
