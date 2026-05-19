"""Auto-paste at cursor using ``osascript`` Cmd+V keystrokes.

After ``copy_to_clipboard`` succeeds, if the user has enabled auto-paste, this
module sends a synthesised Cmd+V into the currently focused app. Requires
macOS Accessibility permission for the parent process.

All shell-outs go through ``runner`` so tests can stub them.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ClipboardRunner = Callable[[list[str], bytes], int]
OsascriptRunner = Callable[[list[str]], int]

PASTE_TEST_PHRASE = "agent-doctor paste test"


class PasteError(RuntimeError):
    pass


def _default_osascript(argv: list[str]) -> int:
    proc = subprocess.run(argv, check=False)
    return proc.returncode


def _default_pbcopy(argv: list[str], data: bytes) -> int:
    if not shutil.which(argv[0]):
        raise PasteError(f"{argv[0]} not found on PATH; clipboard copy requires macOS")
    proc = subprocess.run(argv, input=data, check=False)
    return proc.returncode


PASTE_REQUEST_PATH = Path(
    "~/Library/Application Support/agent-doctor/paste-request"
).expanduser()

# How long to wait for the helper to pick up the paste request file.
# The helper polls every 80 ms, so 500 ms is ~6 polls — plenty of
# margin while still failing fast if the helper isn't running.
_PASTE_REQUEST_TIMEOUT_S = 0.5


def paste(
    *,
    delay_seconds: float = 0.06,
    runner: Optional[OsascriptRunner] = None,
) -> None:
    """Trigger Cmd+V via the hotkey helper's CGEventPost path.

    The previous osascript-based path failed reliably from the
    daemon-spawned process chain: macOS attributes the keystroke
    request to ``/usr/bin/osascript`` (a system binary that the user
    cannot add to Accessibility), so System Events returned
    ``not allowed to send keystroke`` (error 1002).

    The hotkey helper IS in the user's Accessibility list (same binary
    they granted Input Monitoring to), and CGEventPost from inside the
    helper satisfies the TCC check. We coordinate via a sidecar file:
    Python drops ``~/Library/Application Support/agent-doctor/
    paste-request`` and the helper's poller picks it up within ~80 ms
    and synthesises the keystroke from helper context.

    The ``runner`` argument is kept for test backward-compat but
    unused on the helper-driven path. Tests can monkeypatch
    ``PASTE_REQUEST_PATH`` instead.
    """

    if sys.platform != "darwin":
        # Cross-platform paste is out of scope for v1; treat as no-op to keep
        # imports clean on Linux test runners.
        return
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    try:
        PASTE_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        PASTE_REQUEST_PATH.write_text(
            str(int(time.time())) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        sys.stderr.write(f"[paste] could not write request file: {exc}\n")
        sys.stderr.flush()
        raise PasteError(
            f"could not write paste request file ({exc}); helper may "
            "not be installed"
        ) from exc
    # Wait briefly for the helper to consume the request (it deletes
    # the file after delivering the keystroke). If the file is still
    # there after the timeout, the helper isn't running and we
    # surface a clear error.
    deadline = time.time() + _PASTE_REQUEST_TIMEOUT_S
    while time.time() < deadline:
        if not PASTE_REQUEST_PATH.exists():
            sys.stderr.write("[paste] helper consumed request\n")
            sys.stderr.flush()
            return
        time.sleep(0.05)
    # Helper never picked it up. Best-effort clean up so the next
    # paste attempt doesn't see a stale request.
    try:
        PASTE_REQUEST_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    sys.stderr.write(
        f"[paste] helper did not consume request within "
        f"{_PASTE_REQUEST_TIMEOUT_S}s — helper not running?\n"
    )
    sys.stderr.flush()
    raise PasteError(
        "hotkey helper did not respond to paste request; check that "
        "'agent-doctor dictate hotkey show' reports running: True"
    )


def permission_test(
    *,
    runner: Optional[OsascriptRunner] = None,
    clipboard_runner: Optional[ClipboardRunner] = None,
) -> bool:
    """Place a known phrase on the clipboard and try to paste.

    Returns True on success; updates ``paste.last_permission_check``.
    Returns False on failure; settings unchanged.
    """

    from . import dictate_settings as ds

    cb_fn = clipboard_runner or _default_pbcopy
    cb_rc = cb_fn(["pbcopy"], PASTE_TEST_PHRASE.encode("utf-8"))
    if cb_rc != 0:
        return False
    try:
        paste(delay_seconds=0.0, runner=runner)
    except PasteError:
        return False

    settings = ds.load()
    new_paste = ds.PasteSettings(
        auto_paste=settings.paste.auto_paste,
        paste_delay_ms=settings.paste.paste_delay_ms,
        last_permission_check=datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    )
    ds.save(ds.replace_section(settings, paste=new_paste))
    return True


def enable(
    *,
    runner: Optional[OsascriptRunner] = None,
    clipboard_runner: Optional[ClipboardRunner] = None,
) -> None:
    """Run the permission test, then flip ``paste.auto_paste`` on. Raises on failure."""

    from . import dictate_settings as ds

    ok = permission_test(runner=runner, clipboard_runner=clipboard_runner)
    if not ok:
        raise PasteError(
            "permission test failed; grant Accessibility permission and try again"
        )
    settings = ds.load()
    new_paste = ds.PasteSettings(
        auto_paste=True,
        paste_delay_ms=settings.paste.paste_delay_ms,
        last_permission_check=settings.paste.last_permission_check,
    )
    ds.save(ds.replace_section(settings, paste=new_paste))


def disable() -> None:
    from . import dictate_settings as ds

    settings = ds.load()
    new_paste = ds.PasteSettings(
        auto_paste=False,
        paste_delay_ms=settings.paste.paste_delay_ms,
        last_permission_check=settings.paste.last_permission_check,
    )
    ds.save(ds.replace_section(settings, paste=new_paste))


def maybe_auto_paste(
    *,
    runner: Optional[OsascriptRunner] = None,
) -> Optional[PasteError]:
    """Paste only if ``paste.auto_paste`` is set. Returns the captured PasteError
    on osascript failure so the caller can surface a notification while keeping
    the clipboard text intact."""

    from . import dictate_settings as ds

    settings = ds.load()
    if not settings.paste.auto_paste:
        return None
    try:
        paste(
            delay_seconds=max(0, settings.paste.paste_delay_ms) / 1000.0,
            runner=runner,
        )
    except PasteError as exc:
        return exc
    return None
