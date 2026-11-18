"""批次目录布局：

<out_root>/<batch_id>/
  batch.json                      启动时冻结的清单（路径均为绝对路径）
  state.sqlite                    LangGraph checkpoint
  items/<item_id>/<attempt>/      该次生成下载的 *.png 与 sidecar *.json
  items/<item_id>/<attempt>.prompt 已提交到 ComfyUI 的 prompt_id（崩溃后续等，不重复提交）
  .lock                           批次锁（防止两个 ga batch 进程同时推进）
  sheets/selection.png            挑选用拼图
  final/<item_id>.png|_<k>.png    定稿图 + 同名 .json
  manifest.json                   最终清单
"""
from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from game_art.errors import InputError

from .manifest import Manifest


class BatchStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()

    @property
    def spec_path(self) -> Path:
        return self.root / "batch.json"

    @property
    def state_db(self) -> Path:
        return self.root / "state.sqlite"

    @property
    def sheet_path(self) -> Path:
        return self.root / "sheets" / "selection.png"

    @property
    def final_dir(self) -> Path:
        return self.root / "final"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def attempt_dir(self, item_id: str, attempt: int) -> Path:
        return self.root / "items" / item_id / str(attempt)

    def attempt_pngs(self, item_id: str, attempt: int) -> list[Path]:
        """已完整下载的候选图（下载先写到 .part 目录，完成后改名，所以存在即完整）。"""
        d = self.attempt_dir(item_id, attempt)
        return sorted(d.glob("*.png")) if d.is_dir() else []

    def partial_dir(self, item_id: str, attempt: int) -> Path:
        d = self.attempt_dir(item_id, attempt)
        return d.with_name(d.name + ".part")

    def commit_attempt(self, item_id: str, attempt: int) -> None:
        d = self.attempt_dir(item_id, attempt)
        if d.exists():  # 只可能是没有 png 的残留目录
            shutil.rmtree(d)
        self.partial_dir(item_id, attempt).rename(d)

    def archive_item(self, item_id: str) -> None:
        """重跑前把旧的各次生成挪到 archive/<item_id>/<n>/，避免被当作已下载结果复用。"""
        src = self.root / "items" / item_id
        if not src.exists():
            return
        base = self.root / "archive" / item_id
        n = 1
        while (base / str(n)).exists():
            n += 1
        base.mkdir(parents=True, exist_ok=True)
        src.rename(base / str(n))

    def prompt_file(self, item_id: str, attempt: int) -> Path:
        d = self.attempt_dir(item_id, attempt)
        return d.with_name(d.name + ".prompt")

    def save_prompt(self, item_id: str, attempt: int, prompt_id: str) -> None:
        """先写临时文件再改名：文件要么不存在，要么是完整的 prompt_id。"""
        p = self.prompt_file(item_id, attempt)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(prompt_id, encoding="utf-8")
        os.replace(tmp, p)

    def load_prompt(self, item_id: str, attempt: int) -> str | None:
        p = self.prompt_file(item_id, attempt)
        return (p.read_text(encoding="utf-8").strip() or None) if p.is_file() else None

    @contextmanager
    def lock(self) -> Iterator[None]:
        """非阻塞排他锁；已被其他进程持有时抛 InputError("batch_locked")。"""
        try:
            import fcntl
        except ImportError:  # 非 POSIX：不加锁
            yield
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise InputError("batch_locked", f"批次正被另一个 ga batch 进程使用：{self.root}；请等它结束后再试",
                                 {"batch_dir": str(self.root)}) from e
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def final_png(self, item_id: str, k: int, total: int) -> Path:
        return self.final_dir / (f"{item_id}.png" if total == 1 else f"{item_id}_{k}.png")

    def clear_final(self, item_id: str) -> None:
        pat = re.compile(rf"^{re.escape(item_id)}(_\d+)?\.(png|json)$")
        if self.final_dir.is_dir():
            for p in self.final_dir.iterdir():
                if pat.match(p.name):
                    p.unlink()

    def reset_partial(self, item_id: str, attempt: int) -> Path:
        d = self.partial_dir(item_id, attempt)
        if d.exists():
            shutil.rmtree(d)
        return d

    def save_spec(self, m: Manifest) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.spec_path.write_text(m.model_dump_json(indent=2), encoding="utf-8")

    def load_spec(self) -> Manifest:
        return Manifest.model_validate_json(self.spec_path.read_text(encoding="utf-8"))
