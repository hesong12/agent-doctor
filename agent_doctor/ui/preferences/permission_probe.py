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

_INPUT_MONITORING_HEARTBEAT_FRESH_S = 60


@dataclass(frozen=True)
class PermissionStatus:
    accessibility: bool
    input_monitoring: bool
    first_missing: Optional[str]


def _default_accessibility_probe() -> bool:
    """Return True if the running process can drive the system event stream.

    AppleScript ``System Events`` calls fail with osascript exit code 1 when
    Accessibility is not granted.
    """

    proc = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to get name of first process',
        ],
        capture_output=True,
        timeout=2.0,
    )
    return proc.returncode == 0


def _default_input_monitoring_probe(
    heartbeat_path: Path = _INPUT_MONITORING_HEARTBEAT_PATH,
) -> bool:
    """Return True iff the helper has written a heartbeat in the last N seconds.

    The helper touches the heartbeat file on every global event it receives.
    A fresh heartbeat (default: within 60s) means events are flowing, which
    in turn requires Input Monitoring permission to be granted on macOS.
    Missing or stale heartbeat means either the helper isn't running or
    IM is revoked.

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
        age_s = time.time() - heartbeat_path.stat().st_mtime
    except OSError:
        return False
    return age_s < _INPUT_MONITORING_HEARTBEAT_FRESH_S


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
