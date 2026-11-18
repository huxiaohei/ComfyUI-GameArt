from pathlib import Path

import pytest

from game_art.config import Pricing
from game_art.errors import BudgetExceeded
from game_art.ledger import Ledger


def test_cost_from_pricing(tmp_path: Path) -> None:
    lg = Ledger(tmp_path / "l.jsonl", {"p:m": Pricing(per_image=0.04)})
    e = lg.record(tag="b1/i1/0", provider="p", model="m", kind="generate", images=3, ok=True)
    assert e.cost == pytest.approx(0.12)


def test_token_cost(tmp_path: Path) -> None:
    lg = Ledger(tmp_path / "l.jsonl", {"p:m": Pricing(per_1k_input_tokens=1, per_1k_output_tokens=2)})
    e = lg.record(tag="t", provider="p", model="m", kind="chat", input_tokens=500, output_tokens=1000, ok=True)
    assert e.cost == pytest.approx(2.5)


def test_spent_sums_by_prefix_and_survives_reload(tmp_path: Path) -> None:
    lg = Ledger(tmp_path / "l.jsonl", {"p:m": Pricing(per_image=1)})
    lg.record(tag="b1/i1/0", provider="p", model="m", kind="generate", images=1, ok=True)
    lg.record(tag="b2/i1/0", provider="p", model="m", kind="generate", images=1, ok=True)
    assert Ledger(tmp_path / "l.jsonl", {}).spent("b1/") == 1


def test_failed_call_recorded_with_zero_cost(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "l.jsonl"
    lg = Ledger(p, {"p:m": Pricing(per_image=1)})
    e = lg.record(tag="b1/i1/0", provider="p", model="m", kind="generate", images=0, ok=False, error="boom")
    assert e.cost == 0 and e.ok is False and e.error == "boom" and e.images == 0
    e2 = lg.record(tag="b1/i1/1", provider="p", model="m", kind="generate", images=2, ok=False, error="x")
    assert e2.cost == 0
    assert len(p.read_text().splitlines()) == 2
    assert Ledger(p, {}).spent("b1/") == 0


def test_budget_exceeded(tmp_path: Path) -> None:
    lg = Ledger(tmp_path / "l.jsonl", {"p:m": Pricing(per_image=1)})
    lg.record(tag="b1/i1/0", provider="p", model="m", kind="generate", images=1, ok=True)
    lg.record(tag="b1/i2/0", provider="p", model="m", kind="generate", images=1, ok=True)
    with pytest.raises(BudgetExceeded) as ei:
        lg.check_budget("b1/", 1.5)
    assert ei.value.details["spent"] == 2 and ei.value.details["budget"] == 1.5
    lg.check_budget("b1/", None)
    lg.check_budget("b1/", 2)


def test_unknown_price_costs_zero(tmp_path: Path) -> None:
    lg = Ledger(tmp_path / "l.jsonl", {})
    e = lg.record(tag="t", provider="x", model="y", kind="edit", images=5, ok=True)
    assert e.cost == 0


def test_spent_missing_file_and_bad_lines(tmp_path: Path) -> None:
    p = tmp_path / "l.jsonl"
    assert Ledger(p, {}).spent("") == 0
    lg = Ledger(p, {"p:m": Pricing(per_image=1)})
    lg.record(tag="a", provider="p", model="m", kind="generate", images=1, ok=True)
    with p.open("a") as f:
        f.write("garbage\n\n")
    assert lg.spent("a") == 1


def test_from_settings(tmp_path: Path) -> None:
    from game_art.config import Settings
    s = Settings(comfy_root=tmp_path, data_dir=tmp_path / "d", config_dir=tmp_path,
                 pricing={"p:m": Pricing(per_image=1)})
    lg = Ledger.from_settings(s)
    assert lg.path == tmp_path / "d" / "ledger.jsonl"
    assert lg.record(tag="t", provider="p", model="m", kind="generate", images=1, ok=True).cost == 1
