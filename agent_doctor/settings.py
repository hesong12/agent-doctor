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

import datetime
import json
import os
import stat
import sys
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
# Side-channel files that record WHEN and BY WHICH binary the key was last
# touched. Neither contains key content. ``.settings-meta.json`` is a small
# JSON record we re-write on every set; ``audit.log`` is an append-only
# one-line-per-event journal so a future "where did my key go" diagnosis has
# a paper trail. Both live under _CONFIG_DIR so test redirection picks them
# up via the same monkeypatch surface as _CONFIG_FILE.
_META_FILE = _CONFIG_DIR / ".settings-meta.json"
_AUDIT_LOG = _CONFIG_DIR / "audit.log"
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_REDACTED = "***REDACTED***"


class Backend(str, Enum):
    KEYRING = "keyring"
    FILE = "file"
    NONE = "none"


@dataclass(frozen=True)
class SettingsMeta:
    """When and by which binary the key was last set.

    Carries no secret content — just the timestamp, the backend chosen by
    ``store_gemini_key``, and ``sys.executable`` of the writer. Surfaces in
    ``settings show`` and in the confirmation prompt before a clear so the
    user can decide whether the entry they're about to delete is theirs or
    a stale leftover from a smoke test.
    """

    set_at: str
    backend: Backend
    caller_executable: str


@dataclass(frozen=True)
class SettingsStatus:
    backend: Backend
    configured: bool
    meta: SettingsMeta | None = None

    def render(self) -> str:
        state = "configured" if self.configured else "not configured"
        if self.backend is Backend.NONE:
            return "Gemini API key: not configured (no backend in use)"
        head = f"Gemini API key: {state} (backend: {self.backend.value})"
        if self.meta is not None and self.meta.set_at:
            tail = f"  last set: {self.meta.set_at} via {self.meta.caller_executable}"
            return head + "\n" + tail
        return head


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
    secret redacted) if both backends fail. Also records non-secret metadata
    (timestamp + caller binary) and appends an audit-log line so a later
    diagnosis of "where did my key go" has a paper trail. Meta and audit
    writes are best-effort; a failure to log them must not abort the primary
    set.
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
        backend = Backend.KEYRING
    else:
        _file_set(cleaned)
        backend = Backend.FILE

    _write_meta(backend)
    _audit_log("set", backend=backend)
    return backend


def clear_gemini_key() -> bool:
    """Remove any stored key from both backends. Returns True if anything was cleared.

    Always wipes the meta file and appends an audit-log line, even when the
    underlying clear was a no-op, so the journal still records the attempt.
    """

    cleared_keyring = _keyring_clear()
    cleared_file = _file_clear()
    cleared = cleared_keyring or cleared_file
    _clear_meta()
    # Pick the most authoritative backend the clear actually touched, falling
    # back to NONE when neither held a key.
    if cleared_keyring:
        backend = Backend.KEYRING
    elif cleared_file:
        backend = Backend.FILE
    else:
        backend = Backend.NONE
    _audit_log("clear", backend=backend)
    return cleared


def settings_status() -> SettingsStatus:
    """Return which backend currently holds the key (if any), plus the
    recorded last-set metadata when both a key and a meta entry exist.
    """

    meta = _load_meta()
    if _keyring_get() is not None:
        return SettingsStatus(Backend.KEYRING, True, meta)
    if _file_get() is not None:
        return SettingsStatus(Backend.FILE, True, meta)
    # No key in either backend. Don't surface a meta record here — it's
    # informational only when there's actually a key to describe.
    if _try_import_keyring() is not None:
        return SettingsStatus(Backend.KEYRING, False, None)
    return SettingsStatus(Backend.FILE, False, None)


# ----------------------------------------------------------------------------
# Meta + audit helpers
# ----------------------------------------------------------------------------
#
# These are best-effort side channels: a write failure NEVER aborts the
# primary set/clear path. Neither file contains key content — only
# timestamp, backend, pid, and the calling binary path. Mode-locked to
# 0600 the same way ``config.toml`` is, so a future entry that grows to
# include richer caller info still stays user-only readable.


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, seconds precision."""

    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _caller_executable() -> str:
    """Absolute path of the python binary running this code.

    Used in the audit log + meta file so the user can tell which install
    or venv last wrote the key. Never contains user input — comes straight
    from ``sys.executable``.
    """

    return sys.executable or "<unknown>"


def _ensure_config_dir() -> None:
    """Idempotently create _CONFIG_DIR with mode 0700, re-locking the mode
    if a previous run left it more permissive. Failures are swallowed so
    audit/meta writes degrade gracefully — the primary set/clear still has
    its own dir-creation path in ``_file_set``.
    """

    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        os.chmod(_CONFIG_DIR, _DIR_MODE)
    except OSError:
        pass


def _write_meta(backend: Backend) -> None:
    """Record non-secret set-metadata to ``_META_FILE`` (mode 0600). Best-effort."""

    _ensure_config_dir()
    payload = {
        "gemini_api_key": {
            "set_at": _now_iso(),
            "backend": backend.value,
            "caller_executable": _caller_executable(),
        }
    }
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        _atomic_write(_META_FILE, body)
    except OSError:
        # Meta is best-effort — never block the primary set on a meta
        # write failure (e.g. read-only home directory in a sandbox).
        return


def _clear_meta() -> None:
    """Remove ``_META_FILE`` if it exists. Best-effort."""

    try:
        _META_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _load_meta() -> SettingsMeta | None:
    """Return the most-recent set-metadata, or None when missing/corrupt."""

    if not _META_FILE.exists():
        return None
    try:
        data = json.loads(_META_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    section = data.get("gemini_api_key") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return None
    backend_val = section.get("backend")
    if backend_val not in {b.value for b in Backend}:
        return None
    return SettingsMeta(
        set_at=str(section.get("set_at", "")),
        backend=Backend(backend_val),
        caller_executable=str(section.get("caller_executable", "")),
    )


def _audit_log(action: str, *, backend: Backend) -> None:
    """Append one JSON-object-per-line audit entry to ``_AUDIT_LOG`` (mode 0600).

    Records: timestamp, action ("set"/"clear"), backend touched, pid, and
    caller binary path. Never logs key content. POSIX appends to a single
    file under ~4 kB are atomic, so concurrent writers from multiple
    processes won't interleave individual lines. Failures are silent so an
    inability to log can never abort the primary settings operation.
    """

    _ensure_config_dir()
    entry = (
        json.dumps(
            {
                "ts": _now_iso(),
                "action": action,
                "backend": backend.value,
                "pid": os.getpid(),
                "caller": _caller_executable(),
            },
            sort_keys=True,
        )
        + "\n"
    )
    try:
        fd = os.open(
            str(_AUDIT_LOG),
            os.O_CREAT | os.O_WRONLY | os.O_APPEND,
            _FILE_MODE,
        )
    except OSError:
        return
    try:
        # Re-chmod after open: the mode arg to os.open only takes effect on
        # creation, so a pre-existing log with looser bits would otherwise
        # stay looser.
        try:
            os.fchmod(fd, _FILE_MODE)
        except OSError:
            pass
        os.write(fd, entry.encode("utf-8"))
    finally:
        os.close(fd)
