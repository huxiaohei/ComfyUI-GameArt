"""调用账本与预算：每次上游调用追加一行 JSON。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .config import Pricing, Settings
from .errors import BudgetExceeded


class LedgerEntry(BaseModel):
    ts: datetime
    tag: str
    provider: str
    model: str
    kind: Literal["chat", "generate", "edit"]
    images: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float
    ok: bool
    error: str | None = None


class Ledger:
    def __init__(self, path: Path, pricing: dict[str, Pricing]) -> None:
        self.path = Path(path)
        self.pricing = pricing

    @classmethod
    def from_settings(cls, s: Settings) -> Ledger:
        return cls(s.data_dir / "ledger.jsonl", s.pricing)

    def _cost(self, provider: str, model: str, images: int, input_tokens: int, output_tokens: int) -> float:
        p = self.pricing.get(f"{provider}:{model}")
        if p is None:
            return 0.0
        return float(
            images * p.per_image
            + input_tokens / 1000 * p.per_1k_input_tokens
            + output_tokens / 1000 * p.per_1k_output_tokens
        )

    def record(
        self,
        *,
        tag: str,
        provider: str,
        model: str,
        kind: Literal["chat", "generate", "edit"],
        images: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        ok: bool,
        error: str | None = None,
    ) -> LedgerEntry:
        cost = self._cost(provider, model, images, input_tokens, output_tokens) if ok else 0.0
        entry = LedgerEntry(
            ts=datetime.now(UTC), tag=tag, provider=provider, model=model,
            kind=kind, images=images, input_tokens=input_tokens,
            output_tokens=output_tokens, cost=cost, ok=ok, error=error,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        return entry

    def spent(self, tag_prefix: str) -> float:
        if not self.path.is_file():
            return 0.0
        total = 0.0
        with self.path.open(encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(d.get("tag", "")).startswith(tag_prefix):
                    total += float(d.get("cost", 0) or 0)
        return total

    def check_budget(self, tag_prefix: str, budget: float | None) -> None:
        if budget is None:
            return
        spent = self.spent(tag_prefix)
        if spent > budget:
            raise BudgetExceeded(
                "budget_exceeded",
                f"已花费 {spent:.4f}，超出预算 {budget:.4f}",
                {"spent": spent, "budget": budget},
            )
