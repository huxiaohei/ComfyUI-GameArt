"""GameArt 节点：全部节点类 + 映射表。

约定见 tensors.py（IMAGE/MASK 形状与 mask = 1 - alpha）。
所有 GAError 在节点边界转成带中文信息的 RuntimeError，ComfyUI 编辑器会直接显示。
"""
from __future__ import annotations

import functools
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import torch
from PIL import Image

from game_art import imaging
from game_art.compose import compose, load_layout
from game_art.config import Settings, Target, find_config_path, load_settings
from game_art.errors import GAError, InputError, UpstreamError
from game_art.models import all_model_choices
from game_art.paths import resolve_repo_path
from game_art.providers.base import ChatResult, ImageRequest, ImageResult, Message
from game_art.providers.router import Router
from game_art.steps import ASSET_TYPES, analyze_reference, expand_prompt, qc_score
from game_art.style import StyleCard, load_style_card

from .tensors import pil_to_tensors, tensors_to_pils

CATEGORY = "GameArt"

# ---------------------------------------------------------------- 公共辅助
_STATE: dict[str, Any] = {"key": None, "router": None}


def _get_router() -> Router:
    """模块级懒加载并缓存 Router（含 Ledger）；配置路径或 mtime 变化时重载。"""
    path = find_config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    try:
        env_mtime = (path.resolve().parent / ".env").stat().st_mtime
    except OSError:
        env_mtime = None
    key = (str(path), mtime, env_mtime)
    if _STATE["key"] != key or _STATE["router"] is None:
        _STATE["router"] = Router.from_settings(load_settings(path))
        _STATE["key"] = key
    return cast(Router, _STATE["router"])


def _ga_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """GAError → RuntimeError（保留原异常为 __cause__）。"""
    @functools.wraps(fn)
    def wrapper(*a: P.args, **k: P.kwargs) -> R:
        try:
            return fn(*a, **k)
        except GAError as e:
            raise RuntimeError(f"{e.message}（{e.code}）") from e
    return wrapper


def _tag(tag: str, meta: dict[str, Any] | None = None) -> str:
    tag = (tag or "").strip()
    if tag:
        return tag
    if meta and meta.get("tag"):
        return str(meta["tag"])
    return f"run/{uuid.uuid4().hex[:12]}"


def _parse_size(size: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[xX×*]\s*(\d+)\s*", size or "")
    if not m or int(m[1]) <= 0 or int(m[2]) <= 0:
        raise InputError("bad_size", f"尺寸格式不对：{size!r}（应为 宽x高，例如 1024x1024）")
    return int(m[1]), int(m[2])


class _Recorder:
    """包一层 router，记下最后一次 chat 命中的目标（steps 不返回 Target）。"""
    def __init__(self, router: Router) -> None:
        self._router = router
        self.target: Target | None = None

    def chat(self, profile: str, messages: list[Message], *, tag: str,
             json_mode: bool = False) -> tuple[ChatResult, Target]:
        res, target = self._router.chat(profile, messages, tag=tag, json_mode=json_mode)
        self.target = target
        return res, target


def _image_meta(router: Router, meta: dict[str, Any] | None, *, result: ImageResult, target: Target,
                prompt: str, negative: str, size: str, tag: str) -> dict[str, Any]:
    """Generate/Edit 追加 provider/model/seed/partial/size/cost。

    cost = 命中目标在 ledger.pricing 里的 per_image * 实际返回张数（估算；
    不读回账本，也不含此前文本调用的 token 费）。
    """
    pricing = getattr(router.ledger, "pricing", {}).get(f"{target.provider}:{target.model}")
    cost: float = (pricing.per_image if pricing else 0.0) * len(result.images)
    out = dict(meta or {})
    out.update(prompt=prompt, negative=negative, tag=tag, provider=target.provider,
               model=target.model, seed=result.seed, partial=result.partial, size=size, cost=cost)
    return out


def _unify(images: list[Image.Image], size: tuple[int, int]) -> list[Image.Image]:
    if not images:
        raise UpstreamError("no_images", "上游没有返回任何图片")
    if len({im.size for im in images}) > 1:
        return [imaging.fit_canvas(im, size) for im in images]
    return images


def _load_refs(style: StyleCard) -> list[Image.Image]:
    refs: list[Image.Image] = []
    for p in style.reference_images:
        try:
            with Image.open(p) as im:
                refs.append(im.convert("RGBA"))
        except FileNotFoundError as e:
            raise InputError("image_not_found", f"风格卡参考图不存在：{p}") from e
        except OSError as e:
            raise InputError("image_unreadable", f"无法读取风格卡参考图 {p}：{e}") from e
    return refs


def _edit_mask(mask: torch.Tensor, size: tuple[int, int]) -> Image.Image:
    """MASK（1 = 要重画）→ 上游 PNG：alpha = 1 - mask（透明处 = 重画区域）。"""
    return tensors_to_pils(mask.new_zeros((1, size[1], size[0], 3)), mask)[0]


_TAG = ("STRING", {"default": "", "tooltip": (
    "记账标签，一般留空。命令行和批量运行时会自动填入，账本按它统计每次任务花了多少钱；"
    "留空时自动生成一个随机标签。")})
_META = ("GA_META", {"tooltip": (
    "上游节点传来的记录信息（原始描述、提示词、所用模型等）。接上以后，这些信息会一路带到"
    "「保存资产」，写进图片旁边的 .json 文件，方便以后追溯这张图是怎么来的。可以不接。")})
_STYLE_IN = ("GA_STYLE", {"tooltip": "接「风格卡」节点的输出。不接则不套用任何统一风格。"})
_META_OUT = "这一步的记录信息（提示词、模型、种子、估算费用等），接到下游节点的 meta。"
def _settings_or_none() -> Settings | None:
    try:
        return load_settings(find_config_path())
    except Exception:  # 配置有问题时节点仍要能加载；真正运行时会报出具体的配置错误
        return None


def _size_input() -> tuple[list[str], dict[str, Any]]:
    """"尺寸"下拉：选项来自配置的 image_sizes。"""
    s = _settings_or_none()
    choices = list(s.image_sizes) if s and s.image_sizes else ["1024x1024"]
    default = "1024x1024" if "1024x1024" in choices else choices[0]
    return (choices, {"default": default, "tooltip": (
        "图片尺寸，宽x高，单位像素。宽大于高是横图，高大于宽是竖图，相等是方图；尺寸越大越贵。\n"
        "默认的三个选项是 gpt-image-1 支持的全部尺寸：1024x1024（方）、1536x1024（横）、1024x1536（竖）。"
        "需要别的比例（如 16:9）时，选最接近的尺寸出图后再裁剪。\n"
        "Gemini 图片模型没有固定尺寸，只按宽高比出图：1536x1024 会换算成 3:2，实际得到约 1248x832 的图。\n"
        "换了出图模型、想增减选项：改 ~/.game_art/config.yaml 里的 image_sizes，然后刷新本页面。")})


_N = ("INT", {"default": 1, "min": 1, "max": 16, "tooltip": (
    "一次出几张图。同一段提示词会得到几张不同的结果，便于挑选。按张计费，张数越多越贵。")})
_TRANSPARENT = ("BOOLEAN", {"default": False, "tooltip": (
    "打开（true）：要求模型直接画成透明背景，图标、道具、牌面图案这类要叠在别的东西上的素材用它。"
    "关闭（false）：普通的带背景图片，背景、插画用它。\n"
    "注意：Gemini 图片模型不支持透明背景，这个开关对它们不起作用，需要透明底时用后面的「去背景」节点。")})


def _model_input(default: str, what: str) -> tuple[list[str], dict[str, Any]]:
    """"模型"下拉：配置文件里的（profile 名 + 用到的 provider:model）+ 各服务商 /models 列出的全部模型。"""
    s = _settings_or_none()
    choices = all_model_choices(s) if s else []
    if default not in choices:
        choices.insert(0, default)
    return (choices, {"default": default, "tooltip": (
        f"{what}用哪个模型。下拉里有两类选项：\n"
        "· 不带冒号的（如 text、image）是「模型组」：配置文件里预先排好的一组模型，按顺序尝试，"
        "前一个出错时自动换下一个。平时用它就行。\n"
        "· 带冒号的（如 aihubmix:gpt-4o）是直接指定某一个模型，冒号前是服务商，冒号后是模型名。\n"
        "带冒号的选项里，排在前面的是配置文件里用到的，后面是服务商当前提供的全部模型（自动获取，每小时更新一次，"
        "可在弹出的列表顶部输入文字筛选）。注意列表不区分用途：这里要选能做「" + what + "」的模型，"
        "选错类型（比如给出图节点选了聊天模型）运行时会报错。\n"
        "想增加服务商（包括本机运行的模型服务）：编辑 ~/.game_art/config.yaml，然后刷新本页面。")})


def _always_changed(cls: type, /, **_kw: Any) -> float:
    """调用云端的节点：每次都重新执行（NaN != NaN，ComfyUI 不会复用缓存的旧结果）。"""
    return float("nan")


def _changed_unless_seeded(cls: type, /, seed: int = -1, **_kw: Any) -> float:
    """seed < 0（随机）时每次重跑；固定 seed 时同输入可复用缓存。"""
    try:
        return float("nan") if int(seed) < 0 else seed
    except (TypeError, ValueError):
        return float("nan")


_PROMPT_IN = ("STRING", {"forceInput": True, "tooltip": (
    "正向提示词：告诉模型要画什么。这是一个接线口，一般接「提示词扩写」的 prompt 输出；"
    "想自己写提示词，就接一个「字符串」节点。")})
_NEGATIVE_IN = ("STRING", {"forceInput": True, "tooltip": (
    "反向提示词：告诉模型不要画什么（如模糊、水印、文字）。一般接「提示词扩写」的 negative 输出。")})
_IMAGE_OUT = ("生成的图片。", "透明度蒙版：记录图片哪些地方是透明的，和 image 一起接到下游，透明背景才不会丢。",
              _META_OUT)


# ---------------------------------------------------------------- 节点
class GAStyleCard:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("GA_STYLE",)
    RETURN_NAMES = ("style",)

    DESCRIPTION = ("风格卡：规定一整套素材共用的画风（色板、关键词、规则），保证批量出的图风格一致。"
                   "可以读文件，也可以直接在编辑框里写。")
    OUTPUT_TOOLTIPS = ("风格卡，接到「提示词扩写」「云端生图」等节点的 style。",)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {"path": ("STRING", {"default": "", "tooltip": (
                "风格卡文件（.yaml）的路径。以 @repo/ 开头表示本项目目录下的文件，"
                "例如 @repo/examples/style_cards/guochao.yaml；也可以写电脑上的完整路径。"
                "如果下面的编辑框里已经写了完整的风格内容，这里可以留空。")})},
            "optional": {"edit": ("STRING", {"default": "", "multiline": True, "placeholder": (
                "在这里直接编辑风格（可留空）。写了的字段会覆盖文件里的同名字段，例如：\n"
                "positive: 国潮插画, 平涂上色, 描金线条\n"
                "negative: 写实光影, 3D渲染\n"
                "palette: [\"#C8372D\", \"#D4A93C\"]\n"
                "rules:\n  - 色块之间用细金线勾边"), "tooltip": (
                "直接编辑风格内容，格式是「字段: 值」，一行一个。这里写了的字段覆盖文件里的同名字段，"
                "没写的沿用文件。可用字段：name（名称）、description（一句话描述）、palette（色板，颜色列表）、"
                "positive（希望出现的风格关键词）、negative（不希望出现的）、rules（规则列表）、"
                "reference_images（参考图路径列表）。只有 name 和 description 必填，其余都可以不写；"
                "不想设置的字段整行删掉，或只留字段名不写值（如单独一行 palette:），都表示不设置。")})},
        }

    @_ga_errors
    def run(self, path: str = "", edit: str = "") -> tuple[StyleCard]:
        file = Path(resolve_repo_path(path.strip())) if path.strip() else None
        return (load_style_card(file, edit),)


class GAPromptExpand:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "GA_META")
    RETURN_NAMES = ("prompt", "negative", "meta")
    DESCRIPTION = ("提示词扩写：把一句大白话描述，结合风格卡，交给文字模型改写成出图模型能用好的专业提示词。"
                   "每运行一次调用一次文字模型（费用很低）。")
    OUTPUT_TOOLTIPS = ("扩写后的正向提示词：希望画面里出现的内容。", "反向提示词：不希望画面里出现的内容。",
                       _META_OUT)

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return _always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "brief": ("STRING", {"multiline": True, "default": "", "placeholder": "用一两句话描述想要的素材",
                                     "tooltip": (
                    "你想要什么素材，用大白话写一两句即可，例如「一枚国潮风铜钱图标」。"
                    "不用写专业提示词，这个节点会替你扩写。不要要求画文字，文字由模板合成。")}),
                "asset_type": (list(ASSET_TYPES), {"default": "icon", "tooltip": (
                    "素材的种类，决定构图方式：\n" + "\n".join(f"· {k}：{v}" for k, v in ASSET_TYPES.items()))}),
                "profile": _model_input("text", "扩写提示词"),
            },
            "optional": {
                "style": _STYLE_IN,
                "ref_description": ("STRING", {"default": "", "multiline": True,
                                               "placeholder": "参考图的文字描述（可留空）", "tooltip": (
                    "对参考图的文字描述，可留空。在「参考图 → 资源」流程里由「参考图分析」节点自动填入，"
                    "让扩写出的提示词贴近参考图。")}),
                "tag": _TAG,
            },
        }

    @_ga_errors
    def run(self, brief: str, asset_type: str, profile: str = "text", style: StyleCard | None = None,
            ref_description: str = "", tag: str = "") -> tuple[str, str, dict[str, Any]]:
        tag = _tag(tag)
        rec = _Recorder(_get_router())
        res = expand_prompt(rec, brief, asset_type=asset_type, style=style,
                            ref_description=ref_description, tag=tag, profile=profile)
        t = rec.target
        meta = {"brief": brief, "prompt": res.prompt, "negative": res.negative, "tag": tag,
                "text_model": f"{t.provider}:{t.model}" if t else None}
        return (res.prompt, res.negative, meta)


class GAReferenceAnalyze:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("description",)
    DESCRIPTION = "参考图分析：让能看图的模型把参考图的风格、配色等写成一段文字，供「提示词扩写」参考。"
    OUTPUT_TOOLTIPS = ("对参考图的文字描述，接到「提示词扩写」的 ref_description。",)

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return _always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "要分析的参考图，接「加载图像」节点。"}),
                "focus": ("STRING", {"default": "风格", "tooltip": (
                    "让模型重点看参考图的哪方面，随意填写，例如：风格、配色、构图、造型。")}),
                "profile": _model_input("vision", "看图分析"),
            },
            "optional": {"tag": _TAG},
        }

    @_ga_errors
    def run(self, images: torch.Tensor, focus: str = "风格", profile: str = "vision",
            tag: str = "") -> tuple[str]:
        text = analyze_reference(_get_router(), tensors_to_pils(images), focus=focus,
                                 tag=_tag(tag), profile=profile)
        return (text,)


class GACloudGenerate:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK", "GA_META")
    RETURN_NAMES = ("image", "mask", "meta")
    DESCRIPTION = ("云端生图：把提示词发给云端出图模型，拿回图片。这是整条流程里主要花钱的一步，"
                   "费用 = 单张价格 × 张数。")
    OUTPUT_TOOLTIPS = _IMAGE_OUT

    @classmethod
    def IS_CHANGED(cls, seed: int = -1, **kw: Any) -> float:
        return _changed_unless_seeded(cls, seed, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "prompt": _PROMPT_IN,
                "negative": _NEGATIVE_IN,
                "size": _size_input(),
                "n": _N,
                "transparent": _TRANSPARENT,
                "seed": ("INT", {"default": -1, "min": -1, "max": 2**53 - 1, "tooltip": (
                    "随机种子，决定「同样的提示词会不会出同样的图」。\n"
                    "· -1：每次随机，每点一次运行都会重新出图并计费。\n"
                    "· 固定数字（如 42）：只要其他参数没变，再点运行会直接用上次的结果，不重新出图、不计费。\n"
                    "注意：有的出图模型（如 gpt-image-1）不保证相同种子得到完全相同的画面。")}),
                "profile": _model_input("image", "出图"),
            },
            "optional": {
                "style": ("GA_STYLE", {"tooltip": (
                    "接「风格卡」。只有风格卡里带了参考图时才起作用：会把参考图一并发给模型，让画风更贴近。"
                    "文字上的风格要求已经由「提示词扩写」写进提示词了。")}),
                "meta": _META, "tag": _TAG,
                # 风格卡带参考图时改走 edit，用这个 profile
                "edit_profile": _model_input("image_edit", "风格卡带参考图时（这时走的是「按参考图生图」）出图"),
            },
        }

    @_ga_errors
    def run(self, prompt: str, negative: str, size: str = "1024x1024", n: int = 1, transparent: bool = False,
            seed: int = -1, profile: str = "image", style: StyleCard | None = None,
            meta: dict[str, Any] | None = None, tag: str = "",
            edit_profile: str = "image_edit") -> tuple[torch.Tensor, torch.Tensor, dict[str, Any]]:
        wh = _parse_size(size)
        tag = _tag(tag, meta)
        router = _get_router()
        refs = _load_refs(style) if style is not None and style.reference_images else []
        req = ImageRequest(prompt=prompt, negative=negative, size=f"{wh[0]}x{wh[1]}", n=n,
                           transparent=transparent, seed=None if seed < 0 else seed, refs=refs)
        if refs:
            result, target = router.edit(edit_profile, req, tag=tag)
        else:
            result, target = router.generate(profile, req, tag=tag)
        images = _unify(result.images, wh)
        image, mask = pil_to_tensors(images)
        out_meta = _image_meta(router, meta, result=result, target=target, prompt=prompt,
                               negative=negative, size=req.size, tag=tag)
        return (image, mask, out_meta)


class GACloudEdit:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK", "GA_META")
    RETURN_NAMES = ("image", "mask", "meta")
    DESCRIPTION = "云端改图：把参考图和提示词一起发给云端模型，按参考图生成新图，或修改原图。按张计费。"
    OUTPUT_TOOLTIPS = _IMAGE_OUT

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return _always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "参考图或要修改的原图，接「加载图像」节点。"}),
                "prompt": _PROMPT_IN,
                "negative": _NEGATIVE_IN,
                "size": _size_input(),
                "n": _N,
                "transparent": _TRANSPARENT,
                "profile": _model_input("image_edit", "按参考图生图 / 改图"),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": (
                    "可选。指定只重画原图的哪一块：蒙版里涂白的区域会被重画，其余保持不变。不接则整张图都可以改。")}),
                "meta": _META, "tag": _TAG,
            },
        }

    @_ga_errors
    def run(self, images: torch.Tensor, prompt: str, negative: str = "", size: str = "1024x1024", n: int = 1,
            transparent: bool = False, profile: str = "image_edit", mask: torch.Tensor | None = None,
            meta: dict[str, Any] | None = None,
            tag: str = "") -> tuple[torch.Tensor, torch.Tensor, dict[str, Any]]:
        wh = _parse_size(size)
        tag = _tag(tag, meta)
        router = _get_router()
        refs = tensors_to_pils(images)
        req = ImageRequest(prompt=prompt, negative=negative, size=f"{wh[0]}x{wh[1]}", n=n,
                           transparent=transparent, refs=refs,
                           mask=_edit_mask(mask, refs[0].size) if mask is not None else None)
        result, target = router.edit(profile, req, tag=tag)
        image, out_mask = pil_to_tensors(_unify(result.images, wh))
        out_meta = _image_meta(router, meta, result=result, target=target, prompt=prompt,
                               negative=negative, size=req.size, tag=tag)
        return (image, out_mask, out_meta)


class GARemoveBackground:
    """可选 mask（= 上游 1 - alpha）：none 时原样透传透明度；rembg 在合成后的 RGBA 上运行。"""
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    DESCRIPTION = "去背景：在本机把图片背景抠掉变成透明。method 选 none 时什么都不做，原样通过。"
    OUTPUT_TOOLTIPS = ("处理后的图片。", "处理后的透明度蒙版，和 image 一起接到下游。")

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要处理的图片。"}),
                "method": (["rembg", "none"], {"tooltip": (
                    "去背景的方式：\n"
                    "· none：不处理，原样通过。模型已经直接出了透明底（云端生图的 transparent 打开）时用它。\n"
                    "· rembg：在本机用抠图模型把主体抠出来，背景变透明，不花钱。"
                    "需要先给 ComfyUI 装 rembg（见 README「ComfyUI 自带 Python」）。")}),
            },
            "optional": {"mask": ("MASK", {"tooltip": "上游的透明度蒙版。接上才能保留上游已有的透明背景。"})},
        }

    @_ga_errors
    def run(self, image: torch.Tensor, method: Literal["rembg", "none"] = "rembg",
            mask: torch.Tensor | None = None) -> tuple[torch.Tensor, torch.Tensor]:
        outs = [imaging.remove_background(im, method) for im in tensors_to_pils(image, mask)]
        return pil_to_tensors(outs)


class GAQCScore:
    """批量输入时只给第一张图打分。可选 mask 让视觉模型看到透明区域（棋盘格）。"""
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("FLOAT", "STRING")
    RETURN_NAMES = ("score", "report")
    DESCRIPTION = "质检打分：让能看图的模型给图片打分（0 到 10），并列出问题。批量流程用它自动筛掉不合格的图。"
    OUTPUT_TOOLTIPS = ("得分，0 到 10。", "文字报告：得分、是否通过、发现的问题。")

    @classmethod
    def IS_CHANGED(cls, **kw: Any) -> float:
        return _always_changed(cls, **kw)

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要打分的图片。一次给了多张时只给第一张打分。"}),
                "brief": ("STRING", {"multiline": True, "default": "", "tooltip": (
                    "这张图原本想要什么（和「提示词扩写」里写的描述一致），模型据此判断画得对不对。")}),
                "threshold": ("FLOAT", {"default": 6.0, "min": 0.0, "max": 10.0, "step": 0.5, "tooltip": (
                    "及格线，0 到 10 分。得分不低于它算通过。调高更严格，会有更多图被判为不合格。")}),
                "profile": _model_input("vision", "看图质检"),
            },
            "optional": {
                "style": ("GA_STYLE", {"tooltip": "接「风格卡」，让模型同时检查是否符合风格。可不接。"}),
                "tag": _TAG,
                "mask": ("MASK", {"tooltip": "图片的透明度蒙版。接上后模型能看出哪里是透明的。可不接。"}),
            },
        }

    @_ga_errors
    def run(self, image: torch.Tensor, brief: str, threshold: float = 6.0, profile: str = "vision",
            style: StyleCard | None = None, tag: str = "",
            mask: torch.Tensor | None = None) -> tuple[float, str]:
        img = tensors_to_pils(image[:1], mask[:1] if mask is not None else None)[0]
        r = qc_score(_get_router(), img, brief=brief, style=style, threshold=threshold,
                     tag=_tag(tag), profile=profile)
        verdict = "通过" if r.passed else "未通过"
        lines = [f"得分 {r.score:.1f}/10（{verdict}，阈值 {threshold:g}）"]
        lines += [f"- {i}" for i in r.issues]
        return (r.score, "\n".join(lines))


class GAComposeTemplate:
    CATEGORY = CATEGORY
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    DESCRIPTION = ("模板合成：按版式文件把底框、图案、文字拼成一张成品（如一张麻将牌）。"
                   "文字由程序用字体画上去，不经过 AI，所以不会写错字。在本机完成，不花钱。")
    OUTPUT_TOOLTIPS = ("合成好的成品图。", "成品的透明度蒙版，和 image 一起接到下游。")

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "layout_path": ("STRING", {"default": "", "tooltip": (
                    "版式文件（.yaml）的路径，规定成品多大、每一层（底框、图案、文字）放在哪。"
                    "以 @repo/ 开头表示本项目目录下的文件，例如 @repo/examples/layouts/demo_tile.yaml。")}),
                "vars_json": ("STRING", {"default": "{}", "multiline": True, "tooltip": (
                    "填进版式里的文字，写成 {\"变量名\": \"内容\"}。例如版式里的文字层写的是 {name}，"
                    "这里写 {\"name\": \"萬\"}，成品上就印「萬」。没有变量时保持 {}。")}),
            },
            "optional": {
                "art": ("IMAGE", {"tooltip": "要放进版式里的图案（对应版式中的 slot:art）。"}),
                "art_mask": ("MASK", {"tooltip": "图案的透明度蒙版。接上才能保留图案的透明背景。"}),
            },
        }

    @_ga_errors
    def run(self, layout_path: str, vars_json: str = "{}", art: torch.Tensor | None = None,
            art_mask: torch.Tensor | None = None) -> tuple[torch.Tensor, torch.Tensor]:
        try:
            vars_ = json.loads(vars_json or "{}")
            if not isinstance(vars_, dict):
                raise ValueError("顶层必须是对象")
        except ValueError as e:
            raise InputError("bad_vars_json", f"vars_json 不是合法的 JSON 对象：{e}") from e
        path = Path(resolve_repo_path(layout_path)).expanduser()
        layout = load_layout(path)
        slots: dict[str, Image.Image] = {}
        if art is not None:
            slots["art"] = tensors_to_pils(art[:1], art_mask[:1] if art_mask is not None else None)[0]
        out = compose(layout, slots, vars_, path.resolve().parent)
        return pil_to_tensors([out])


_BAD_DRIVE = re.compile(r"^[A-Za-z]:")


def _safe_subfolder(sub: str) -> str:
    sub = (sub or "").strip().replace("\\", "/")
    if sub.startswith("/") or _BAD_DRIVE.match(sub) or ".." in sub.split("/"):
        raise InputError("bad_subfolder", f"子目录不合法：{sub!r}（不能是绝对路径，也不能含 ..）")
    return "/".join(p for p in sub.split("/") if p not in ("", "."))


def _safe_name(name: str) -> str:
    name = (name or "").strip() or "asset"
    if "/" in name or "\\" in name or ".." in name:
        raise InputError("bad_name", f"名称不合法：{name!r}（不能含路径分隔符或 ..）")
    return name


class GASaveAsset:
    CATEGORY = CATEGORY
    FUNCTION = "save"
    RETURN_TYPES = ()
    RETURN_NAMES = ()
    OUTPUT_NODE = True
    DESCRIPTION = ("保存资产：把图片存成 PNG，并在旁边存一个同名的 .json，记录这张图的提示词、模型、种子和估算费用。")

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要保存的图片。"}),
                "subfolder": ("STRING", {"default": "game_art", "tooltip": (
                    "存到 ComfyUI 输出目录下的哪个子文件夹，例如 ga/text_to_asset。文件夹不存在会自动创建。")}),
                "name": ("STRING", {"default": "asset", "tooltip": (
                    "文件名前缀。实际文件名会自动加编号，如 asset_00001.png，不会覆盖已有文件。")}),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": "透明度蒙版。接上才会存成透明背景；不接则存成不透明图片。"}),
                "meta": _META,
            },
        }

    @_ga_errors
    def save(self, image: torch.Tensor, subfolder: str = "", name: str = "asset",
             mask: torch.Tensor | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        import folder_paths  # ComfyUI 之外不存在，惰性导入

        sub, name = _safe_subfolder(subfolder), _safe_name(name)
        root = Path(folder_paths.get_output_directory()).resolve()
        target_dir = (root / sub).resolve()
        if root != target_dir and root not in target_dir.parents:
            raise InputError("bad_subfolder", f"子目录不合法：{subfolder!r}（越出了输出目录）")
        target_dir.mkdir(parents=True, exist_ok=True)

        # 无 mask 时也写 RGBA（alpha 全不透明），保持产物格式一致
        pils = [im.convert("RGBA") for im in tensors_to_pils(image, mask)]
        saved: list[dict[str, str]] = []
        counter = 1
        for im in pils:
            while (target_dir / f"{name}_{counter:05d}.png").exists() or \
                    (target_dir / f"{name}_{counter:05d}.json").exists():
                counter += 1
            fname = f"{name}_{counter:05d}.png"
            im.save(target_dir / fname, format="PNG")
            side = dict(meta or {})
            side.update(file=fname, saved_at=datetime.now(UTC).isoformat())
            (target_dir / f"{name}_{counter:05d}.json").write_text(
                json.dumps(side, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            saved.append({"filename": fname, "subfolder": sub, "type": "output"})
            counter += 1
        return {"ui": {"images": saved}}


NODE_CLASS_MAPPINGS: dict[str, type] = {c.__name__: c for c in (
    GAStyleCard, GAPromptExpand, GAReferenceAnalyze, GACloudGenerate, GACloudEdit,
    GARemoveBackground, GAQCScore, GAComposeTemplate, GASaveAsset)}

NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {
    "GAStyleCard": "GA Style Card", "GAPromptExpand": "GA Prompt Expand",
    "GAReferenceAnalyze": "GA Reference Analyze", "GACloudGenerate": "GA Cloud Generate",
    "GACloudEdit": "GA Cloud Edit", "GARemoveBackground": "GA Remove Background",
    "GAQCScore": "GA QC Score", "GAComposeTemplate": "GA Compose Template",
    "GASaveAsset": "GA Save Asset",
}
