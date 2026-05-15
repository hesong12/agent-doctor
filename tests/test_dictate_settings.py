"""Tests for ~/.agent-doctor/dictate.json settings storage."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_doctor import dictate_settings as ds


def test_defaults_have_expected_shape() -> None:
    defaults = ds.default_settings()
    assert defaults.version == 1
    assert defaults.transcription.model_id is None
    assert defaults.transcription.model_path is None
    assert defaults.transcription.language == "auto"
    assert defaults.transcription.extra_buffer_ms == 150
    assert defaults.llm.provider_id == "lm_studio"
    assert defaults.llm.base_url == "http://localhost:1234/v1"
    assert defaults.llm.model is None
    assert defaults.llm.api_key_ref is None
    assert defaults.llm.timeout_s == 30
    assert defaults.llm.optimize_prompt is None
    assert defaults.hotkey.binding == "ctrl+option+space"
    assert defaults.hotkey.push_to_talk is True
    assert defaults.hotkey.daemon_enabled is False
    assert defaults.paste.auto_paste is False
    assert defaults.paste.paste_delay_ms == 60
    assert defaults.paste.last_permission_check is None
    assert defaults.pet.animate_listening is True
    assert defaults.pet.animate_thinking is True


def test_dataclasses_are_frozen() -> None:
    defaults = ds.default_settings()
    with pytest.raises(Exception):
        defaults.transcription.language = "en"  # type: ignore[misc]


def test_save_and_load_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "dictate.json"
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)

    settings = ds.default_settings()
    settings = ds.replace_section(
        settings,
        transcription=ds.TranscriptionSettings(
            model_id="ggml-small",
            model_path=str(tmp_path / "small.bin"),
            language="en",
            extra_buffer_ms=200,
        ),
    )
    ds.save(settings)

    loaded = ds.load()
    assert loaded.transcription.model_id == "ggml-small"
    assert loaded.transcription.model_path == str(tmp_path / "small.bin")
    assert loaded.transcription.language == "en"
    assert loaded.transcription.extra_buffer_ms == 200


def test_load_missing_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    loaded = ds.load()
    assert loaded == ds.default_settings()


def test_save_writes_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "dictate.json"
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    ds.save(ds.default_settings())
    mode = stat.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o600


def test_load_corrupt_json_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "dictate.json"
    cfg.write_text("{ not json")
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    with pytest.raises(ds.DictateSettingsError, match="parse"):
        ds.load()


def test_load_future_version_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "dictate.json"
    cfg.write_text(json.dumps({"version": 999}))
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    with pytest.raises(ds.DictateSettingsError, match="version"):
        ds.load()


def test_save_wraps_oserror_as_dictate_settings_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare OSError from the OS layer must surface as DictateSettingsError."""

    cfg = tmp_path / "dictate.json"
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(ds.os, "replace", boom)

    with pytest.raises(ds.DictateSettingsError, match="could not write"):
        ds.save(ds.default_settings())

    # And the temp-file cleanup path should have unlinked the tmp file —
    # no .dictate.json.* droppings left in the config dir.
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith(".dictate.json.")]
    assert leftover == [], f"orphan temp files left behind: {leftover}"


def test_load_non_numeric_extra_buffer_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-numeric int fields must raise DictateSettingsError, not bare ValueError."""

    cfg = tmp_path / "dictate.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "transcription": {"extra_buffer_ms": "not a number"},
            }
        )
    )
    monkeypatch.setattr(ds, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)

    with pytest.raises(ds.DictateSettingsError, match="non-numeric"):
        ds.load()
