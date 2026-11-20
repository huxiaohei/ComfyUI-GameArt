"""GameArt ComfyUI 节点包：把 game_art 核心库暴露到编辑器里。"""
import importlib.util
import sys
from pathlib import Path

if importlib.util.find_spec("game_art") is None:
    _src = str(Path(__file__).resolve().parent.parent / "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .routes import register

WEB_DIRECTORY = "./web"  # 编辑器端脚本（风格卡读文件进编辑框；参考图节点接收拖进来的图片）
register()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
