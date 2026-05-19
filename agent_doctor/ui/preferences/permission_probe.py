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
    """Return True iff the helper has written a heartbeat in the last N seconds.

    The helper touches the heartbeat file on every global event it receives.
    A fresh heartbeat (default: within 60s) means events are flowing, which
    in turn requires Input Monitoring permission to be granted on macOS.
    Missing or stale heartbeat means either the helper isn't running or
    IM is revoked.

    The helper also writes a one-shot ``im-startup`` stamp right before
    installing its event monitor. We require ``heartbeat.mtime >
    startup.mtime``: if the helper just relaunched (e.g. because the user
    revoked IM and the install flow re-bootstrapped the LaunchAgent) and no
    events have arrived since, the heartbeat carries forward from the
    previous run but the startup stamp is newer — the probe reports IM as
    "not confirmed" rather than echoing the stale signal back as "granted".

    Backward compatibility: if ``startup_path`` does not exist (a helper
    binary that predates the startup stamp is still installed on the
    user's machine), we fall back to the original "fresh heartbeat alone"
    check so we don't regress upgrade-in-place users.

    Note: a freshly installed helper that has not yet received any event
    will have no heartbeat — the probe correctly reports IM as "not yet
    confirmed" (False). Users will see "Permission needed" until they
    press a key while the helper is registered. This is the conservative
    direction: false negatives (you see Permission needed but IM is OK
    and you just haven't pressed anything yet) are fine; false positives
    (you see Listening but no events flow) are not.
    """

    if not heartbeat_path.exists():
        return False
    try:
        heartbeat_mtime = heartbeat_path.stat().st_mtime
    except OSError:
        return False

    age_s = time.time() - heartbeat_mtime
    if age_s >= _INPUT_MONITORING_HEARTBEAT_FRESH_S:
        return False

    # Legacy fallback: helper without the startup stamp (pre-this-PR) gets
    # the original behavior — fresh heartbeat alone is sufficient.
    if not startup_path.exists():
        return True
    try:
        startup_mtime = startup_path.stat().st_mtime
    except OSError:
        # Startup stat failed but heartbeat is fresh — be conservative and
        # treat as not-confirmed rather than falsely-confirmed.
        return False
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
