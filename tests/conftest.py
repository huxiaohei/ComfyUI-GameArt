import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]

# 依赖 torch 的测试（ComfyUI 节点相关）在没有 torch 的环境里整体跳过收集
collect_ignore: list[str] = []
if importlib.util.find_spec("torch") is None:
    collect_ignore += ["comfy_nodes", "test_workflows.py"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GAME_ART_CONFIG", raising=False)
    # 先 set 再 del，确保 load_settings 写入 os.environ 的 .env 值在测试后被还原
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "x")
    monkeypatch.delenv("COMFYUI_LLM_BASE_KEY")
    monkeypatch.setenv("COMFYUI_LLM_BASE_URL", "x")
    monkeypatch.delenv("COMFYUI_LLM_BASE_URL")


def write_cfg(tmp_path: Path, **overrides: Any) -> Path:
    cfg = {
        "providers": {"aihubmix": {"type": "openai_compatible", "base_url": "https://example.test/v1",
                                   "api_key_env": "COMFYUI_LLM_BASE_KEY"}},
        "profiles": {"image": ["aihubmix:img-model-a"], "text": ["aihubmix:txt-model"]},
        "pricing": {"aihubmix:img-model-a": {"per_image": 0.04}},
        "comfy_root": "~/ComfyUI",
    }
    cfg.update(overrides)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p
