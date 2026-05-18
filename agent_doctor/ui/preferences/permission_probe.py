"""Detect macOS Accessibility + Input Monitoring permission state.

We can't read TCC.db directly without full disk access, so each probe is
behaviour-based: ``accessibility_probe`` runs a tiny AppleScript that needs
Accessibility, and ``input_monitoring_probe`` checks whether the hotkey
helper has been able to receive global events recently. Callers can inject
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

# If the helper hasn't logged in this window, assume Input Monitoring
# was revoked. Tuned to balance false positives (user away) vs
# false negatives (permission lost long ago).
_INPUT_MONITORING_FRESH_DAYS = 30

_INPUT_MONITORING_LOG_PATH = Path("~/Library/Logs/agent-doctor-hotkey.log").expanduser()


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
    log_path: Path = _INPUT_MONITORING_LOG_PATH,
) -> bool:
    """Return True if a recent run of the hotkey helper succeeded.

    Heuristic: presence of the helper log file with non-empty content within
    the last ``_INPUT_MONITORING_FRESH_DAYS`` days. Refined detection requires
    private APIs we don't ship.
    """

    if not log_path.exists():
        return False
    try:
        stat = log_path.stat()
    except OSError:
        return False
    if stat.st_size == 0:
        return False
    age_days = (time.time() - stat.st_mtime) / 86400
    return age_days < _INPUT_MONITORING_FRESH_DAYS


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
