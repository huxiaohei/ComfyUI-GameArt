"""各节点共用的辅助函数与输入/输出定义。

约定见 tensors.py（IMAGE/MASK 形状与 mask = 1 - alpha）。
所有 GAError 在节点边界转成带中文信息的 RuntimeError，ComfyUI 编辑器会直接显示。
"""
from __future__ import annotations

import functools
import re
import uuid
from collections.abc import Callable
from typing import Any, cast

import torch
from PIL import Image

from game_art import imaging
from game_art.config import Settings, Target, find_config_path, load_settings
from game_art.errors import GAError, InputError, UpstreamError
from game_art.models import all_model_choices
from game_art.providers.base import ChatResult, ImageResult, Message
from game_art.providers.router import Router

from .tensors import tensors_to_pils

CATEGORY = "GameArt"

# ---------------------------------------------------------------- Router / 错误
_STATE: dict[str, Any] = {"key": None, "router": None}


def get_router() -> Router:
    """模块级懒加载并缓存 Router（含 Ledger）；配置路径或 mtime 变化时重载。

    节点里一律写 `common.get_router()`（按模块属性取），测试才能用
    monkeypatch.setattr(common, "get_router", ...) 统一替换。
    """
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


def ga_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """GAError → RuntimeError（保留原异常为 __cause__）。"""
    @functools.wraps(fn)
    def wrapper(*a: P.args, **k: P.kwargs) -> R:
        try:
            return fn(*a, **k)
        except GAError as e:
            raise RuntimeError(f"{e.message}（{e.code}）") from e
    return wrapper


# ---------------------------------------------------------------- 运行期辅助
def resolve_tag(tag: str, meta: dict[str, Any] | None = None) -> str:
    tag = (tag or "").strip()
    if tag:
        return tag
    if meta and meta.get("tag"):
        return str(meta["tag"])
    return f"run/{uuid.uuid4().hex[:12]}"


def parse_size(size: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[xX×*]\s*(\d+)\s*", size or "")
    if not m or int(m[1]) <= 0 or int(m[2]) <= 0:
        raise InputError("bad_size", f"尺寸格式不对：{size!r}（应为 宽x高，例如 1024x1024）")
    return int(m[1]), int(m[2])


class Recorder:
    """包一层 router，记下最后一次 chat 命中的目标（steps 不返回 Target）。"""
    def __init__(self, router: Router) -> None:
        self._router = router
        self.target: Target | None = None

    def chat(self, profile: str, messages: list[Message], *, tag: str,
             json_mode: bool = False) -> tuple[ChatResult, Target]:
        res, target = self._router.chat(profile, messages, tag=tag, json_mode=json_mode)
        self.target = target
        return res, target


def image_meta(router: Router, meta: dict[str, Any] | None, *, result: ImageResult, target: Target,
               prompt: str, negative: str, size: str, tag: str, refs: int = 0) -> dict[str, Any]:
    """Generate/Edit 追加 provider/model/seed/partial/size/cost，以及这次随提示词发给模型的参考图张数 refs
    （0 = 纯文字出图；事后排查"参考图到底有没有起作用"时先看它）。

    cost = 命中目标在 ledger.pricing 里的 per_image * 实际返回张数（估算；
    不读回账本，也不含此前文本调用的 token 费）。
    """
    pricing = getattr(router.ledger, "pricing", {}).get(f"{target.provider}:{target.model}")
    cost: float = (pricing.per_image if pricing else 0.0) * len(result.images)
    out = dict(meta or {})
    out.update(prompt=prompt, negative=negative, tag=tag, provider=target.provider,
               model=target.model, seed=result.seed, partial=result.partial, size=size, cost=cost,
               refs=refs)
    return out


def unify(images: list[Image.Image], size: tuple[int, int]) -> list[Image.Image]:
    if not images:
        raise UpstreamError("no_images", "上游没有返回任何图片")
    if len({im.size for im in images}) > 1:
        return [imaging.fit_canvas(im, size) for im in images]
    return images


def collect_refs(images: torch.Tensor | None, refs: list[Image.Image] | None) -> list[Image.Image]:
    """把 IMAGE 口进来的图（排前面）和「参考图」节点给的列表合成一个列表；两边都可以不接。"""
    out: list[Image.Image] = tensors_to_pils(images) if images is not None else []
    return out + list(refs or [])


def edit_mask(mask: torch.Tensor, size: tuple[int, int]) -> Image.Image:
    """MASK（1 = 要重画）→ 上游 PNG：alpha = 1 - mask（透明处 = 重画区域）。"""
    return tensors_to_pils(mask.new_zeros((1, size[1], size[0], 3)), mask)[0]


# ---------------------------------------------------------------- IS_CHANGED
def always_changed(cls: type, /, **_kw: Any) -> float:
    """调用云端的节点：每次都重新执行（NaN != NaN，ComfyUI 不会复用缓存的旧结果）。"""
    return float("nan")


def changed_unless_seeded(cls: type, /, seed: int = -1, **_kw: Any) -> float:
    """seed < 0（随机）时每次重跑；固定 seed 时同输入可复用缓存。"""
    try:
        return float("nan") if int(seed) < 0 else seed
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------- 输入 / 输出定义
TAG_IN = ("STRING", {"default": "", "tooltip": (
    "记账标签，一般留空。命令行和批量运行时会自动填入，账本按它统计每次任务花了多少钱；"
    "留空时自动生成一个随机标签。")})
META_IN = ("GA_META", {"tooltip": (
    "上游节点传来的记录信息（原始描述、提示词、所用模型等）。接上以后，这些信息会一路带到"
    "「保存资产」，写进图片旁边的 .json 文件，方便以后追溯这张图是怎么来的。可以不接。")})
STYLE_IN = ("GA_STYLE", {"tooltip": "接「风格卡」节点的输出。不接则不套用任何统一风格。"})
REFS_IN = ("GA_REFS", {"tooltip": "接「参考图」节点的 refs 输出：一张或多张参考图，各自保留原始尺寸。"})
META_OUT = "这一步的记录信息（提示词、模型、种子、估算费用等），接到下游节点的 meta。"
N_IN = ("INT", {"default": 1, "min": 1, "max": 16, "tooltip": (
    "一次出几张图。同一段提示词会得到几张不同的结果，便于挑选。按张计费，张数越多越贵。")})
TRANSPARENT_IN = ("BOOLEAN", {"default": False, "tooltip": (
    "打开（true）：要求模型直接画成透明背景，图标、道具、牌面图案这类要叠在别的东西上的素材用它。"
    "关闭（false）：普通的带背景图片，背景、插画用它。\n"
    "注意：Gemini 图片模型不支持透明背景，这个开关对它们不起作用，需要透明底时用后面的「去背景」节点。")})
PROMPT_IN = ("STRING", {"forceInput": True, "tooltip": (
    "正向提示词：告诉模型要画什么。这是一个接线口，一般接「提示词扩写」的 prompt 输出；"
    "想自己写提示词，就接一个「字符串」节点。")})
NEGATIVE_IN = ("STRING", {"forceInput": True, "tooltip": (
    "反向提示词：告诉模型不要画什么（如模糊、水印、文字）。一般接「提示词扩写」的 negative 输出。")})
IMAGE_OUT_TOOLTIPS = ("生成的图片。", "透明度蒙版：记录图片哪些地方是透明的，和 image 一起接到下游，透明背景才不会丢。",
                      META_OUT)


def settings_or_none() -> Settings | None:
    try:
        return load_settings(find_config_path())
    except Exception:  # 配置有问题时节点仍要能加载；真正运行时会报出具体的配置错误
        return None


def size_input() -> tuple[list[str], dict[str, Any]]:
    """"尺寸"下拉：选项来自配置的 image_sizes。"""
    s = settings_or_none()
    choices = list(s.image_sizes) if s and s.image_sizes else ["1024x1024"]
    default = "1024x1024" if "1024x1024" in choices else choices[0]
    return (choices, {"default": default, "tooltip": (
        "图片尺寸，宽x高，单位像素。宽大于高是横图，高大于宽是竖图，相等是方图；尺寸越大越贵。\n"
        "默认的三个选项是 gpt-image-1 支持的全部尺寸：1024x1024（方）、1536x1024（横）、1024x1536（竖）。"
        "需要别的比例（如 16:9）时，选最接近的尺寸出图后再裁剪。\n"
        "Gemini 图片模型没有固定尺寸，只按宽高比出图：1536x1024 会换算成 3:2，实际得到约 1248x832 的图。\n"
        "换了出图模型、想增减选项：改 ~/.game_art/config.yaml 里的 image_sizes，然后刷新本页面。")})


def model_input(default: str, what: str) -> tuple[list[str], dict[str, Any]]:
    """"模型"下拉：配置文件里的（profile 名 + 用到的 provider:model）+ 各服务商 /models 列出的全部模型。"""
    s = settings_or_none()
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
