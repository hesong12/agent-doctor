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
    binding: str = "right_cmd"
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


def _to_dict(settings: DictateSettings) -> dict[str, Any]:
    return {
        "version": settings.version,
        "transcription": {
            "model_id": settings.transcription.model_id,
            "model_path": settings.transcription.model_path,
            "language": settings.transcription.language,
            "extra_buffer_ms": settings.transcription.extra_buffer_ms,
        },
        "llm": {
            "provider_id": settings.llm.provider_id,
            "base_url": settings.llm.base_url,
            "model": settings.llm.model,
            "api_key_ref": settings.llm.api_key_ref,
            "timeout_s": settings.llm.timeout_s,
            "optimize_prompt": settings.llm.optimize_prompt,
        },
        "hotkey": {
            "binding": settings.hotkey.binding,
            "push_to_talk": settings.hotkey.push_to_talk,
            "daemon_enabled": settings.hotkey.daemon_enabled,
        },
        "paste": {
            "auto_paste": settings.paste.auto_paste,
            "paste_delay_ms": settings.paste.paste_delay_ms,
            "last_permission_check": settings.paste.last_permission_check,
        },
        "pet": {
            "animate_listening": settings.pet.animate_listening,
            "animate_thinking": settings.pet.animate_thinking,
        },
    }


def _from_dict(payload: dict[str, Any]) -> DictateSettings:
    if not isinstance(payload, dict):
        raise DictateSettingsError("expected JSON object at top level")
    version = payload.get("version", SCHEMA_VERSION)
    if not isinstance(version, int):
        raise DictateSettingsError("'version' must be an integer")
    if version > SCHEMA_VERSION:
        raise DictateSettingsError(
            f"unsupported settings version {version} (this build supports {SCHEMA_VERSION})"
        )
    t = payload.get("transcription") or {}
    llm_d = payload.get("llm") or {}
    h = payload.get("hotkey") or {}
    p = payload.get("paste") or {}
    pet = payload.get("pet") or {}
    try:
        extra_buffer_ms = int(t.get("extra_buffer_ms", 150))
        timeout_s = int(llm_d.get("timeout_s", 30))
        paste_delay_ms = int(p.get("paste_delay_ms", 60))
    except (ValueError, TypeError) as exc:
        raise DictateSettingsError(
            f"non-numeric integer field in settings: {exc}"
        ) from exc
    return DictateSettings(
        version=version,
        transcription=TranscriptionSettings(
            model_id=t.get("model_id"),
            model_path=t.get("model_path"),
            language=t.get("language", "auto"),
            extra_buffer_ms=extra_buffer_ms,
        ),
        llm=LLMSettings(
            provider_id=llm_d.get("provider_id", "lm_studio"),
            base_url=llm_d.get("base_url", "http://localhost:1234/v1"),
            model=llm_d.get("model"),
            api_key_ref=llm_d.get("api_key_ref"),
            timeout_s=timeout_s,
            optimize_prompt=llm_d.get("optimize_prompt"),
        ),
        hotkey=HotkeySettings(
            binding=h.get("binding", "right_cmd"),
            push_to_talk=bool(h.get("push_to_talk", True)),
            daemon_enabled=bool(h.get("daemon_enabled", False)),
        ),
        paste=PasteSettings(
            auto_paste=bool(p.get("auto_paste", False)),
            paste_delay_ms=paste_delay_ms,
            last_permission_check=p.get("last_permission_check"),
        ),
        pet=PetSettings(
            animate_listening=bool(pet.get("animate_listening", True)),
            animate_thinking=bool(pet.get("animate_thinking", True)),
        ),
    )


def replace_section(settings: DictateSettings, **overrides: Any) -> DictateSettings:
    """Return a new DictateSettings with the given top-level sections replaced.

    Example: ``replace_section(s, llm=LLMSettings(...))``.
    """

    return replace(settings, **overrides)


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, _DIR_MODE)
    except OSError:
        # Best-effort: some filesystems (e.g. mounted tmpfs in tests) reject chmod.
        pass


def _atomic_write(dest: Path, body: bytes) -> None:
    """Write ``body`` to ``dest`` atomically with mode 0600.

    Mirrors the canonical pattern in ``agent_doctor.settings._atomic_write``:
    fchmod the temp fd to ``_FILE_MODE`` BEFORE writing the body so the bytes
    never live on disk under a looser mode, fsync, then atomically replace
    into place. On any failure the temp file is unlinked so we don't leave
    orphan ``.dictate.json.*`` droppings in the config dir.
    """

    _ensure_dir()
    fd, tmp_name = tempfile.mkstemp(prefix=".dictate.json.", dir=str(dest.parent))
    tmp_path = Path(tmp_name)
    try:
        try:
            os.fchmod(fd, _FILE_MODE)
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, dest)
    except Exception:
        # Atomic-replace never partially-applies on POSIX, but an fchmod /
        # write / fsync / replace error leaves the temp file on disk — clean
        # it up so we don't litter the config dir.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def save(settings: DictateSettings) -> Path:
    """Persist ``settings`` to ``CONFIG_FILE`` atomically. Returns the path.

    Every OS-level failure is wrapped in ``DictateSettingsError`` so the
    public contract is "every exception path raises DictateSettingsError".
    """

    body = json.dumps(_to_dict(settings), indent=2, sort_keys=True).encode("utf-8")
    try:
        _atomic_write(CONFIG_FILE, body)
    except OSError as exc:
        raise DictateSettingsError(
            f"could not write {CONFIG_FILE}: {exc}"
        ) from exc
    return CONFIG_FILE


def load() -> DictateSettings:
    """Load settings from ``CONFIG_FILE`` or return defaults if missing."""

    if not CONFIG_FILE.exists():
        return default_settings()
    try:
        payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DictateSettingsError(
            f"failed to parse {CONFIG_FILE}: {exc.msg} at line {exc.lineno}"
        ) from exc
    return _from_dict(payload)
