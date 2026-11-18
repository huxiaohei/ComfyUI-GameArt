from pathlib import Path
from typing import Any

import pytest
import yaml

from game_art.batch.manifest import load_manifest
from game_art.errors import InputError
from game_art.paths import REPO_ROOT


def _write(tmp_path: Path, **kw: Any) -> Path:
    data: dict[str, Any] = {"batch_id": "b1", "workflow": "wf.json", "items": [{"id": "a", "brief": "铜钱"}]}
    data.update(kw)
    p = tmp_path / "m" / "batch.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def test_manifest_duplicate_ids_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, items=[{"id": "a", "brief": "x"}, {"id": "a", "brief": "y"}])
    with pytest.raises(InputError) as ei:
        load_manifest(p)
    assert "a" in ei.value.message and "重复" in ei.value.message


def test_manifest_relative_paths_resolved(tmp_path: Path) -> None:
    p = _write(tmp_path, style_card="../cards/s.yaml")
    m = load_manifest(p)
    assert m.workflow == (tmp_path / "m" / "wf.json").resolve()
    assert m.style_card == (tmp_path / "cards" / "s.yaml").resolve()
    assert m.candidates == 2 and m.qc_threshold == 6.0 and m.max_retries is None and m.budget is None
    assert m.items[0].sets == {} and m.defaults == {}


def test_manifest_repo_prefix_and_absolute(tmp_path: Path) -> None:
    p = _write(tmp_path, workflow="@repo/workflows/text_to_asset.json", style_card=str(tmp_path / "abs.yaml"))
    m = load_manifest(p)
    assert m.workflow == REPO_ROOT / "workflows" / "text_to_asset.json"
    assert m.style_card == tmp_path / "abs.yaml"


@pytest.mark.parametrize("bad", [
    {"items": []},
    {"candidates": 0},
    {"batch_id": "a/b"},
    {"items": [{"id": "x/y", "brief": "z"}]},
    {"defaults": {"noDot": 1}},
    {"items": [{"id": "coin_2", "brief": "z"}]},
])
def test_manifest_invalid_fields_rejected(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(InputError):
        load_manifest(_write(tmp_path, **bad))


def test_manifest_missing_or_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        load_manifest(tmp_path / "nope.yaml")
    p = tmp_path / "bad.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(InputError):
        load_manifest(p)


def test_smoke_manifest_loads() -> None:
    m = load_manifest(REPO_ROOT / "examples" / "manifests" / "smoke.yaml")
    assert len(m.items) == 3 and m.candidates == 2 and m.budget == 2.0
    assert m.workflow == REPO_ROOT / "workflows" / "text_to_asset.json" and m.workflow.is_file()
    assert m.style_card == REPO_ROOT / "examples" / "style_cards" / "guochao.yaml" and m.style_card.is_file()


def test_manifest_id_with_numeric_suffix_explained(tmp_path: Path) -> None:
    with pytest.raises(InputError) as ei:
        load_manifest(_write(tmp_path, items=[{"id": "tile_12", "brief": "z"}]))
    assert "tile_12" in ei.value.message and "_<数字>" in ei.value.message
