# Dictate Phase 5 — Auto-paste at cursor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After the LLM-optimised prompt is written to the clipboard, if the user has enabled auto-paste, send Cmd+V into the focused app via `osascript` so the prompt lands at the cursor. Off by default; an opt-in `enable` command runs a permission-test first. A failure path notifies the user without losing the clipboard text.

**Architecture:** A new `dictate_paste.py` module owns the OS-level paste call, the permission-test helper, and the enable/disable settings flips. The dictate finish handler calls `dictate_paste.maybe_auto_paste(...)` after `copy_to_clipboard`. The CLI gets three new subcommands: `dictate paste {enable,disable,test}`.

**Tech Stack:** Python stdlib (subprocess + osascript). No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-05-14-dictate-handy-parity-design.md` §9.

**Prereq:** Phases 1–4 landed.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent_doctor/dictate_paste.py` | Create — `paste()`, `maybe_auto_paste()`, `permission_test()`, enable/disable. |
| `agent_doctor/cli.py` | Modify — register `dictate paste {enable,disable,test}`; call `maybe_auto_paste` in `_dictate_finish`. |
| `tests/test_dictate_paste.py` | Create — paste invocation, permission test branches, pipeline integration. |
| `tests/test_cli_subcommand_registration.py` | Modify — assert new subcommands. |

---

## Task 1: Create `dictate_paste.py` with paste + permission test

**Files:**
- Create: `agent_doctor/dictate_paste.py`
- Test: `tests/test_dictate_paste.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dictate_paste.py`:

```python
"""Tests for the auto-paste helper."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_doctor import dictate_paste as dp


def test_paste_invokes_osascript_keystroke() -> None:
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    dp.paste(runner=fake_runner, delay_seconds=0.0)
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "osascript"
    assert "-e" in argv
    script_idx = argv.index("-e") + 1
    assert 'keystroke "v" using {command down}' in argv[script_idx]


def test_paste_propagates_failure() -> None:
    def fake_runner(_argv: list[str]) -> int:
        return 17

    with pytest.raises(dp.PasteError, match="osascript"):
        dp.paste(runner=fake_runner, delay_seconds=0.0)


def test_permission_test_records_timestamp_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")

    def fake_runner(_argv: list[str]) -> int:
        return 0

    def fake_pbcopy(_argv: list[str], _data: bytes) -> int:
        return 0

    ok = dp.permission_test(runner=fake_runner, clipboard_runner=fake_pbcopy)
    assert ok is True
    settings = ds.load()
    assert settings.paste.last_permission_check is not None


def test_enable_requires_passing_permission_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")

    def failing_runner(_argv: list[str]) -> int:
        return 1

    with pytest.raises(dp.PasteError, match="permission"):
        dp.enable(runner=failing_runner, clipboard_runner=lambda *_: 0)
    settings = ds.load()
    assert settings.paste.auto_paste is False


def test_enable_flips_settings_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    dp.enable(runner=lambda _a: 0, clipboard_runner=lambda *_: 0)
    settings = ds.load()
    assert settings.paste.auto_paste is True


def test_disable_flips_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        paste=ds.PasteSettings(auto_paste=True),
    )
    ds.save(settings)
    dp.disable()
    assert ds.load().paste.auto_paste is False


def test_maybe_auto_paste_noop_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    called: list[bool] = []
    dp.maybe_auto_paste(runner=lambda _a: called.append(True) or 0)
    assert called == []


def test_maybe_auto_paste_runs_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        paste=ds.PasteSettings(auto_paste=True, paste_delay_ms=0),
    )
    ds.save(settings)
    called: list[list[str]] = []
    dp.maybe_auto_paste(runner=lambda argv: (called.append(argv), 0)[1])
    assert called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dictate_paste.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the module**

Create `agent_doctor/dictate_paste.py`:

```python
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
    """Synthesise Cmd+V via osascript. Raises PasteError on non-zero exit."""

    if sys.platform != "darwin":
        # Cross-platform paste is out of scope for v1; treat as no-op to keep
        # imports clean on Linux test runners.
        return
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    fn = runner or _default_osascript
    rc = fn(
        [
            "osascript",
            "-e",
            'tell application "System Events" to keystroke "v" using {command down}',
        ]
    )
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dictate_paste.py -v`
Expected: 8 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_paste.py tests/test_dictate_paste.py
git commit -m "feat(dictate): auto-paste at cursor with opt-in permission test"
```

---

## Task 2: Wire `maybe_auto_paste` into `_dictate_finish`

**Files:**
- Modify: `agent_doctor/cli.py`
- Test: `tests/test_dictate_paste.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dictate_paste.py`:

```python
def test_dictate_finish_calls_auto_paste_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: when auto_paste is on and pipeline succeeds, paste fires."""

    from agent_doctor import cli, dictate as _d, dictate_paste as dp, dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        paste=ds.PasteSettings(auto_paste=True, paste_delay_ms=0),
    )
    ds.save(settings)

    # Stub the full pipeline like in Phase 3.
    state = _d.DictateState(
        pid=12345,
        audio_path=str(tmp_path / "x.wav"),
        mode="optimize",
        started_at=0.0,
        recorder="sox",
    )
    (tmp_path / "x.wav").write_bytes(b"\x00")
    monkeypatch.setattr(_d, "default_state_dir", lambda: tmp_path)
    _d.write_state(state, state_dir=tmp_path)

    monkeypatch.setattr(_d, "is_pid_alive", lambda _pid: False)
    monkeypatch.setattr(_d, "stop_recording", lambda **_k: Path(state.audio_path))
    monkeypatch.setattr(_d, "transcribe", lambda *a, **k: "hello")
    monkeypatch.setattr(_d, "enhance_prompt", lambda *a, **k: "Hello.")
    monkeypatch.setattr(_d, "copy_to_clipboard", lambda *a, **k: None)
    monkeypatch.setattr(_d, "record_history", lambda **_k: 0)
    monkeypatch.setattr(_d, "notify", lambda *a, **k: None)
    monkeypatch.setattr(_d, "play_sound", lambda *a, **k: None)

    calls: list[list[str]] = []
    monkeypatch.setattr(dp, "_default_osascript", lambda argv: (calls.append(argv), 0)[1])
    rc = cli.main(["dictate", "stop"])
    assert rc == 0
    assert calls  # paste fired
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_dictate_paste.py::test_dictate_finish_calls_auto_paste_on_success -v`
Expected: FAIL — paste not invoked.

- [ ] **Step 3: Wire into the finish handler**

In `agent_doctor/cli.py`, find the section of `_dictate_finish` that calls `copy_to_clipboard` (search for `copy_to_clipboard`). Just after a successful clipboard write but before the user notification, add:

```python
            from . import dictate_paste as _dp
            err = _dp.maybe_auto_paste()
            if err is not None:
                _d.notify(
                    "agent-doctor",
                    "Auto-paste failed — text is on the clipboard, paste manually.",
                )
```

- [ ] **Step 4: Run the test**

Run: `python3 -m pytest tests/test_dictate_paste.py::test_dictate_finish_calls_auto_paste_on_success -v`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/cli.py tests/test_dictate_paste.py
git commit -m "feat(dictate): _dictate_finish triggers auto-paste when enabled"
```

---

## Task 3: Register `dictate paste {enable,disable,test}` subcommands

**Files:**
- Modify: `agent_doctor/cli.py`
- Test: `tests/test_cli_subcommand_registration.py`

- [ ] **Step 1: Add CLI registration**

In `agent_doctor/cli.py`, after the `dictate hotkey` block from Phase 4 Task 4, append:

```python
    dictate_paste = dictate_subs.add_parser(
        "paste",
        help="Configure auto-paste at cursor after the prompt is copied.",
    )
    dictate_paste_subs = dictate_paste.add_subparsers(
        dest="dictate_paste_cmd", required=True
    )

    paste_enable = dictate_paste_subs.add_parser(
        "enable",
        help="Run a Cmd+V permission test, then flip auto-paste ON.",
    )
    paste_enable.set_defaults(func=_cmd_dictate_paste_enable)

    paste_disable = dictate_paste_subs.add_parser(
        "disable", help="Flip auto-paste OFF."
    )
    paste_disable.set_defaults(func=_cmd_dictate_paste_disable)

    paste_test = dictate_paste_subs.add_parser(
        "test", help="Write a known phrase to the clipboard and try to paste it."
    )
    paste_test.set_defaults(func=_cmd_dictate_paste_test)
```

Add handlers at the end of `cli.py`:

```python
def _cmd_dictate_paste_enable(_args: argparse.Namespace) -> int:
    from . import dictate_paste as _dp

    try:
        _dp.enable()
    except _dp.PasteError as exc:
        print(
            f"agent-doctor: {exc}\n"
            "open: x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            file=sys.stderr,
        )
        return 2
    print("auto-paste: ON")
    return 0


def _cmd_dictate_paste_disable(_args: argparse.Namespace) -> int:
    from . import dictate_paste as _dp

    _dp.disable()
    print("auto-paste: OFF")
    return 0


def _cmd_dictate_paste_test(_args: argparse.Namespace) -> int:
    from . import dictate_paste as _dp

    ok = _dp.permission_test()
    if ok:
        print("paste test: OK (text 'agent-doctor paste test' should now be at your cursor)")
        return 0
    print(
        "paste test: FAILED — open System Settings -> Privacy & Security -> Accessibility "
        "and add your terminal / agent-doctor.",
        file=sys.stderr,
    )
    return 2
```

- [ ] **Step 2: Update the registration smoke test**

Append to `tests/test_cli_subcommand_registration.py`:

```python
EXPECTED_DICTATE_PASTE_SUBCOMMANDS = {"enable", "disable", "test"}


def test_dictate_paste_subcommands_registered() -> None:
    parser = cli.build_parser()
    dictate_sub = _nested_subparser(parser, "dictate")
    paste_sub = _nested_subparser(dictate_sub, "paste")
    assert _subparser_choices(paste_sub) >= EXPECTED_DICTATE_PASTE_SUBCOMMANDS
```

- [ ] **Step 3: Run the suite**

Run: `python3 -m pytest tests/test_cli_subcommand_registration.py tests/test_dictate_paste.py -v`
Expected: all green.

- [ ] **Step 4: Hand-run a smoke check**

```bash
python3 -m agent_doctor.cli dictate paste disable
python3 -m agent_doctor.cli dictate paste enable    # may fail without Accessibility permission; that's fine
```

Expected: `disable` exits 0 and updates settings; `enable` either succeeds + prints "auto-paste: ON" or exits 2 with the permissions hint.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/cli.py tests/test_cli_subcommand_registration.py
git commit -m "feat(cli): register dictate paste {enable,disable,test}"
```

---

## Task 4: README + final pass

- [ ] **Step 1: Document the paste commands**

In `README.md`:

```markdown
### Auto-paste at cursor

By default agent-doctor copies the optimised prompt to the clipboard and stops there. To have it land at the focused cursor automatically:

```bash
# One-time: runs a synthetic Cmd+V test and asks you to grant Accessibility permission.
agent-doctor dictate paste enable

# Re-test after enabling.
agent-doctor dictate paste test

# Stop auto-pasting.
agent-doctor dictate paste disable
```

If the keystroke fails (e.g. permission revoked), the text is still on the clipboard and a notification fires explaining the fallback.
```

- [ ] **Step 2: Run the suite**

Run: `python3 -m pytest -q -m "not tkinter"`
Expected: green.

- [ ] **Step 3: Commit + tag**

```bash
git add README.md
git commit -m "docs(paste): document opt-in auto-paste at cursor"
git tag dictate-phase-5-complete
```

---

## Phase 5 verification checklist

- [ ] `python3 -m pytest -q -m "not tkinter"` is green.
- [ ] `agent-doctor dictate paste enable` runs the permission test; succeeds only with Accessibility permission granted.
- [ ] With auto-paste ON, a full dictation cycle pastes the optimised prompt into the focused text field (verify in TextEdit / Cursor / Chrome).
- [ ] With auto-paste ON but permission revoked, the pipeline still writes the clipboard and posts a notification instead of crashing.
- [ ] `agent-doctor dictate paste disable` reverts the setting and disables paste.
- [ ] No new runtime dependencies introduced.
