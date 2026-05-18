"""Detect macOS Accessibility + Input Monitoring permission state.

We can't read TCC.db directly without full disk access, so each probe is
behaviour-based: ``accessibility_probe`` runs a tiny AppleScript that needs
Accessibility. ``input_monitoring_probe`` is intentionally optimistic — see
``_default_input_monitoring_probe`` for the rationale. Callers can inject
custom probes for testing.
"""

from __future__ import annotations

import subprocess
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
    """Return True — we cannot reliably detect Input Monitoring revocation.

    The helper's stdout/stderr are redirected to /dev/null when invoking
    ``agent-doctor dictate ...`` child commands, and the LaunchAgent's own
    stdout/stderr log file stays empty in the healthy case (the helper
    only writes to stderr on errors). Reading the TCC.db directly requires
    Full Disk Access we don't ship. So this probe is optimistic; if IM is
    revoked, the user will observe that the hotkey stops working and can
    inspect ``~/Library/Logs/agent-doctor-hotkey.err.log`` for clues. The
    Accessibility probe remains the actionable signal in the pill state.

    The ``log_path`` parameter is retained for forward compatibility — if
    we later add a heartbeat-write in the Swift helper, this probe will
    revert to a real check based on log freshness.
    """

    return True


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
