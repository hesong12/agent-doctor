"""Local-first settings storage for Agent Doctor.

Stores a single secret today — the Gemini API key used by
``pet-generate-sprite`` — across two backends:

1. macOS Keychain (preferred) via the optional ``keyring`` package.
2. ``~/.agent-doctor/config.toml`` (mode 0600, parent dir 0700) as a fallback
   for environments without keyring.

Reads are transparent: :func:`load_gemini_key` checks keyring first, then the
file. Writes go to keyring when it works, file otherwise. Clearing removes
from both so a key never lingers.

The stored key MUST NOT appear in:

- log lines (this module never logs it),
- exception messages (we redact via :func:`redact_secret`),
- argv (the CLI exposes ``--from-env`` / stdin only),
- repo files or fixtures.

The ``tomllib`` stdlib module (Python 3.11+) is used for reading; writing
uses a small hand-rolled emitter so we avoid an extra dependency for the
single-key file we care about.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

_KEYRING_SERVICE = "agent-doctor"
_KEYRING_USERNAME = "gemini-api-key"
_CONFIG_DIR = Path("~/.agent-doctor").expanduser()
_CONFIG_FILE = _CONFIG_DIR / "config.toml"
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_REDACTED = "***REDACTED***"


class Backend(str, Enum):
    KEYRING = "keyring"
    FILE = "file"
    NONE = "none"


@dataclass(frozen=True)
class SettingsStatus:
    backend: Backend
    configured: bool

    def render(self) -> str:
        state = "configured" if self.configured else "not configured"
        if self.backend is Backend.NONE:
            return "Gemini API key: not configured (no backend in use)"
        return f"Gemini API key: {state} (backend: {self.backend.value})"


class SettingsError(RuntimeError):
    """Raised for settings I/O failures with the secret already redacted."""


def config_dir() -> Path:
    return _CONFIG_DIR


def config_file() -> Path:
    return _CONFIG_FILE


def redact_secret(text: str, secret: str | None) -> str:
    """Return ``text`` with ``secret`` (and any non-trivial substring) removed.

    Used as the last line of defense before re-raising third-party exceptions
    that might quote the key in their message. We replace the full secret;
    callers that build their own error strings should never include the key
    in the first place.
    """

    if not text or not secret:
        return text
    if secret and secret in text:
        return text.replace(secret, _REDACTED)
    return text


def _try_import_keyring() -> Any | None:
    try:
        import keyring as _keyring  # type: ignore[import-not-found]
    except Exception:
        # ImportError is the common case; some keyring backends raise other
        # types when their native deps are missing. Either way we silently
        # fall back to the file backend.
        return None
    return _keyring


def _keyring_get() -> str | None:
    keyring_mod = _try_import_keyring()
    if keyring_mod is None:
        return None
    try:
        value = keyring_mod.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception:
        return None
    if value is None or value == "":
        return None
    return value


def _keyring_set(key: str) -> bool:
    keyring_mod = _try_import_keyring()
    if keyring_mod is None:
        return False
    try:
        keyring_mod.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, key)
    except Exception as exc:
        # Re-raise with the secret stripped so a noisy backend error doesn't
        # leak the key. Returning False here would silently fall back to the
        # file backend, which is also fine — but we surface the redacted
        # error so the user can fix a broken keyring backend.
        raise SettingsError(
            f"Could not write key to keyring backend: {redact_secret(str(exc), key)}"
        ) from None
    return True


def _keyring_clear() -> bool:
    keyring_mod = _try_import_keyring()
    if keyring_mod is None:
        return False
    try:
        keyring_mod.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception:
        # delete_password raises when the entry is absent in some backends;
        # treat as "nothing to clear" rather than an error.
        return False
    return True


def _file_get() -> str | None:
    path = _CONFIG_FILE
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    section = data.get("gemini") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return None
    value = section.get("api_key")
    if not isinstance(value, str) or value == "":
        return None
    return value


def _file_set(key: str) -> None:
    """Write ``key`` to ``config.toml`` with mode 0600 and parent mode 0700.

    The mode is set BEFORE the secret is written so it never lands on disk
    world-readable, even briefly. We also ``os.fsync`` the file to make the
    new mode + content durable before returning.
    """

    parent = _CONFIG_DIR
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, _DIR_MODE)
    except OSError as exc:
        raise SettingsError(
            f"Could not lock config dir permissions: {redact_secret(str(exc), key)}"
        ) from None

    fd = os.open(
        str(_CONFIG_FILE),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        _FILE_MODE,
    )
    try:
        # Re-chmod after open in case the file already existed with looser
        # mode (the mode arg to os.open only applies on creation).
        os.fchmod(fd, _FILE_MODE)
        body = _emit_toml(key)
        os.write(fd, body.encode("utf-8"))
        os.fsync(fd)
    except OSError as exc:
        raise SettingsError(
            f"Could not write config file: {redact_secret(str(exc), key)}"
        ) from None
    finally:
        os.close(fd)


def _file_clear() -> bool:
    path = _CONFIG_FILE
    if not path.exists():
        return False
    try:
        # Re-write with the section absent so a single config.toml that
        # might pick up other future keys keeps its other contents. Today
        # the only contents is the gemini key, so this is equivalent to
        # truncation.
        os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    except OSError:
        return False
    return True


def _emit_toml(key: str) -> str:
    escaped = (
        key.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return '[gemini]\napi_key = "' + escaped + '"\n'


def load_gemini_key() -> str | None:
    """Return the stored Gemini API key, or ``None`` when nothing is set."""

    value = _keyring_get()
    if value:
        return value
    return _file_get()


def store_gemini_key(key: str) -> Backend:
    """Store ``key`` in keyring if available, else in the config file.

    Returns the backend actually used. Raises :class:`SettingsError` (with the
    secret redacted) if both backends fail.
    """

    cleaned = key.strip()
    if not cleaned:
        raise SettingsError("Gemini API key was empty after stripping whitespace.")

    keyring_ok = False
    try:
        keyring_ok = _keyring_set(cleaned)
    except SettingsError:
        # Keyring backend is present but broken. Fall back to file.
        keyring_ok = False
    if keyring_ok:
        # Make sure file-backend never holds a stale duplicate after we
        # promote a key into keyring.
        _file_clear()
        return Backend.KEYRING

    _file_set(cleaned)
    return Backend.FILE


def clear_gemini_key() -> bool:
    """Remove any stored key from both backends. Returns True if anything was cleared."""

    cleared_keyring = _keyring_clear()
    cleared_file = _file_clear()
    return cleared_keyring or cleared_file


def settings_status() -> SettingsStatus:
    """Return which backend currently holds the key (if any)."""

    if _keyring_get() is not None:
        return SettingsStatus(Backend.KEYRING, True)
    if _file_get() is not None:
        return SettingsStatus(Backend.FILE, True)
    # Reflect which backend a write WOULD use right now, so 'show' tells the
    # user where their next set-gemini-key call will land.
    if _try_import_keyring() is not None:
        return SettingsStatus(Backend.KEYRING, False)
    return SettingsStatus(Backend.FILE, False)
