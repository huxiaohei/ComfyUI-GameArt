"""提示词扩写：大白话描述 + 风格卡 → 出图用的正/反向提示词。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from game_art.briefs import load_briefs
from game_art.errors import InputError
from game_art.paths import resolve_repo_path
from game_art.steps import ASSET_TYPES, expand_key, expand_prompt, feedback_lines
from game_art.style import StyleCard

from .. import common
from ..common import CATEGORY, META_OUT, STYLE_IN, TAG_IN, Recorder, always_changed, ga_errors, model_input, resolve_tag

# 把 .yaml 拖到节点上、又拿不到完整路径时，编辑器在 briefs_path 里填 "拖入:<文件名>"（和风格卡节点同一个约定）。
# 要和 web/prompt_expand.js 里的 DROPPED_PREFIX 保持一致。
DROPPED_PREFIX = "拖入:"


CACHE_FILE = "expand_cache.json"
CACHE_LIMIT = 300  # 最多记多少条，超过后丢掉最早的
_MEMORY: dict[str, dict[str, Any]] = {}  # 找不到配置目录（比如测试里）时只记在内存里


def _cache_file(router: Any) -> Path | None:
    data_dir = getattr(getattr(router, "settings", None), "data_dir", None)
    return Path(data_dir) / CACHE_FILE if data_dir else None


def _read_cache(file: Path | None) -> dict[str, dict[str, Any]]:
    if file is None:
        return _MEMORY
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("prompt"), str)
            and isinstance(v.get("negative"), str)} if isinstance(data, dict) else {}


def _write_cache(file: Path | None, key: str, entry: dict[str, Any]) -> None:
    data = _read_cache(file)
    data.pop(key, None)
    data[key] = entry  # 放到最后：最近用过的最后被丢
    while len(data) > CACHE_LIMIT:
        data.pop(next(iter(data)))
    if file is None:
        return
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 记不下来不影响出图，只是下次要重新扩写


class GAPromptExpand:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "GA_META")
    RETURN_NAMES = ("prompt", "negative", "meta")
    DESCRIPTION = ("提示词扩写：把一句大白话描述，结合风格卡，交给文字模型改写成出图模型能用好的专业提示词。"
                   "每运行一次调用一次文字模型（费用很低）。")
    OUTPUT_TOOLTIPS = ("扩写后的正向提示词：希望画面里出现的内容。", "反向提示词：不希望画面里出现的内容。",
                       META_OUT)

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "brief": ("STRING", {"multiline": True, "default": "", "placeholder": "用一两句话描述想要的素材",
                                     "tooltip": (
                    "你想要什么素材，用大白话写一两句即可，例如「一座圆筒形的茅草顶小粮仓」。"
                    "不用写专业提示词，这个节点会替你扩写。不要要求画文字，文字由模板合成。")}),
                "asset_type": (list(ASSET_TYPES), {"default": "icon", "tooltip": (
                    "素材的种类，决定构图方式：\n" + "\n".join(f"· {k}：{v}" for k, v in ASSET_TYPES.items()))}),
                "profile": model_input("text", "扩写提示词"),
            },
            "optional": {
                "style": STYLE_IN,
                "ref_description": ("STRING", {"default": "", "multiline": True,
                                               "placeholder": "参考图的文字描述（可留空）", "tooltip": (
                    "对参考图的文字描述，可留空。在「参考图 → 资源」流程里由「参考图分析」节点自动填入，"
                    "让扩写出的提示词贴近参考图。")}),
                "briefs_path": ("STRING", {"default": "", "tooltip": (
                    "描述清单（.yaml）的路径，可留空。清单里列出一批素材，每项一个 id 和一段描述，"
                    "填好后点下面的 item 选一项，它的描述会自动填进 brief。\n"
                    "可以写完整路径或 @repo/...，也可以把 .yaml 文件直接拖到本节点上；"
                    "拿不到文件完整路径时这里显示「拖入:文件名」，之后文件有改动需要重新拖一次。\n"
                    "格式见 examples/briefs/demo.yaml。")}),
                "item": ("STRING", {"default": "", "tooltip": (
                    "从描述清单里选哪一项。在编辑器里点一下这一栏会列出清单里的所有项；选中后，"
                    "这一项的描述填进 brief、素材种类填进 asset_type，「保存资产」的文件名改成这一项的 id，"
                    "并清空上一项留下的修订意见。brief 填进来之后还可以接着改，实际出图以 brief 里的文字为准。")}),
                "feedback": ("STRING", {"default": "", "multiline": True,
                                        "placeholder": "修订意见（一般留空，由「质检结果」节点自动填入）", "tooltip": (
                    "对上一版图片的修订意见，一行一条，留空表示第一次出图。\n"
                    "在「质检结果」节点上勾选问题、点「采纳并重新生成」，勾选的问题会自动追加到这里；"
                    "也可以自己手写。扩写时模型会把每一条改写成正面的画面描述并入提示词。\n"
                    "换了一个素材、或者想从头再来时，把这里清空（「质检结果」节点上有「清空修订意见」按钮）。")}),
                "tag": TAG_IN,
                "reuse": ("BOOLEAN", {"default": True, "tooltip": (
                    "打开（默认）：描述、素材种类、风格卡、参考图描述、修订意见都和之前某一次完全一样时，"
                    "直接沿用那次扩写出的提示词，不再调用文字模型。同一个素材反复出图用的是同一段提示词，画风更稳。\n"
                    "关闭：每次都重新扩写。想换一种写法碰碰运气时关掉。\n"
                    "记录保存在配置目录的 expand_cache.json 里，重启后仍然有效；改了上面任何一项都会重新扩写。")}),
            },
        }

    @ga_errors
    def run(self, brief: str, asset_type: str, profile: str = "text", style: StyleCard | None = None,
            ref_description: str = "", briefs_path: str = "", item: str = "", feedback: str = "",
            tag: str = "", reuse: bool = True) -> tuple[str, str, dict[str, Any]]:
        tag = resolve_tag(tag)
        item, briefs_path = item.strip(), briefs_path.strip()
        if not brief.strip() and item and briefs_path and not briefs_path.startswith(DROPPED_PREFIX):
            # 命令行用法：只给了清单和项的 id，没在编辑器里选过，这里直接从文件取描述
            briefs = load_briefs(Path(resolve_repo_path(briefs_path)))
            chosen = briefs.find(item)
            brief = briefs.full_brief(chosen)
            asset_type = chosen.asset_type or briefs.asset_type or asset_type
        if not brief.strip():
            raise InputError("no_brief", (
                "没有描述：请在 brief 里写想要什么，或者填好描述清单（briefs_path）后点 item 选一项"))
        router = common.get_router()
        key = expand_key(brief, asset_type=asset_type, style=style, ref_description=ref_description,
                         feedback=feedback, profile=profile)
        cache_file = _cache_file(router)
        cached = _read_cache(cache_file).get(key) if reuse else None
        if cached:
            prompt, negative, model = cached["prompt"], cached["negative"], cached.get("text_model")
        else:
            rec = Recorder(router)
            res = expand_prompt(rec, brief, asset_type=asset_type, style=style, ref_description=ref_description,
                                feedback=feedback, tag=tag, profile=profile)
            t = rec.target
            prompt, negative, model = res.prompt, res.negative, f"{t.provider}:{t.model}" if t else None
            # 关着"沿用"时也记下来：之后再打开，沿用的就是最近这一版
            _write_cache(cache_file, key, {"prompt": prompt, "negative": negative, "text_model": model})
        meta: dict[str, Any] = {"brief": brief, "prompt": prompt, "negative": negative, "tag": tag,
                                "text_model": model}
        if cached:
            meta["prompt_reused"] = True  # 这次没有调用文字模型，沿用了之前扩写的提示词
        if item:
            meta["item"] = item  # 这张图对应描述清单里的哪一项
        if feedback_lines(feedback):
            meta["feedback"] = feedback_lines(feedback)  # 这一版是带着哪些修订意见出的
        return (prompt, negative, meta)
