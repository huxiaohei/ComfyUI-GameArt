"""端到端冒烟：真实配置 + 运行中的 ComfyUI + 真实云端密钥，会产生费用（预期合计 < 2 美元）。

默认跳过。运行：  GA_E2E=1 uv run pytest -m e2e tests/e2e -s
前置：~/.game_art/config.yaml 与 .env 已就绪，已执行 ga comfy install 并重启 ComfyUI，ga config check 全部通过。
可选环境变量：GA_E2E_REF_IMAGE=<参考图路径>（缺省则用测试里生成的纯色图）。

通过 CLI 子进程驱动（--json），因此测的是用户真正会用的入口。
注意：tests/conftest.py 会把 HOME 指到临时目录做隔离，这里要把真实 HOME 还给子进程，
才能读到 ~/.game_art 下的真实配置、密钥与账本。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("GA_E2E") != "1", reason="设置 GA_E2E=1 才运行端到端冒烟（会花钱）"),
]

REPO = Path(__file__).resolve().parents[2]
REAL_HOME = os.path.expanduser("~")  # 模块导入时取值，早于 conftest 的 HOME 隔离夹具


def ga(*args: str, ok: tuple[int, ...] = (0,), timeout: float = 1800) -> dict[str, Any]:
    """运行 `ga <args> --json`，断言退出码，返回 stdout 里的单个 JSON 对象。"""
    env = {**os.environ, "HOME": REAL_HOME}  # GAME_ART_CONFIG 若已设置则沿用
    cp = subprocess.run([sys.executable, "-m", "game_art.cli.app", *args, "--json"], check=False, cwd=REPO, env=env,
                        capture_output=True, text=True, timeout=timeout)
    assert cp.returncode in ok, (
        f"ga {' '.join(args)} 退出码 {cp.returncode}\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}")
    result: dict[str, Any] = json.loads(cp.stdout)
    return result


def _assert_asset(files: list[str], transparent: bool = False) -> None:
    pngs = [Path(f) for f in files if f.lower().endswith(".png")]
    assert pngs, "没有产出 PNG"
    for p in pngs:
        assert p.is_file() and p.stat().st_size > 0
        with Image.open(p) as im:
            im.verify()
        if transparent:  # 透明底链路：至少有像素 alpha < 250，否则透明度在链路中丢了
            with Image.open(p) as im:
                alpha_min = cast(tuple[int, int], im.getchannel("A").getextrema())[0]
                assert im.mode == "RGBA" and alpha_min < 250, f"{p} 没有透明像素"
        side = p.with_suffix(".json")
        assert side.is_file(), f"缺少 sidecar：{side}"
        meta = json.loads(side.read_text(encoding="utf-8"))
        assert isinstance(meta, dict) and meta


@pytest.fixture(scope="module")
def ref_image(tmp_path_factory: pytest.TempPathFactory) -> Path:
    env = os.environ.get("GA_E2E_REF_IMAGE")
    if env:
        return Path(env).expanduser()
    p = tmp_path_factory.mktemp("ref") / "ref.png"
    Image.new("RGB", (512, 512), (200, 55, 45)).save(p)
    return p


@pytest.fixture(scope="module")
def made_asset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """链路 1：文案 → 透明底资源。后面的合成步骤复用它。"""
    out = tmp_path_factory.mktemp("t2a")
    res = ga("run", str(REPO / "workflows/text_to_asset.json"), "--set", "expand.brief=一枚国潮风铜钱图标",
             "--set", "gen.n=1", "--out", str(out))
    assert res["ok"] and res["files"]
    _assert_asset(res["files"], transparent=True)
    return Path(res["files"][0])


def test_config_check() -> None:
    assert ga("config", "check")["ok"] is True


def test_text_to_asset(made_asset: Path) -> None:
    assert made_asset.is_file()


def test_ref_to_asset(tmp_path: Path, ref_image: Path) -> None:
    res = ga("run", str(REPO / "workflows/ref_to_asset.json"), "--set", f"ref.image=@upload{{{ref_image}}}",
             "--set", "expand.brief=同风格的灯笼", "--out", str(tmp_path))
    assert res["ok"]
    _assert_asset(res["files"])


def test_batch_smoke(tmp_path: Path) -> None:
    st = ga("batch", "start", str(REPO / "examples/manifests/smoke.yaml"), "--out", str(tmp_path))
    batch_dir = Path(st["batch_dir"])
    assert st["stage"] == "awaiting_selection", st
    assert Path(st["sheet_path"]).is_file()
    ids = [i["id"] for i in st["items"]]
    assert ids == ["coin", "cloud", "lantern"]
    # 每项挑第一个可选编号（status 里 numbers 是全局编号）
    first: dict[str, int] = {}
    for n in st["numbers"]:
        first.setdefault(n["item_id"], n["number"])
    assert set(first) == set(ids), f"有项没有可选候选：{st['items']}"
    args: list[str] = []
    for iid, num in first.items():
        args += ["--item", f"{iid}={num}"]
    done = ga("batch", "select", str(batch_dir), *args)
    assert done["stage"] == "finished"
    finals = sorted((batch_dir / "final").glob("*.png"))
    assert len(finals) == 3
    for p in finals:
        assert p.with_suffix(".json").is_file()
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in manifest["items"]] == ids
    assert manifest["cost"] < 2.0


def test_compose_template(tmp_path: Path, made_asset: Path) -> None:
    # vars_json 是字符串输入：--set 按原文传入，直接写 JSON 对象文本即可
    vars_json = json.dumps({"name": "萬"}, ensure_ascii=False)
    res = ga("run", str(REPO / "workflows/compose_template.json"), "--set", f"art.image=@upload{{{made_asset}}}",
             "--set", f"compose.vars_json={vars_json}", "--out", str(tmp_path))
    assert res["ok"]
    _assert_asset(res["files"])
