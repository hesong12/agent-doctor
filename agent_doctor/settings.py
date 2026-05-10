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
import tempfile
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


def _atomic_write(dest: Path, body: bytes) -> None:
    """Write ``body`` to ``dest`` atomically with mode 0600.

    Creates a temp file in the same directory (so ``os.replace`` is atomic
    on POSIX), chmods it to ``_FILE_MODE`` BEFORE writing the secret so the
    bytes never live on disk under a looser mode, ``fsync``s, then swaps
    into place. Any failure unlinks the temp file rather than leaving a
    stray ``.config-*.toml.tmp`` next to the real config.

    This replaces the pre-review ``O_WRONLY | O_TRUNC`` open-and-truncate
    pattern, which would empty the destination on a crash mid-write and
    leak the file descriptor on the error path.
    """

    parent = dest.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-",
        suffix=".toml.tmp",
        dir=str(parent),
    )
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
        # Atomic-replace never partially-applies on POSIX, but a chmod /
        # write / fsync error before the replace leaves the temp file on
        # disk — clean it up so we don't litter the config dir.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _file_set(key: str) -> None:
    """Write ``key`` to ``config.toml`` atomically with mode 0600 (parent 0700).

    Preserves any non-``[gemini]`` top-level tables that already live in the
    file so a future co-tenant setting doesn't get nuked when we re-write.
    Today only ``[gemini]`` is ever written, but parsing-then-rewriting is
    almost the same cost and removes a foot-gun for whoever adds the next
    section.
    """

    parent = _CONFIG_DIR
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, _DIR_MODE)
    except OSError as exc:
        raise SettingsError(
            f"Could not lock config dir permissions: {redact_secret(str(exc), key)}"
        ) from None

    existing_tables: dict[str, dict[str, Any]] = {}
    if _CONFIG_FILE.exists():
        try:
            parsed = tomllib.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            for name, body in parsed.items():
                if name == "gemini" or not isinstance(body, dict):
                    continue
                existing_tables[name] = {
                    k: v for k, v in body.items() if isinstance(v, str)
                }

    tables: dict[str, dict[str, str]] = dict(existing_tables)
    tables["gemini"] = {"api_key": key}

    body = _emit_toml_tables(tables).encode("utf-8")
    try:
        _atomic_write(_CONFIG_FILE, body)
    except OSError as exc:
        raise SettingsError(
            f"Could not write config file: {redact_secret(str(exc), key)}"
        ) from None


def _file_clear() -> bool:
    """Remove the ``[gemini]`` section from the config file, atomically.

    Behaviour by file shape:
    - File missing → ``False`` (nothing to clear).
    - Parse fails (corrupt TOML) → ``unlink`` the file. We'd rather lose a
      corrupt config we can't read than leave a key on disk that nothing can
      load but ``cat`` can still print.
    - File contains only ``[gemini]`` → ``unlink``.
    - File contains other tables → atomically rewrite WITHOUT ``[gemini]``,
      preserving the rest. Uses ``_atomic_write`` so a crash mid-rewrite
      can never leave the file empty.

    The pre-review implementation opened the file ``O_WRONLY | O_TRUNC`` and
    leaked the returned file descriptor; both bugs are fixed here.
    """

    path = _CONFIG_FILE
    if not path.exists():
        return False

    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        try:
            path.unlink()
        except OSError:
            return False
        return True

    if not isinstance(parsed, dict):
        return False

    gemini_section = parsed.get("gemini")
    has_gemini_key = (
        isinstance(gemini_section, dict)
        and isinstance(gemini_section.get("api_key"), str)
        and gemini_section.get("api_key") != ""
    )
    if not has_gemini_key:
        return False

    remaining_tables: dict[str, dict[str, str]] = {}
    for name, body in parsed.items():
        if name == "gemini" or not isinstance(body, dict):
            continue
        remaining_tables[name] = {
            k: v for k, v in body.items() if isinstance(v, str)
        }

    if not remaining_tables:
        try:
            path.unlink()
        except OSError:
            return False
        return True

    body = _emit_toml_tables(remaining_tables).encode("utf-8")
    try:
        _atomic_write(path, body)
    except OSError:
        return False
    return True


def _escape_toml_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _emit_toml(key: str) -> str:
    return '[gemini]\napi_key = "' + _escape_toml_string(key) + '"\n'


def _emit_toml_tables(tables: dict[str, dict[str, str]]) -> str:
    """Emit a minimal TOML document of top-level tables of string values.

    Sufficient for our settings file today (``[gemini] api_key = "..."``)
    and for any near-future addition that follows the same shape. Numbers,
    booleans, arrays, and nested tables are intentionally omitted — when
    the first such field shows up, this emitter gets one new branch (and
    a test) rather than us reaching for a third-party TOML writer.
    """

    if not tables:
        return ""
    parts: list[str] = []
    for table_name, body in tables.items():
        parts.append(f"[{table_name}]")
        for key, value in body.items():
            parts.append(f'{key} = "{_escape_toml_string(value)}"')
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


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
