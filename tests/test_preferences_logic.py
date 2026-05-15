"""Headless tests for Preferences tab controllers (no tkinter)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_doctor import dictate_settings as ds
from agent_doctor.ui.preferences import dictation_tab as dt


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
