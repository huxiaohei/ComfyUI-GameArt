"""描述清单：一份 yaml 里列出一批要出的素材，每项一个 id 和一段描述。

「提示词扩写」节点读它，运行时从里面选一项。格式：

    asset_type: character        # 可选：整份清单默认的素材种类
    prefix: 只参考画风……          # 可选：每条描述前面都加的公共文字
    items:
      - id: guanyu
        brief: 关羽，绿袍金甲……
        asset_type: character    # 可选：这一项单独指定

只认 items / prefix / asset_type，其余字段忽略，所以批量清单（ga batch 用的 manifest）也能直接读。
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import InputError


class BriefItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1)
    brief: str = Field(min_length=1)
    asset_type: str | None = None


class BriefList(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prefix: str = ""
    asset_type: str | None = None
    items: list[BriefItem] = Field(min_length=1)

    def full_brief(self, item: BriefItem) -> str:
        """这一项最终交给扩写的描述：公共文字在前，自己的描述在后。"""
        prefix = self.prefix.strip()
        return f"{prefix}{item.brief.strip()}" if prefix else item.brief.strip()

    def find(self, item_id: str) -> BriefItem:
        for item in self.items:
            if item.id == item_id:
                return item
        known = "、".join(i.id for i in self.items)
        raise InputError("unknown_brief_item", f"描述清单里没有 {item_id!r} 这一项（现有：{known}）")


def parse_briefs(text: str, where: str = "拖入的内容") -> BriefList:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise InputError("bad_briefs", f"描述清单不是合法的 YAML: {where}（{e}）") from e
    if not isinstance(data, dict) or "items" not in data:
        raise InputError("bad_briefs", (
            f"这不是一份描述清单: {where}（需要有 items，每项写 id 和 brief；"
            "风格卡请拖到「风格卡」节点上）"))
    try:
        briefs = BriefList.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise InputError("bad_briefs", f"描述清单格式不对: {where}（{loc}: {first['msg']}）") from e
    ids = [i.id for i in briefs.items]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise InputError("bad_briefs", f"描述清单里有重复的 id: {where}（{'、'.join(dup)}）")
    return briefs


def load_briefs(path: Path) -> BriefList:
    path = Path(path).expanduser()
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise InputError("bad_briefs", f"描述清单必须是 .yaml 文件：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise InputError("bad_briefs", f"读取描述清单失败: {path}（{e}）") from e
    return parse_briefs(text, str(path))
