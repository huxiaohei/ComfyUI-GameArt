import json
from pathlib import Path

import pytest

from game_art import assets
from game_art.errors import InputError

RECORD = {"brief": "箭楼", "qc": {"score": 6.0, "passed": False, "issues": ["a", "b", "c", "d", "e"], "details": [
    {"text": "支脚外撇", "level": "must", "kind": "layout"},
    {"text": "斜撑线太粗", "level": "must", "kind": "detail"},
    {"text": "旗杆多了顶饰", "level": "suggest", "kind": "detail"},
    {"text": "旗子偏橙", "level": "suggest", "kind": "detail"},
    {"text": "栏杆太密", "level": "must", "kind": "detail"},
]}}


def _asset(folder: Path, name: str, record: dict[str, object] | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    png = folder / f"{name}.png"
    png.write_bytes(b"png:" + name.encode())
    png.with_suffix(".json").write_text(json.dumps(record or {"brief": name}), encoding="utf-8")
    return png


def test_base_name() -> None:
    assert assets.base_name(Path("a/archer_lv1_00002.png")) == "archer_lv1"
    assert assets.base_name(Path("archer_lv1.png")) == "archer_lv1"
    assert assets.base_name(Path("x_1.png")) == "x_1"  # 不是五位编号，不动


def test_issues_numbered_in_record_order() -> None:
    issues = assets.issues_of(RECORD)
    assert [i["n"] for i in issues] == [1, 2, 3, 4, 5]
    assert issues[0] == {"n": 1, "text": "支脚外撇", "level": "must", "kind": "layout"}
    # 旧记录：只有文字、qc 是一条的列表
    assert assets.issues_of({"qc": [{"issues": ["糊"]}]}) == [{"n": 1, "text": "糊", "level": "must", "kind": "detail"}]
    assert assets.issues_of({}) == []


def test_pick_issues_rules() -> None:
    assert [i["n"] for i in assets.pick_issues(RECORD, [4, 2, 4], details_only=True)] == [4, 2]
    with pytest.raises(InputError, match=r"构图类.*ga regen") as e:
        assets.pick_issues(RECORD, [1, 2], details_only=True)
    assert e.value.code == "layout_issue"
    with pytest.raises(InputError, match="最多修 3 条"):
        assets.pick_issues(RECORD, [2, 3, 4, 5], details_only=True)
    with pytest.raises(InputError, match="没有第 9 条"):
        assets.pick_issues(RECORD, [9], details_only=False)
    with pytest.raises(InputError, match="ga qc"):
        assets.pick_issues({}, [1], details_only=False)
    assert len(assets.pick_issues(RECORD, [1, 2, 3, 4], details_only=False)) == 4  # 重新生成不限
    assert assets.pick_issues({}, [], details_only=True) == []


def test_finalize_copies_and_refuses_to_overwrite(tmp_path: Path) -> None:
    cand = _asset(tmp_path / "imgs", "archer_lv1_00002", {"brief": "箭楼", "file": "archer_lv1_00002.png"})
    dest, backup = assets.finalize(cand, "archer_lv1", force=False)
    assert dest == (tmp_path / "imgs" / "archer_lv1.png").resolve() and backup is None
    assert dest.read_bytes() == cand.read_bytes() and cand.exists()  # 候选图不删
    rec = json.loads(dest.with_suffix(".json").read_text(encoding="utf-8"))
    assert rec["file"] == "archer_lv1.png" and rec["copied_from"] == str(cand.resolve())

    other = _asset(tmp_path / "imgs", "archer_lv1_00003")
    with pytest.raises(InputError, match="--force") as e:
        assets.finalize(other, "archer_lv1", force=False)
    assert e.value.code == "final_exists" and dest.read_bytes() == cand.read_bytes()

    dest, backup = assets.finalize(other, "archer_lv1", force=True)
    assert dest.read_bytes() == other.read_bytes()
    assert backup is not None and backup.read_bytes() == cand.read_bytes()
    assert backup.parent.name == assets.FINAL_BACKUP and backup.with_suffix(".json").is_file()


def test_finalize_bad_input(tmp_path: Path) -> None:
    cand = _asset(tmp_path, "a_00001")
    with pytest.raises(InputError, match="定稿名不合法"):
        assets.finalize(cand, "../x", force=False)
    with pytest.raises(InputError, match="已经是定稿"):
        assets.finalize(cand, "a_00001", force=True)
    cand.with_suffix(".json").unlink()
    with pytest.raises(InputError, match=r"没有同名的 \.json"):
        assets.finalize(cand, "a", force=False)
    with pytest.raises(InputError, match="找不到这张图"):
        assets.checked_asset(tmp_path / "none.png")
