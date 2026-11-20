"""风格卡：模型与加载。"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from .errors import InputError


class StyleCard(BaseModel):
    name: str
    description: str
    palette: list[str] = []
    positive: str = ""
    negative: str = ""
    rules: list[str] = []

    def to_prompt_text(self) -> str:
        """渲染成给模型看的风格说明。"""
        lines = [f"名称：{self.name}", f"描述：{self.description}"]
        if self.palette:
            lines.append("色板：" + "、".join(self.palette))
        if self.positive:
            lines.append("正向关键词：" + self.positive)
        if self.negative:
            lines.append("负向关键词：" + self.negative)
        if self.rules:
            lines.append("风格规则：" + "；".join(self.rules))
        return "\n".join(lines)


def _read_mapping(text: str, where: str) -> dict[str, object]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise InputError("bad_style_card", f"风格卡不是合法的 YAML: {where}（{e}）") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise InputError("bad_style_card", f"风格卡格式不对（应为键值映射）: {where}")
    return {str(k): v for k, v in data.items()}


def load_style_card(path: Path | None, overrides: str = "") -> StyleCard:
    """读取风格卡。overrides 是一段 YAML 文本，里面写了的字段覆盖文件里的同名字段；
    不给 path 时完全由 overrides 定义（此时至少要有 name 和 description）。"""
    data: dict[str, object] = {}
    where = "直接编辑的内容"
    if path is not None:
        path = Path(path).expanduser()
        where = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise InputError("bad_style_card", f"读取风格卡失败: {path}（{e}）") from e
        data = _read_mapping(text, where)
        if not data:
            raise InputError("bad_style_card", f"风格卡格式不对（应为键值映射）: {path}")
    extra = _read_mapping(overrides, "直接编辑的内容") if overrides.strip() else {}
    if path is None and not extra:
        raise InputError("bad_style_card", "风格卡为空：请填写文件路径，或在编辑框里直接写风格内容")
    data.update(extra)
    if extra:
        where = f"{where} + 直接编辑的内容" if path is not None else where
    # 只写了字段名、没写值（如单独一行 "palette:"）表示"这项不设置"，按空处理；
    # 写在编辑框里时同样会清掉文件里的同名字段
    data = {k: v for k, v in data.items() if v is not None}
    if "reference_images" in data:
        raise InputError("bad_style_card", (
            f"风格卡不再支持 reference_images: {where}\n"
            "参考图现在由工作流里的「参考图 · ref」节点提供：把这些图片路径填到该节点的 paths（一行一个），"
            "并从风格卡里删掉 reference_images 这一项。"))
    missing = [k for k in ("name", "description") if not str(data.get(k, "")).strip()]
    if missing:
        raise InputError("bad_style_card", f"风格卡缺少必填字段 {'、'.join(missing)}: {where}（其余字段都可以不写）")
    try:
        card = StyleCard.model_validate(data)
    except ValidationError as e:
        problems = "；".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        raise InputError(
            "bad_style_card",
            f"风格卡字段不合法: {where}\n{problems}\n"
            "提示：palette、rules 要写成列表（每项一行、以 \"- \" 开头），其余字段写成一行文字。",
        ) from e
    return card
