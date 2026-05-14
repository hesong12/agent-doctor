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
