"""Headless tests for Preferences tab controllers (no tkinter)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_doctor import dictate_settings as ds
from agent_doctor.ui.preferences import dictation_tab as dt
from agent_doctor.ui.preferences import llm_tab as lt


def test_dictation_state_initialises_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        transcription=ds.TranscriptionSettings(
            model_id="ggml-small",
            model_path=str(tmp_path / "ggml-small.bin"),
            language="en",
            extra_buffer_ms=222,
        ),
    )
    ds.save(settings)
    state = dt.DictationState.from_settings()
    assert state.model_id == "ggml-small"
    assert state.language == "en"
    assert state.extra_buffer_ms == 222


def test_dictation_state_apply_persists_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    state = dt.DictationState(
        model_id="ggml-medium",
        model_path=str(tmp_path / "ggml-medium.bin"),
        language="zh",
        extra_buffer_ms=100,
    )
    state.apply()
    loaded = ds.load()
    assert loaded.transcription.model_id == "ggml-medium"
    assert loaded.transcription.language == "zh"
    assert loaded.transcription.extra_buffer_ms == 100


def test_dictation_state_validates_buffer_range() -> None:
    with pytest.raises(dt.DictationStateError, match="buffer"):
        dt.DictationState(
            model_id=None,
            model_path=None,
            language="auto",
            extra_buffer_ms=-1,
        ).apply()


def test_install_options_lists_catalog_with_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_models as dm

    monkeypatch.setattr(dm, "DOWNLOAD_DIR", tmp_path / "models")
    options = dt.model_install_options()
    ids = {opt["id"] for opt in options}
    assert "ggml-large-v3-turbo" in ids
    for opt in options:
        assert "installed" in opt
        assert "display_name" in opt


def test_llm_state_from_and_to_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    state = lt.LLMState(
        provider_id="ollama",
        base_url="http://localhost:11434/v1",
        model="llama3.1:8b",
        api_key=None,
        timeout_s=20,
        optimize_prompt=None,
    )
    state.apply()
    loaded = ds.load()
    assert loaded.llm.provider_id == "ollama"
    assert loaded.llm.base_url == "http://localhost:11434/v1"
    assert loaded.llm.model == "llama3.1:8b"
    assert loaded.llm.timeout_s == 20


def test_llm_state_blocks_custom_base_url_on_non_custom_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    with pytest.raises(lt.LLMStateError, match="custom"):
        lt.LLMState(
            provider_id="lm_studio",
            base_url="http://elsewhere/v1",
            model=None,
            api_key=None,
            timeout_s=30,
            optimize_prompt=None,
        ).apply()


def test_llm_state_probe_returns_rows() -> None:
    """The tab uses ``probe_all`` so we just sanity-check the bridge."""

    rows = lt.probe_providers(timeout=0.5)
    ids = {r.provider_id for r in rows}
    assert ids == {"lm_studio", "ollama", "custom"}
