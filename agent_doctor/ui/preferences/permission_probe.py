"""Detect macOS Accessibility + Input Monitoring permission state.

We can't read TCC.db directly without full disk access, so each probe is
behaviour-based: ``accessibility_probe`` runs a tiny AppleScript that needs
Accessibility. ``input_monitoring_probe`` reads a heartbeat file the Swift
helper rewrites on every global event — see
``_default_input_monitoring_probe`` for the rationale. Callers can inject
custom probes for testing.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_FIRST_ORDER = ("accessibility", "input_monitoring")

_URLS = {
    "accessibility": (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    ),
    "input_monitoring": (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
    ),
}

_INPUT_MONITORING_HEARTBEAT_PATH = Path(
    "~/Library/Application Support/agent-doctor/im-heartbeat"
).expanduser()

_INPUT_MONITORING_STARTUP_PATH = Path(
    "~/Library/Application Support/agent-doctor/im-startup"
).expanduser()

_INPUT_MONITORING_HEARTBEAT_FRESH_S = 60


@dataclass(frozen=True)
class PermissionStatus:
    accessibility: bool
    input_monitoring: bool
    first_missing: Optional[str]


def _default_accessibility_probe() -> bool:
    """Return True if the running process can drive the system event stream.

    AppleScript ``System Events`` calls fail with osascript exit code 1 when
    Accessibility is not granted. We also treat osascript missing, timing
    out, or any other subprocess failure as "not granted" — the worst that
    happens is the user sees a "permission needed" pill when they shouldn't,
    which is better than crashing Preferences.
    """

    try:
        proc = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first process',
            ],
            capture_output=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _default_input_monitoring_probe(
    heartbeat_path: Path = _INPUT_MONITORING_HEARTBEAT_PATH,
    startup_path: Path = _INPUT_MONITORING_STARTUP_PATH,
) -> bool:
    """Return True iff the helper has received events since *this* daemon
    started, AND the latest event was within the freshness window.

    The helper writes ``im-startup`` once at process start (before
    installing the event monitor) and refreshes ``im-heartbeat`` on
    every global event. Comparing the two mtimes eliminates the false
    positive where a leftover heartbeat from a previous daemon lifetime
    makes IM look granted after the user has revoked it — the symptom
    that motivated this rewrite (PRα-3).

    Decision matrix:
    - startup missing            → daemon never started cleanly → False
    - heartbeat missing          → daemon started, no event yet → False
    - heartbeat <= startup       → only old events on disk      → False
    - heartbeat fresher than the freshness window               → True
    - heartbeat older than the freshness window                 → False

    First-install UX: user sees "Permission needed" until they press
    the bound key once, even though IM may already be granted. This is
    the safer direction — false positives ("Active" but no events)
    would silently break dictation; false negatives ("Permission
    needed" prompt to press the key) just nudge the user to test.
    """

    try:
        heartbeat_mtime = heartbeat_path.stat().st_mtime
    except OSError:
        return False
    age_s = time.time() - heartbeat_mtime
    if age_s >= _INPUT_MONITORING_HEARTBEAT_FRESH_S:
        return False
    # The startup stamp was added in PRα-3. Legacy helpers from before
    # that PR write only the heartbeat — for those, fall back to the
    # original "heartbeat freshness" probe so an in-place agent-doctor
    # upgrade does not falsely report Input Monitoring missing. Codex
    # review caught this regression path; users see "Permission needed"
    # only AFTER their next dictate-hotkey-install rebuilds the helper
    # to the version that writes im-startup.
    try:
        startup_mtime = startup_path.stat().st_mtime
    except OSError:
        return True
    # New-protocol helpers: heartbeat must be strictly newer than the
    # startup stamp, otherwise we're looking at events from a previous
    # daemon lifetime (or no events at all yet from this one).
    return heartbeat_mtime > startup_mtime


def check_macos_permissions(
    *,
    accessibility_probe: Optional[Callable[[], bool]] = None,
    input_monitoring_probe: Optional[Callable[[], bool]] = None,
) -> PermissionStatus:
    a = (accessibility_probe or _default_accessibility_probe)()
    im = (input_monitoring_probe or _default_input_monitoring_probe)()
    first = None
    for name, ok in zip(_FIRST_ORDER, (a, im)):
        if not ok:
            first = name
            break
    return PermissionStatus(
        accessibility=a, input_monitoring=im, first_missing=first
    )


def settings_url(pane: str) -> str:
    return _URLS[pane]
