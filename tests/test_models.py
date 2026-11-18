import json
from pathlib import Path

import pytest
import requests
import responses

from game_art import models
from game_art.config import Settings, load_settings
from tests.conftest import write_cfg

URL = "https://example.test/v1/models"


@pytest.fixture(autouse=True)
def _clean_mem() -> None:
    models._MEM.clear()


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: object) -> Settings:
    monkeypatch.setenv("COMFYUI_LLM_BASE_KEY", "k")
    return load_settings(write_cfg(tmp_path, data_dir=str(tmp_path / "data"), **kw))


@responses.activate
def test_provider_models_fetches_sorts_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path, monkeypatch)
    responses.get(URL, json={"data": [{"id": "b"}, {"id": "a"}]})
    assert models.provider_models(s, "aihubmix") == ["a", "b"]
    assert models.provider_models(s, "aihubmix") == ["a", "b"]
    assert len(responses.calls) == 1  # 第二次走内存缓存
    models._MEM.clear()               # 模拟重启：走磁盘缓存，仍不请求
    assert models.provider_models(s, "aihubmix") == ["a", "b"]
    assert len(responses.calls) == 1
    saved = json.loads((tmp_path / "data" / models.CACHE_FILE).read_text(encoding="utf-8"))
    assert saved["aihubmix"]["models"] == ["a", "b"]
    assert "k" not in json.dumps(saved["aihubmix"]["models"]) and "example.test" not in json.dumps(saved)


@responses.activate
def test_provider_models_refetches_when_stale_and_keeps_old_on_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path, monkeypatch)
    cache = tmp_path / "data" / models.CACHE_FILE
    cache.parent.mkdir()
    cache.write_text(json.dumps({"aihubmix": {"fetched_at": 0, "models": ["old"]}}), encoding="utf-8")
    responses.get(URL, body=requests.ConnectionError("down"))
    assert models.provider_models(s, "aihubmix") == ["old"]   # 断网：退回旧列表
    assert models.provider_models(s, "aihubmix") == ["old"]
    assert len(responses.calls) == 1                          # 失败后一分钟内不重试
    responses.replace(responses.GET, URL, json={"data": [{"id": "new"}]})
    assert models.provider_models(s, "aihubmix", refresh=True) == ["new"]


@responses.activate
def test_provider_models_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path, monkeypatch)
    responses.get(URL, status=401)
    assert models.provider_models(s, "aihubmix") == []
    monkeypatch.delenv("COMFYUI_LLM_BASE_KEY")
    models._MEM.clear()
    assert models.provider_models(s, "aihubmix") == []        # 缺密钥：不请求、不报错
    assert len(responses.calls) == 1
    assert models.provider_models(s, "nope") == []
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / models.CACHE_FILE).write_text("not json", encoding="utf-8")
    assert models.provider_models(s, "nope") == []


@responses.activate
def test_all_model_choices_puts_config_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path, monkeypatch, providers={"aihubmix": {
        "type": "openai_compatible", "base_url": "https://example.test/v1",
        "api_key_env": "COMFYUI_LLM_BASE_KEY", "unlisted_models": ["hidden"]}})
    responses.get(URL, json={"data": [{"id": "zeta"}, {"id": "txt-model"}, {"id": "alpha"}]})
    assert models.all_model_choices(s) == [
        "image", "text", "aihubmix:img-model-a", "aihubmix:txt-model",
        "aihubmix:alpha", "aihubmix:hidden", "aihubmix:zeta"]
