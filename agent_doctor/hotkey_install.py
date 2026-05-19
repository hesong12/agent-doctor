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

import hashlib
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


def _source_fingerprint(src: Path) -> str:
    """SHA-256 hex digest of the Swift source. Content-based so it is
    immune to mtime perturbations from tarball restore, scp -p, package
    extraction, etc. — see codex review feedback that motivated the
    switch from mtime to hash."""

    h = hashlib.sha256()
    with src.open("rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint_sidecar(helper: Path) -> Path:
    return helper.with_name(helper.name + ".source-sha256")


def _should_rebuild(src: Path, helper: Path) -> bool:
    """Return True if ``helper`` needs to be rebuilt from ``src``.

    Rebuild when ANY of:
    - ``helper`` does not exist (fresh install),
    - ``helper`` is not a regular file (broken/replaced by directory etc.),
    - ``helper`` is missing the user-execute bit (a previous restore
      stripped permissions and only ``build()`` chmods 0o755),
    - the sidecar fingerprint is missing or does not match the current
      source hash (upgrade path — bundled Swift bumped since last build).

    Otherwise the existing helper was compiled from this exact source
    revision, so we leave the file untouched. This is the whole point of
    the function: an unchanged binary keeps the same cdhash, and macOS
    Input Monitoring (TCC) tracks adhoc-signed binaries by cdhash. Any
    rebuild — even producing a byte-identical binary — silently
    invalidates the user's previously-granted Input Monitoring
    permission.
    """

    try:
        helper_stat = helper.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return True
    if not helper.is_file():
        return True
    # 0o100 == owner-execute bit; launchd cannot exec a non-executable file
    # so a helper without it would silently fail at bootstrap time.
    if not helper_stat.st_mode & 0o100:
        return True
    sidecar = _fingerprint_sidecar(helper)
    try:
        recorded = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        # No sidecar means we don't know what source built this helper.
        # Safer to rebuild than to assume it matches.
        return True
    try:
        current = _source_fingerprint(src)
    except OSError:
        # Source unreadable but helper is executable + has a recorded
        # fingerprint. Prefer reusing what's on disk over failing the
        # install entirely; launchd will surface a real exec error if
        # the helper itself turns out to be corrupt.
        return False
    return recorded != current


def install(*, agent_doctor_bin: Optional[str] = None) -> dict[str, object]:
    """Build the helper if needed, write the plist, and launchctl-bootstrap it.

    Rebuild policy: only recompile when :func:`_should_rebuild` says the
    on-disk helper is stale relative to the Swift source. Skipping the
    rebuild when the binary is already current preserves its cdhash so
    macOS Input Monitoring (TCC) does not silently invalidate the user's
    granted permission on every Background-daemon toggle.

    If ``agent_doctor_bin`` is not provided and an existing plist is on
    disk, reuse the value from that plist — preserves a power user's
    custom ``--agent-doctor-bin`` choice across resume/migration paths.
    Only falls back to ``which agent-doctor`` when there's no existing
    plist (i.e. true fresh install).
    """

    helper = DEFAULT_HELPER_PATH
    plist = DEFAULT_PLIST_PATH
    bin_path = (
        agent_doctor_bin
        or read_agent_doctor_bin()
        or shutil.which("agent-doctor")
        or "/usr/local/bin/agent-doctor"
    )
    rebuilt = False
    if _should_rebuild(SWIFT_SOURCE, helper):
        build(SWIFT_SOURCE, helper)
        # Record the source fingerprint next to the helper so the next
        # install() call can decide "rebuild or reuse" by content rather
        # than mtime (which restores/copies can perturb).
        try:
            _fingerprint_sidecar(helper).write_text(
                _source_fingerprint(SWIFT_SOURCE), encoding="utf-8"
            )
        except OSError:
            # Sidecar write failure is non-fatal — the next install()
            # will just rebuild again, which is correct behavior.
            pass
        rebuilt = True
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
        "rebuilt": rebuilt,
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


def resume(*, agent_doctor_bin: Optional[str] = None) -> dict[str, object]:
    """Delegate to :func:`install`. Kept as a named alias so the UI/CLI can
    distinguish "resume from pause" intent from "fresh install" in their
    messaging.

    The upgrade-safety concern (a pre-Handy-UX helper sitting on disk that
    doesn't understand ``right_cmd``) is now handled inside
    :func:`install` via :func:`_should_rebuild`: when the bundled Swift
    source is newer than the on-disk helper, install rebuilds; when they
    match, install reuses the binary so its cdhash — and the user's
    Input Monitoring grant — survive.
    """

    return install(agent_doctor_bin=agent_doctor_bin)


def uninstall() -> dict[str, str]:
    """Bootout the LaunchAgent and remove the plist."""

    plist = DEFAULT_PLIST_PATH
    _run_launchctl(["launchctl", "bootout", f"{_domain_target()}/{LABEL}"])
    plist.unlink(missing_ok=True)
    return {"plist_removed": str(plist)}


def read_agent_doctor_bin() -> Optional[str]:
    """Read the AGENT_DOCTOR_BIN value from the currently-installed plist.

    Returns None if the plist doesn't exist or the value isn't present.
    Used by migration paths to avoid overwriting a power-user's custom
    --agent-doctor-bin choice when rebuilding the helper.
    """

    plist = DEFAULT_PLIST_PATH
    if not plist.exists():
        return None
    try:
        with plist.open("rb") as fp:
            payload = plistlib.load(fp)
    except (OSError, plistlib.InvalidFileException):
        return None
    env = payload.get("EnvironmentVariables") or {}
    bin_path = env.get("AGENT_DOCTOR_BIN")
    return bin_path if isinstance(bin_path, str) else None


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
