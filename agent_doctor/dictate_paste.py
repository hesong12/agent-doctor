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


def paste(
    *,
    delay_seconds: float = 0.06,
    runner: Optional[OsascriptRunner] = None,
) -> None:
    """Synthesise Cmd+V via osascript. Raises PasteError on non-zero exit.

    Diagnostic logging on stderr lands in the LaunchAgent log when this
    path runs daemon-spawned, so the user-flagged "auto-paste needs
    manual paste" failure mode surfaces with the actual osascript exit
    code rather than silently succeeding.
    """

    if sys.platform != "darwin" and runner is None:
        # Cross-platform paste is out of scope for v1; treat as no-op to keep
        # imports clean on Linux test runners. Tests inject a runner explicitly.
        return
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    fn = runner or _default_osascript
    # Activating the frontmost app before the keystroke makes the
    # daemon-spawned paste reliable: without it, the in-flight pet pop-up
    # or any focus drift during whisper/LLM (5-10s pipeline) can swallow
    # the synthesised Cmd+V. The script asks System Events for whoever
    # is frontmost right now and activates it before pressing the keys —
    # which routes the keystroke to the user's text input (Cursor /
    # Notes / Terminal) instead of e.g. the pet sprite.
    script = """
        tell application "System Events"
            set frontProc to first process whose frontmost is true
            set frontName to name of frontProc
        end tell
        tell application frontName to activate
        delay 0.05
        tell application "System Events" to keystroke "v" using {command down}
    """
    rc = fn(["osascript", "-e", script])
    sys.stderr.write(f"[paste] osascript rc={rc}\n")
    sys.stderr.flush()
    if rc != 0:
        raise PasteError(
            f"osascript paste keystroke exited with {rc} — likely missing "
            "Accessibility permission"
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
