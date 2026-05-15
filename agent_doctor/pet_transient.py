"""Transient pet state overlay.

`pet_display` reads the main `pet-status.json` (autopilot-driven) on every
tick, then checks for `pet-transient.json`. If present and unexpired, the
transient state OVERLAYS the snapshot's state without clobbering any other
field. This lets dictate temporarily switch the pet to `listening` or
`thinking` without losing autopilot's `intervening` underneath.

The TTL is a safety net: a crashed pipeline cannot strand the pet in a
transient state forever.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SUPPORTED_TRANSIENT_STATES = ("listening", "thinking")
DEFAULT_TRANSIENT_DIR = Path("~/.agent-doctor/pet").expanduser()
TRANSIENT_FILE_NAME = "pet-transient.json"


def default_transient_file() -> Path:
    return DEFAULT_TRANSIENT_DIR / TRANSIENT_FILE_NAME


class PetTransientError(RuntimeError):
    pass


def write_transient(
    state: str,
    *,
    ttl_seconds: float,
    owner: str = "dictate",
    clock: Optional[Any] = None,
) -> Path:
    if state not in SUPPORTED_TRANSIENT_STATES:
        raise PetTransientError(
            f"state {state!r} is not transient; expected one of {SUPPORTED_TRANSIENT_STATES}"
        )
    now = (clock or time.time)()
    path = default_transient_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "owner": owner,
        "started_at": float(now),
        "expires_at": float(now) + float(ttl_seconds),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".pet-transient.", dir=str(path.parent))
    try:
        os.write(fd, json.dumps(payload, sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_name, path)
    return path


def read_transient(*, clock: Optional[Any] = None) -> Optional[dict[str, Any]]:
    path = default_transient_file()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    expires_at = float(payload.get("expires_at", 0))
    now = (clock or time.time)()
    if expires_at <= now:
        return None
    state = payload.get("state")
    if state not in SUPPORTED_TRANSIENT_STATES:
        return None
    return payload


def clear_transient(*, owner: str = "dictate") -> None:
    path = default_transient_file()
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        path.unlink(missing_ok=True)
        return
    if payload.get("owner", owner) == owner:
        path.unlink(missing_ok=True)


@contextmanager
def pet_state(state: str, *, ttl_seconds: float, owner: str = "dictate") -> Iterator[None]:
    """Write the transient state on enter, clear it on exit (even on exception)."""

    write_transient(state, ttl_seconds=ttl_seconds, owner=owner)
    try:
        yield
    finally:
        clear_transient(owner=owner)
