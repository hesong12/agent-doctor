"""Build the Swift hotkey helper and register it with launchd.

Public API:
- ``build(src, dest)`` — compile the Swift source.
- ``write_plist(path, helper, agent_doctor_bin)`` — produce the LaunchAgent plist.
- ``install(agent_doctor_bin=None)`` — build, write plist, launchctl bootstrap.
- ``sighup()`` — kick the running daemon so it re-reads ``dictate.json``.
- ``pause()`` — stop the running LaunchAgent without removing the plist.
- ``resume()`` — re-bootstrap an existing plist (no rebuild).
- ``uninstall()`` — launchctl bootout + remove the plist.

All shell-outs go through ``_run_launchctl`` so tests can stub them.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Optional

LABEL = "com.agent-doctor.hotkey"
DEFAULT_HELPER_PATH = Path(
    "~/Library/Application Support/agent-doctor/bin/agent-doctor-hotkey"
).expanduser()
DEFAULT_PLIST_PATH = Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()
SWIFT_SOURCE = Path(__file__).with_name("hotkey") / "HotkeyHelper.swift"


class HotkeyInstallError(RuntimeError):
    """Raised when the hotkey helper cannot be built or registered."""


def build(src: Path, dest: Path) -> Path:
    """Compile ``src`` with ``swiftc`` into ``dest`` (chmod 0755)."""

    swiftc = shutil.which("swiftc")
    if swiftc is None:
        raise HotkeyInstallError(
            "swiftc not found on PATH; install Xcode Command Line Tools with "
            "'xcode-select --install'"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Augment PATH for the child so shebangs like ``#!/usr/bin/env bash``
    # in build wrappers can still resolve when callers set a sparse PATH.
    child_env = dict(os.environ)
    child_env["PATH"] = (
        child_env.get("PATH", "")
        + os.pathsep
        + "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    )
    proc = subprocess.run(
        [swiftc, "-O", str(src), "-o", str(dest)],
        capture_output=True,
        check=False,
        env=child_env,
    )
    if proc.returncode != 0:
        raise HotkeyInstallError(
            f"swiftc failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    dest.chmod(0o755)
    return dest


def write_plist(path: Path, helper: Path, agent_doctor_bin: str) -> Path:
    """Write a LaunchAgent plist that runs ``helper`` with ``agent_doctor_bin`` in env."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(helper)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {
            "AGENT_DOCTOR_BIN": agent_doctor_bin,
            # macOS does not inherit PATH for launchd-managed processes; bake one.
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        },
        "StandardOutPath": str(
            Path("~/Library/Logs/agent-doctor-hotkey.log").expanduser()
        ),
        "StandardErrorPath": str(
            Path("~/Library/Logs/agent-doctor-hotkey.err.log").expanduser()
        ),
    }
    body = plistlib.dumps(payload)
    path.write_bytes(body)
    return path


def _run_launchctl(
    argv: list[str], *, check: bool = False
) -> subprocess.CompletedProcess:
    """Run a launchctl command. Centralised so tests can stub one place."""

    return subprocess.run(argv, capture_output=True, check=check)


def _domain_target() -> str:
    return f"gui/{os.getuid()}"


def install(*, agent_doctor_bin: Optional[str] = None) -> dict[str, str]:
    """Build the helper, write the plist, and launchctl-bootstrap it."""

    helper = DEFAULT_HELPER_PATH
    plist = DEFAULT_PLIST_PATH
    bin_path = (
        agent_doctor_bin
        or shutil.which("agent-doctor")
        or "/usr/local/bin/agent-doctor"
    )
    build(SWIFT_SOURCE, helper)
    write_plist(plist, helper, bin_path)
    # Best-effort bootout in case a stale agent is loaded; ignore its rc.
    _run_launchctl(["launchctl", "bootout", f"{_domain_target()}/{LABEL}"])
    proc = _run_launchctl(
        ["launchctl", "bootstrap", _domain_target(), str(plist)]
    )
    if proc.returncode != 0:
        raise HotkeyInstallError(
            f"launchctl bootstrap failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    return {
        "helper": str(helper),
        "plist": str(plist),
        "agent_doctor_bin": bin_path,
    }


def sighup() -> bool:
    """Send SIGHUP to the running daemon. Returns True on success."""

    proc = _run_launchctl(
        ["launchctl", "kill", "SIGHUP", f"{_domain_target()}/{LABEL}"]
    )
    return proc.returncode == 0


def pause() -> bool:
    """Stop the running LaunchAgent without removing the plist.

    Returns True iff launchctl bootout reported success. The plist itself is
    untouched so a subsequent :func:`resume` can re-bootstrap without a full
    rebuild.
    """

    proc = _run_launchctl(["launchctl", "bootout", f"{_domain_target()}/{LABEL}"])
    return proc.returncode == 0


def resume(*, agent_doctor_bin: Optional[str] = None) -> dict[str, str]:
    """Rebuild the helper from current Swift source, write the plist, and
    bootstrap. Equivalent to :func:`install` — kept as a named alias so the
    UI/CLI can distinguish "resume from pause" intent from "fresh install"
    in their messaging.

    Both paths must rebuild so an upgraded user with a stale on-disk helper
    from a previous version gets the current Swift source compiled (e.g.
    pre-Handy-UX helpers don't understand ``right_cmd`` and would silently
    fail). The extra ``swiftc`` cost on resume is multi-second but is worth
    the correctness guarantee.
    """

    return install(agent_doctor_bin=agent_doctor_bin)


def uninstall() -> dict[str, str]:
    """Bootout the LaunchAgent and remove the plist."""

    plist = DEFAULT_PLIST_PATH
    _run_launchctl(["launchctl", "bootout", f"{_domain_target()}/{LABEL}"])
    plist.unlink(missing_ok=True)
    return {"plist_removed": str(plist)}


def status() -> dict[str, object]:
    """Report whether the plist, helper, and running agent are present.

    Defensive against missing ``launchctl`` (non-macOS, sandboxed env, broken
    PATH): if the subprocess raises :class:`FileNotFoundError` or other OS
    errors, treat the agent as "not running" rather than crashing the
    caller.
    """

    plist_exists = DEFAULT_PLIST_PATH.exists()
    helper_exists = DEFAULT_HELPER_PATH.exists()
    try:
        proc = _run_launchctl(
            ["launchctl", "print", f"{_domain_target()}/{LABEL}"]
        )
        running = proc.returncode == 0
    except (FileNotFoundError, OSError):
        running = False
    return {
        "plist": str(DEFAULT_PLIST_PATH),
        "plist_exists": plist_exists,
        "helper": str(DEFAULT_HELPER_PATH),
        "helper_exists": helper_exists,
        "running": running,
    }
