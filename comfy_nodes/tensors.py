"""torch IMAGE / MASK 与 PIL 互转。

ComfyUI 约定：
- IMAGE：torch.float32，形状 [B, H, W, 3]，取值 0..1。
- MASK：torch.float32，形状 [B, H, W]，取值 0..1。

MASK 语义（本节点包所有输出/输入统一遵循，与 ComfyUI 的 LoadImage 一致）：
    mask = 1 - alpha
即 mask=1 表示"完全透明/被遮住"，mask=0 表示"完全不透明"。
不带 alpha 的图（RGB）转换后 mask 全为 0。
GA Save Asset 带 mask 时写出 RGBA，alpha = 1 - mask；
GA Cloud Edit 把 mask 转成"alpha = 1 - mask"的 PNG 发给上游（透明处 = 要重画的区域）。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt
import torch
from PIL import Image

from game_art.errors import InputError


def pil_to_tensors(images: list[Image.Image]) -> tuple[torch.Tensor, torch.Tensor]:
    """PIL 列表 → (IMAGE [B,H,W,3], MASK [B,H,W])，mask = 1 - alpha。"""
    if not images:
        raise InputError("empty_images", "没有可转换的图片")
    sizes = {im.size for im in images}
    if len(sizes) != 1:
        raise InputError("size_mismatch", f"同一批图片尺寸必须一致，实际有：{sorted(sizes)}")
    rgbs: list[npt.NDArray[np.float32]] = []
    masks: list[npt.NDArray[np.float32]] = []
    for im in images:
        arr = np.asarray(im.convert("RGBA"), dtype=np.float32) / 255.0
        rgbs.append(arr[..., :3])
        masks.append(1.0 - arr[..., 3])
    return torch.from_numpy(np.stack(rgbs)), torch.from_numpy(np.stack(masks))


def _to_u8(arr: npt.NDArray[np.floating[Any]]) -> npt.NDArray[np.uint8]:
    out: npt.NDArray[np.uint8] = np.rint(np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)
    return out


def tensors_to_pils(image: torch.Tensor, mask: torch.Tensor | None = None) -> list[Image.Image]:
    """IMAGE (+ 可选 MASK) → PIL 列表。无 mask 返回 RGB；有 mask 返回 RGBA（alpha = 1 - mask）。"""
    if image.ndim == 3:
        image = image.unsqueeze(0)
    if image.ndim != 4 or image.shape[-1] < 3:
        raise InputError("bad_image_tensor", f"IMAGE 形状应为 [B,H,W,3]，实际：{tuple(image.shape)}")
    imgs = image.detach().cpu().float().numpy()[..., :3]
    masks = None
    if mask is not None:
        if mask.ndim == 2:
            mask = mask.unsqueeze(0)
        if mask.ndim != 3:
            raise InputError("bad_mask_tensor", f"MASK 形状应为 [B,H,W]，实际：{tuple(mask.shape)}")
        masks = mask.detach().cpu().float().numpy()
    out: list[Image.Image] = []
    for i in range(imgs.shape[0]):
        pil = Image.fromarray(_to_u8(imgs[i]), "RGB")
        if masks is None:
            out.append(pil)
            continue
        m = masks[i] if i < masks.shape[0] else masks[0]  # 单张 mask 广播给整批
        alpha = Image.fromarray(_to_u8(1.0 - m), "L")
        if alpha.size != pil.size:
            alpha = alpha.resize(pil.size, Image.Resampling.BILINEAR)
        rgba = pil.convert("RGBA")
        rgba.putalpha(alpha)
        out.append(rgba)
    return out
