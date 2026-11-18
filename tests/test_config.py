from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from game_art.config import Target, load_settings
from game_art.errors import BudgetExceeded, ExecutionError, InputError, TimeoutError_, UpstreamError
from tests.conftest import REPO, write_cfg


def test_load_from_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = write_cfg(tmp_path)
    monkeypatch.setenv("GAME_ART_CONFIG", str(cfg))
    s = load_settings()
    assert s.profiles["image"][0].provider == "aihubmix"
    assert s.profiles["image"][0].model == "img-model-a"


def test_dotenv_fills_missing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMFYUI_LLM_BASE_KEY", raising=False)
    cfg = write_cfg(tmp_path)
    (tmp_path / ".env").write_text("# c\n\nCOMFYUI_LLM_BASE_KEY=k1\n")
    assert load_settings(cfg).api_key("aihubmix") == "k1"


def test_dotenv_does_not_override_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "real")
    cfg = write_cfg(tmp_path)
    (tmp_path / ".env").write_text("COMFYUI_LLM_BASE_KEY=k1\n")
    assert load_settings(cfg).api_key("aihubmix") == "real"


def test_dotenv_reload_overwrites_keys_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ComfyUI 常驻进程里改了 .env：来自 .env 的键要更新，真实 shell 环境变量仍优先。"""
    monkeypatch.delenv("COMFYUI_LLM_BASE_KEY", raising=False)
    monkeypatch.setenv("GA_SHELL_ONLY", "shell")
    monkeypatch.setenv("GA_DOTENV_GONE", "x")
    monkeypatch.delenv("GA_DOTENV_GONE")
    cfg = write_cfg(tmp_path)
    env = tmp_path / ".env"
    env.write_text("COMFYUI_LLM_BASE_KEY=k1\nGA_SHELL_ONLY=fromfile\nGA_DOTENV_GONE=1\n")
    assert load_settings(cfg).api_key("aihubmix") == "k1"
    env.write_text("COMFYUI_LLM_BASE_KEY=k2\nGA_SHELL_ONLY=fromfile2\n")
    assert load_settings(cfg).api_key("aihubmix") == "k2"
    import os
    assert os.environ["GA_SHELL_ONLY"] == "shell"
    assert "GA_DOTENV_GONE" not in os.environ  # 从 .env 删掉的键也撤销


def test_dotenv_reload_keeps_shell_value_set_later(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMFYUI_LLM_BASE_KEY", raising=False)
    cfg = write_cfg(tmp_path)
    (tmp_path / ".env").write_text("COMFYUI_LLM_BASE_KEY=k1\n")
    load_settings(cfg)
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "real")  # 之后被进程内其他代码/用户显式设置
    (tmp_path / ".env").write_text("COMFYUI_LLM_BASE_KEY=k2\n")
    assert load_settings(cfg).api_key("aihubmix") == "real"


def test_missing_key_message_names_var_and_env_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMFYUI_LLM_BASE_KEY", raising=False)
    with pytest.raises(InputError) as e:
        load_settings(write_cfg(tmp_path)).api_key("aihubmix")
    assert "COMFYUI_LLM_BASE_KEY" in e.value.message and str(tmp_path / ".env") in e.value.message
    assert e.value.code == "missing_api_key"


def test_tilde_paths_expanded(tmp_path: Path) -> None:
    s = load_settings(write_cfg(tmp_path, data_dir="~/x"))
    assert not str(s.data_dir).startswith("~")
    assert not str(s.comfy_root).startswith("~")


def test_example_config_is_valid() -> None:
    s = load_settings(REPO / "config/config.example.yaml")
    for p in ("text", "vision", "image", "image_edit"):
        assert s.profiles[p]
    # 出图目标固定 quality，与 per_image 估价对应
    for p in ("image", "image_edit"):
        for t in s.profiles[p]:
            assert t.params.get("quality") == "medium" and s.pricing[f"{t.provider}:{t.model}"].per_image > 0


def test_lookup_falls_back_to_user_then_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = load_settings()  # HOME 已隔离、无用户配置 -> 示例配置
    assert s.config_dir == REPO / "config"
    ud = tmp_path / "home" / ".game_art"
    ud.mkdir()
    write_cfg(ud)
    assert load_settings().config_dir == ud.resolve()


def test_target_from_string_and_errors() -> None:
    t = Target.model_validate("aihubmix:gpt-image-1")
    assert (t.provider, t.model, t.params) == ("aihubmix", "gpt-image-1", {})
    with pytest.raises(ValidationError):
        Target.model_validate("nocolon")


def test_error_exit_codes() -> None:
    classes = (InputError, UpstreamError, TimeoutError_, ExecutionError, BudgetExceeded)
    assert [c("x", "m").exit_code for c in classes] == [3, 4, 5, 6, 9]
    e = InputError("c", "m", {"a": 1})
    assert (e.code, e.message, e.details) == ("c", "m", {"a": 1})


def _cfg_with_base_env(tmp_path: Path, base_url: str | None = "https://fallback.test/v1") -> Path:
    prov: dict[str, Any] = {"type": "openai_compatible", "api_key_env": "COMFYUI_LLM_BASE_KEY",
            "base_url_env": "COMFYUI_LLM_BASE_URL"}
    if base_url is not None:
        prov["base_url"] = base_url
    return write_cfg(tmp_path, providers={"aihubmix": prov})


def test_base_url_from_dotenv(tmp_path: Path) -> None:
    cfg = _cfg_with_base_env(tmp_path)
    (tmp_path / ".env").write_text("COMFYUI_LLM_BASE_URL=https://proxy.test/v1\n")
    assert load_settings(cfg).base_url("aihubmix") == "https://proxy.test/v1"


def test_base_url_env_falls_back_to_config_value(tmp_path: Path) -> None:
    assert load_settings(_cfg_with_base_env(tmp_path)).base_url("aihubmix") == "https://fallback.test/v1"


def test_base_url_plain_config_still_works(tmp_path: Path) -> None:
    assert load_settings(write_cfg(tmp_path)).base_url("aihubmix") == "https://example.test/v1"


def test_missing_base_url_message_names_var_and_env_path(tmp_path: Path) -> None:
    with pytest.raises(InputError) as e:
        load_settings(_cfg_with_base_env(tmp_path, base_url=None)).base_url("aihubmix")
    assert e.value.code == "missing_base_url"
    assert "COMFYUI_LLM_BASE_URL" in e.value.message and str(tmp_path / ".env") in e.value.message


def test_provider_without_any_base_url_is_invalid(tmp_path: Path) -> None:
    cfg = write_cfg(tmp_path, providers={"aihubmix": {"type": "openai_compatible",
                                                      "api_key_env": "COMFYUI_LLM_BASE_KEY"}})
    with pytest.raises(InputError):
        load_settings(cfg)


def test_model_choices_lists_profiles_then_targets(tmp_path: Path) -> None:
    from game_art.config import model_choices

    p = tmp_path / "config.yaml"
    p.write_text(
        "providers:\n"
        "  cloud: {type: openai_compatible, base_url: 'http://c/v1', api_key_env: K}\n"
        "  local: {type: openai_compatible, base_url: 'http://l/v1', models: ['qwen2.5:14b']}\n"
        "profiles:\n"
        "  text: ['cloud:a', 'cloud:b']\n"
        "  image: [{provider: cloud, model: a}]\n"
        f"comfy_root: {tmp_path}\n", encoding="utf-8")
    s = load_settings(p)
    assert model_choices(s) == ["text", "image", "cloud:a", "cloud:b", "local:qwen2.5:14b"]
    # 本机服务不写 api_key_env：不需要密钥，也不报缺密钥
    assert s.api_key("local") == "not-needed"
    assert Target.model_validate("local:qwen2.5:14b").model == "qwen2.5:14b"
