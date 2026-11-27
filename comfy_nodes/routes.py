"""编辑器用的小接口，只在 ComfyUI 进程里注册：
- 让「风格卡」节点能把文件内容读进编辑框；
- 让「提示词扩写」节点能读描述清单（yaml），列出里面的每一项供选择；
- 弹出系统自带的"选择文件夹"窗口（网页拿不到硬盘上的完整路径，只能让本机的 ComfyUI 进程代劳）；
- 把「保存资产」节点上选中的一张图另存到选定的文件夹，或者直接存成定稿（不弹窗，存到节点的 subfolder）。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

from game_art.briefs import load_briefs, parse_briefs
from game_art.errors import GAError, InputError
from game_art.paths import resolve_repo_path
from game_art.style import load_style_card

from .nodes.save_asset import copy_asset, finalize_asset

ROUTE = "/game_art/style_card"
CHOOSE_FOLDER_ROUTE = "/game_art/choose_folder"
SAVE_AS_ROUTE = "/game_art/save_as"
SAVE_FINAL_ROUTE = "/game_art/save_final"
BRIEFS_ROUTE = "/game_art/briefs"
_LAST_SAVE_AS: dict[str, str] = {}  # 上次「另存」选的文件夹，下次弹窗从这里开始（只在这次 ComfyUI 运行期间记着）
_TK_SCRIPT = (
    "import tkinter, tkinter.filedialog as f\n"
    "r = tkinter.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
    "a = __import__('sys').argv\n"
    "print(f.askdirectory(title=a[1], initialdir=(a[2] if len(a) > 2 and a[2] else None)) or '')\n")


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


def briefs_items(path: str = "", text: str = "") -> dict[str, Any]:
    """读描述清单，返回 {"items": [{"id", "brief", "asset_type"}]}，brief 已经拼上公共文字。
    给了 text（拖进来的文件内容）就解析它，否则读 path 指向的文件。"""
    if text.strip():
        briefs = parse_briefs(text)
    elif path.strip():
        briefs = load_briefs(Path(resolve_repo_path(path.strip())))
    else:
        raise InputError("bad_briefs", "没有填写描述清单的文件路径")
    return {"items": [{"id": i.id, "brief": briefs.full_brief(i), "asset_type": i.asset_type or briefs.asset_type}
                      for i in briefs.items]}


def _quoted(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def choose_folder_command(prompt: str, platform: str = sys.platform, start: str = "") -> list[str]:
    """弹出"选择文件夹"窗口的命令；选中的路径打印到标准输出，取消时输出为空或退出码非 0。
    macOS 用系统自带的 osascript；其他系统用当前 Python 的 tkinter。
    start：窗口一打开就停在哪个文件夹（比如上次另存的位置）；不存在或没给就用系统默认的。"""
    start = start if start and Path(start).is_dir() else ""
    if platform == "darwin":
        where = f' default location POSIX file "{_quoted(start)}"' if start else ""
        return ["osascript", "-e", "activate", "-e",
                f'POSIX path of (choose folder with prompt "{_quoted(prompt)}"{where})']
    return [sys.executable, "-c", _TK_SCRIPT, prompt, start]


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


async def choose_folder(prompt: str = "选择保存文件夹", start: str = "") -> str:
    """在运行 ComfyUI 的这台机器上弹出选择窗口，等用户选完。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *choose_folder_command(prompt, start=start),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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

    async def briefs_handler(request: Any) -> Any:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise InputError("bad_briefs", "请求内容格式不对")
            return web.json_response(briefs_items(str(body.get("path") or ""), str(body.get("text") or "")))
        except GAError as e:
            return web.json_response({"error": e.message}, status=400)
        except ValueError:
            return web.json_response({"error": "请求内容不是合法的 JSON"}, status=400)

    async def save_as_handler(request: Any) -> Any:
        """「另存选中的图」：弹窗选文件夹，把这张图和它的记录复制过去。窗口默认停在上次另存的位置。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise InputError("bad_asset", "请求内容格式不对")
            src, name = str(body.get("path") or ""), str(body.get("name") or "")
            folder = await choose_folder(f"把 {name or '这张图'} 另存到哪个文件夹", start=_LAST_SAVE_AS.get("dir", ""))
            if not folder:
                return web.json_response({"cancelled": True})
            dest = copy_asset(src, folder, name)
            _LAST_SAVE_AS["dir"] = folder
            return web.json_response({"saved": str(dest)})
        except GAError as e:
            return web.json_response({"error": e.message}, status=400)
        except ValueError:
            return web.json_response({"error": "请求内容不是合法的 JSON"}, status=400)

    async def save_final_handler(request: Any) -> Any:
        """「保存为定稿」：不弹窗，把这张图和它的记录存成节点 subfolder 里的 <name>.png，已有的定稿直接替换；
        请求里 cleanup 为 true 时，存好后删掉这个名字的带编号候选图。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise InputError("bad_asset", "请求内容格式不对")
            output_dir = importlib.import_module("folder_paths").get_output_directory()
            dest, replaced, removed = finalize_asset(
                str(body.get("path") or ""), str(body.get("subfolder") or ""), str(body.get("name") or ""),
                output_dir, cleanup=body.get("cleanup") is True)
            return web.json_response({"saved": str(dest), "replaced": replaced, "removed": removed})
        except GAError as e:
            return web.json_response({"error": e.message}, status=400)
        except ValueError:
            return web.json_response({"error": "请求内容不是合法的 JSON"}, status=400)

    instance.routes.post(SAVE_AS_ROUTE)(save_as_handler)
    instance.routes.post(SAVE_FINAL_ROUTE)(save_final_handler)
    instance.routes.get(ROUTE)(handler)
    instance.routes.post(BRIEFS_ROUTE)(briefs_handler)
    instance.routes.post(CHOOSE_FOLDER_ROUTE)(choose_folder_handler)
    return True
