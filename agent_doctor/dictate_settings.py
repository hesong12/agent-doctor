"""Local-first settings storage for the dictate pipeline.

Stores user preferences for:
- Whisper model selection
- LLM provider + URL + model
- Global hotkey binding
- Auto-paste configuration
- Pet animation toggles

File location: ``~/.agent-doctor/dictate.json``. Schema-versioned, atomic
writes, mode 0600. Secrets (LLM API keys) live in the system keychain via
``agent_doctor.settings``; this file stores only references.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

CONFIG_DIR = Path("~/.agent-doctor").expanduser()
CONFIG_FILE = CONFIG_DIR / "dictate.json"
SCHEMA_VERSION = 1
_FILE_MODE = 0o600
_DIR_MODE = 0o700


class DictateSettingsError(RuntimeError):
    """Raised on invalid / unparseable settings files."""


@dataclass(frozen=True)
class TranscriptionSettings:
    model_id: Optional[str] = None
    model_path: Optional[str] = None
    language: str = "auto"
    extra_buffer_ms: int = 150


@dataclass(frozen=True)
class LLMSettings:
    provider_id: str = "lm_studio"
    base_url: str = "http://localhost:1234/v1"
    model: Optional[str] = None
    api_key_ref: Optional[str] = None
    timeout_s: int = 30
    optimize_prompt: Optional[str] = None


@dataclass(frozen=True)
class HotkeySettings:
    binding: str = "ctrl+option+space"
    push_to_talk: bool = True
    daemon_enabled: bool = False


@dataclass(frozen=True)
class PasteSettings:
    auto_paste: bool = False
    paste_delay_ms: int = 60
    last_permission_check: Optional[str] = None


@dataclass(frozen=True)
class PetSettings:
    animate_listening: bool = True
    animate_thinking: bool = True


@dataclass(frozen=True)
class DictateSettings:
    version: int = SCHEMA_VERSION
    transcription: TranscriptionSettings = field(default_factory=TranscriptionSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    hotkey: HotkeySettings = field(default_factory=HotkeySettings)
    paste: PasteSettings = field(default_factory=PasteSettings)
    pet: PetSettings = field(default_factory=PetSettings)


def default_settings() -> DictateSettings:
    """Return a fresh DictateSettings populated with defaults."""

    return DictateSettings()
