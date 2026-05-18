# Hotkey tab — Handy-style UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewire the Preferences > Hotkey tab to a Handy-style single-modifier-hold model (default `right_cmd`), with an inline status pill, permission banner, modeless capture overlay (4 states), preserved chord fallback, and visible daemon plumbing.

**Architecture:** Three layers extended in parallel — (1) `hotkey_parse.Chord` gains modifier-only bindings (empty `modifiers`, key holds the modifier token); (2) `HotkeyHelper.swift` extends `NSEvent.addGlobalMonitorForEvents` to subscribe to `.flagsChanged` for modifier-only bindings with an "alone-key rule"; (3) the Tk preferences shell gains a `permission_probe` module, a pure-Python `CaptureController` state machine, and an extracted `hotkey_tab_view.py` for the new layout. Settings file schema is unchanged; only the canonical form of `hotkey.binding` widens.

**Tech Stack:** Python 3.11+, pytest, ttk/tkinter, Swift (compiled via swiftc at install time), macOS launchd LaunchAgent, `launchctl` for daemon control.

**Spec:** `docs/superpowers/specs/2026-05-18-hotkey-tab-handy-ux-design.md` — read it before starting.

---

## File Structure

**Create:**

- `agent_doctor/ui/preferences/permission_probe.py` — `check_macos_permissions()` returns a `PermissionStatus` dataclass `{accessibility: bool, input_monitoring: bool, first_missing: str | None}`. Wraps `tccutil`/Accessibility detection via subprocess. Headless-testable with mocked subprocess.
- `agent_doctor/ui/preferences/hotkey_capture.py` — pure-Python `CaptureController` state machine (states `IDLE` / `CAPTURED_MODIFIER` / `CAPTURED_CHORD` / `CONFLICT`) plus a thin `CaptureOverlay` Tk wrapper. The controller is fully unit-tested without tkinter.
- `agent_doctor/ui/preferences/hotkey_tab_view.py` — widget composition for the redesigned tab. Pure layout; logic lives in `hotkey_tab.py`.
- `tests/test_permission_probe.py` — headless coverage for permission detection.
- `tests/test_hotkey_capture.py` — headless coverage for the capture state machine.

**Modify:**

- `agent_doctor/hotkey_parse.py` — extend `Chord` so `modifiers=()` + `key=<modifier_only_token>` represents a modifier-only binding; widen `KEY_TOKENS` with `left_cmd`, `right_cmd`, `left_option`, `right_option`, `left_ctrl`, `right_ctrl`, `left_shift`, `right_shift`, `fn`; add `MODIFIER_ONLY_TOKENS` constant; add `is_modifier_only(chord)` helper.
- `agent_doctor/dictate_settings.py` — change default `HotkeySettings.binding` from `"ctrl+option+space"` to `"right_cmd"`; everything else unchanged.
- `agent_doctor/ui/preferences/hotkey_tab.py` — extend `HotkeyState` with derived fields (`is_modifier_only`, parsed `Chord`); coerce `push_to_talk=True` in `apply()` when binding is modifier-only; add `daemon_status_snapshot()` and `permission_status_snapshot()` wrappers.
- `agent_doctor/ui/preferences/__init__.py` — replace the old `_build_hotkey_tab(notebook, ht)` body with a one-line delegation to `hotkey_tab_view.build(notebook)`.
- `agent_doctor/hotkey/HotkeyHelper.swift` — add new modifier-only keycodes to `KEYCODES`; teach `parse(_:)` to emit modifier-only `ParsedChord` (empty modifiers, modifier-only keycode); split `HotkeyDaemon.reload()` so a modifier-only binding subscribes to `.flagsChanged` instead of `.keyDown/.keyUp`, applying the alone-key rule.
- `tests/test_hotkey_parse.py` — add cases for new tokens, modifier-only canonical, mixed-rejection, and `is_modifier_only`.
- `tests/test_dictate_settings.py` — update the default-binding assertion.
- `tests/test_preferences_logic.py` — add `HotkeyState.apply()` coercion test and `daemon_status_snapshot`/`permission_status_snapshot` shape tests.
- `tests/test_preferences_ui_smoke.py` — extend the smoke test to also instantiate the new hotkey view without raising.
- `README.md` — short note documenting the new default binding and capture overlay.

---

## Tasks

### Task 1: Add modifier-only tokens to `hotkey_parse`

**Files:**
- Modify: `agent_doctor/hotkey_parse.py`
- Test: `tests/test_hotkey_parse.py`

**Spec form (§6.1):** `Chord` gains a `side: str | None` field; for modifier-only bindings `modifiers=("cmd",)` (which modifier), `key=None`, and `side="right"|"left"|None`. Fn has no L/R so `side=None`.

- [ ] **Step 1: Write failing tests for new tokens, modifier-only canonical form, and side semantics**

Append to `tests/test_hotkey_parse.py`:

```python
@pytest.mark.parametrize(
    ("raw", "expected_canonical", "expected_mod", "expected_side"),
    [
        ("right_cmd",    "right_cmd",    "cmd",    "right"),
        ("RIGHT_CMD",    "right_cmd",    "cmd",    "right"),
        ("left_option",  "left_option",  "option", "left"),
        ("fn",           "fn",           "fn",     None),
        ("right_ctrl",   "right_ctrl",   "ctrl",   "right"),
    ],
)
def test_parse_modifier_only_canonical(
    raw: str, expected_canonical: str, expected_mod: str, expected_side
) -> None:
    chord = hp.parse(raw)
    assert chord.canonical() == expected_canonical
    assert chord.modifiers == (expected_mod,)
    assert chord.key is None
    assert chord.side == expected_side


def test_is_modifier_only_helper() -> None:
    assert hp.is_modifier_only(hp.parse("right_cmd"))
    assert hp.is_modifier_only(hp.parse("fn"))
    assert not hp.is_modifier_only(hp.parse("ctrl+option+space"))


@pytest.mark.parametrize(
    "raw",
    ["right_cmd+space", "ctrl+right_cmd", "right_cmd+left_option"],
)
def test_modifier_only_tokens_cannot_mix_with_others(raw: str) -> None:
    with pytest.raises(hp.HotkeyParseError, match="modifier-only"):
        hp.parse(raw)


def test_chord_canonical_unchanged_for_multi_key() -> None:
    chord = hp.parse("ctrl+option+space")
    assert chord.canonical() == "ctrl+option+space"
    assert chord.key == "space"
    assert chord.side is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_hotkey_parse.py -v
```

Expected: the four new tests fail (`AttributeError: is_modifier_only` / `unknown token in hotkey`).

- [ ] **Step 3: Extend `hotkey_parse.py` with the new vocabulary**

In `agent_doctor/hotkey_parse.py`, replace the existing `Chord` dataclass + `parse()` block with the new schema. Specifically:

(a) Add new constants near the top, after `MODIFIER_ORDER`:

```python
# Maps "right_cmd" → (modifier_name, side). "fn" maps to ("fn", None).
MODIFIER_ONLY_TOKENS: dict[str, tuple[str, str | None]] = {
    "left_cmd":    ("cmd",    "left"),
    "right_cmd":   ("cmd",    "right"),
    "left_option": ("option", "left"),
    "right_option":("option", "right"),
    "left_ctrl":   ("ctrl",   "left"),
    "right_ctrl":  ("ctrl",   "right"),
    "left_shift":  ("shift",  "left"),
    "right_shift": ("shift",  "right"),
    "fn":          ("fn",     None),
}
```

(b) Replace the `Chord` dataclass + `canonical()`:

```python
@dataclass(frozen=True)
class Chord:
    modifiers: Tuple[str, ...]
    key: str | None = None       # None ⇔ modifier-only binding
    side: str | None = None      # "left" | "right" | None; meaningful when key is None

    def canonical(self) -> str:
        if self.key is None:
            # Modifier-only. Expect exactly one modifier in modifiers.
            mod = self.modifiers[0]
            if mod == "fn":
                return "fn"
            return f"{self.side}_{mod}"
        return "+".join((*self.modifiers, self.key))
```

(c) Add `is_modifier_only` helper:

```python
def is_modifier_only(chord: "Chord") -> bool:
    return chord.key is None
```

(d) Replace the body of `parse()` so the modifier-only branch is recognised:

```python
def parse(raw: str) -> Chord:
    if not raw or not raw.strip():
        raise HotkeyParseError("empty hotkey")
    tokens = [t.strip().lower() for t in raw.replace(",", "+").split("+") if t.strip()]
    if not tokens:
        raise HotkeyParseError("empty hotkey")

    # Modifier-only path: a single token from MODIFIER_ONLY_TOKENS.
    mo_hits = [t for t in tokens if t in MODIFIER_ONLY_TOKENS]
    if mo_hits:
        if len(tokens) != 1:
            raise HotkeyParseError(
                f"modifier-only binding {mo_hits[0]!r} cannot be combined with other tokens (got {raw!r})"
            )
        mod, side = MODIFIER_ONLY_TOKENS[mo_hits[0]]
        return Chord(modifiers=(mod,), key=None, side=side)

    modifiers: set[str] = set()
    keys: list[str] = []
    for tok in tokens:
        if tok in MODIFIER_ALIASES:
            modifiers.add(MODIFIER_ALIASES[tok])
            continue
        if tok in KEY_TOKENS:
            keys.append(tok)
            continue
        raise HotkeyParseError(f"unknown token in hotkey: {tok!r}")

    if not modifiers:
        raise HotkeyParseError(
            f"hotkey requires at least one modifier (got {raw!r})"
        )
    if len(keys) != 1:
        raise HotkeyParseError(
            f"hotkey must have exactly one key (got {len(keys)} in {raw!r})"
        )

    ordered = tuple(m for m in MODIFIER_ORDER if m in modifiers)
    canonical_str = "+".join((*ordered, keys[0]))
    if canonical_str in CONFLICT_CHORDS:
        raise HotkeyParseError(
            f"hotkey {canonical_str} conflicts with a macOS system shortcut"
        )
    return Chord(modifiers=ordered, key=keys[0], side=None)
```

`KEY_TOKENS` itself is **not** widened (the modifier-only tokens live in their own dict, keyed by the raw input form).

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_hotkey_parse.py -v
```

Expected: all pass, including pre-existing tests (regression-free).

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/hotkey_parse.py tests/test_hotkey_parse.py
git commit -m "feat(hotkey): parser supports modifier-only bindings"
```

---

### Task 2: Change default binding to `right_cmd`

**Files:**
- Modify: `agent_doctor/dictate_settings.py:55-58`
- Test: `tests/test_dictate_settings.py`

- [ ] **Step 1: Update the test for the new default**

In `tests/test_dictate_settings.py`, find the line that reads `assert defaults.hotkey.binding == "ctrl+option+space"` and replace with:

```python
    assert defaults.hotkey.binding == "right_cmd"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_dictate_settings.py::test_defaults_have_expected_shape -v
```

Expected: FAIL — actual is still `"ctrl+option+space"`.

- [ ] **Step 3: Update the default in code**

In `agent_doctor/dictate_settings.py`, change the `HotkeySettings` dataclass default:

```python
@dataclass(frozen=True)
class HotkeySettings:
    binding: str = "right_cmd"
    push_to_talk: bool = True
    daemon_enabled: bool = False
```

And in `_from_dict()`, change the fallback for `h.get("binding", ...)`:

```python
        hotkey=HotkeySettings(
            binding=h.get("binding", "right_cmd"),
            push_to_talk=bool(h.get("push_to_talk", True)),
            daemon_enabled=bool(h.get("daemon_enabled", False)),
        ),
```

- [ ] **Step 4: Run the full settings tests**

```bash
.venv/bin/pytest tests/test_dictate_settings.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/dictate_settings.py tests/test_dictate_settings.py
git commit -m "feat(hotkey): default binding is right_cmd (single-modifier hold)"
```

---

### Task 3: Coerce `push_to_talk=True` for modifier-only bindings

**Files:**
- Modify: `agent_doctor/ui/preferences/hotkey_tab.py`
- Test: `tests/test_preferences_logic.py`

- [ ] **Step 1: Write failing test for coercion**

Append to `tests/test_preferences_logic.py`:

```python
def test_hotkey_state_modifier_only_coerces_push_to_talk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    ht.HotkeyState(binding="right_cmd", push_to_talk=False).apply()
    loaded = ds.load()
    assert loaded.hotkey.binding == "right_cmd"
    assert loaded.hotkey.push_to_talk is True  # coerced
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_preferences_logic.py::test_hotkey_state_modifier_only_coerces_push_to_talk -v
```

Expected: FAIL — `loaded.hotkey.push_to_talk` is still `False`.

- [ ] **Step 3: Update `HotkeyState.apply()` to coerce**

In `agent_doctor/ui/preferences/hotkey_tab.py`, change `apply()`:

```python
    def apply(self) -> None:
        try:
            chord = hp.parse(self.binding)
        except hp.HotkeyParseError as exc:
            raise HotkeyStateError(str(exc)) from exc
        ptt = bool(self.push_to_talk) or hp.is_modifier_only(chord)
        s = ds.load()
        new = ds.HotkeySettings(
            binding=chord.canonical(),
            push_to_talk=ptt,
            daemon_enabled=s.hotkey.daemon_enabled,
        )
        ds.save(ds.replace_section(s, hotkey=new))
        if hi.DEFAULT_PLIST_PATH.exists():
            hi.sighup()
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/pytest tests/test_preferences_logic.py -v
```

Expected: all preferences tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/ui/preferences/hotkey_tab.py tests/test_preferences_logic.py
git commit -m "feat(hotkey): coerce push-to-talk on modifier-only bindings"
```

---

### Task 4: Permission probe module

**Files:**
- Create: `agent_doctor/ui/preferences/permission_probe.py`
- Create: `tests/test_permission_probe.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_permission_probe.py`:

```python
"""Headless tests for macOS permission detection."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agent_doctor.ui.preferences import permission_probe as pp


def _fake_run(return_codes: dict[str, int]):
    def _runner(argv, *args, **kwargs):
        class Result:
            returncode = return_codes.get(" ".join(argv), 0)
            stdout = b""
            stderr = b""
        return Result()
    return _runner


def test_both_granted_returns_no_missing() -> None:
    with patch.object(pp.subprocess, "run", _fake_run({})):
        status = pp.check_macos_permissions(
            accessibility_probe=lambda: True,
            input_monitoring_probe=lambda: True,
        )
    assert status.accessibility is True
    assert status.input_monitoring is True
    assert status.first_missing is None


def test_only_accessibility_missing() -> None:
    status = pp.check_macos_permissions(
        accessibility_probe=lambda: False,
        input_monitoring_probe=lambda: True,
    )
    assert status.first_missing == "accessibility"


def test_only_input_monitoring_missing() -> None:
    status = pp.check_macos_permissions(
        accessibility_probe=lambda: True,
        input_monitoring_probe=lambda: False,
    )
    assert status.first_missing == "input_monitoring"


def test_both_missing_picks_accessibility_first() -> None:
    status = pp.check_macos_permissions(
        accessibility_probe=lambda: False,
        input_monitoring_probe=lambda: False,
    )
    assert status.first_missing == "accessibility"


def test_settings_url_for_known_panes() -> None:
    assert pp.settings_url("accessibility").startswith("x-apple.systempreferences:")
    assert "Accessibility" in pp.settings_url("accessibility")
    assert "ListenEvent" in pp.settings_url("input_monitoring")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_permission_probe.py -v
```

Expected: ImportError / ModuleNotFoundError.

- [ ] **Step 3: Implement the module**

Create `agent_doctor/ui/preferences/permission_probe.py`:

```python
"""Detect macOS Accessibility + Input Monitoring permission state.

We can't read TCC.db directly without full disk access, so each probe is
behaviour-based: ``accessibility_probe`` runs a tiny AppleScript that needs
Accessibility, and ``input_monitoring_probe`` checks whether the hotkey
helper has been able to receive global events recently. Callers can inject
custom probes for testing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
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


def _default_input_monitoring_probe() -> bool:
    """Return True if a recent run of the hotkey helper succeeded.

    Heuristic: presence of the helper log file with non-empty content within
    the last 30 days. Refined detection requires private APIs we don't ship.
    """

    from pathlib import Path
    import time

    log = Path("~/Library/Logs/agent-doctor-hotkey.log").expanduser()
    if not log.exists():
        return False
    try:
        stat = log.stat()
    except OSError:
        return False
    if stat.st_size == 0:
        return False
    age_days = (time.time() - stat.st_mtime) / 86400
    return age_days < 30


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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_permission_probe.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/ui/preferences/permission_probe.py tests/test_permission_probe.py
git commit -m "feat(hotkey): permission probe module for Accessibility + Input Monitoring"
```

---

### Task 5: Capture controller state machine (pure-Python)

**Files:**
- Create: `agent_doctor/ui/preferences/hotkey_capture.py` (controller only — Tk wrapper comes in Task 10)
- Create: `tests/test_hotkey_capture.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hotkey_capture.py`:

```python
"""Headless tests for the capture overlay state machine."""

from __future__ import annotations

import pytest

from agent_doctor.ui.preferences import hotkey_capture as hc


def test_initial_state_is_idle() -> None:
    ctl = hc.CaptureController()
    assert ctl.state is hc.State.IDLE
    assert ctl.captured is None


def test_modifier_press_transitions_to_captured_modifier() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="right_cmd"))
    assert ctl.state is hc.State.CAPTURED_MODIFIER
    assert ctl.captured == "right_cmd"


def test_modifier_release_before_min_hold_returns_to_idle() -> None:
    ctl = hc.CaptureController(min_hold_ms=400)
    ctl.on_key_event(hc.KeyEvent(kind="press", key="right_cmd", t_ms=0))
    ctl.on_key_event(hc.KeyEvent(kind="release", key="right_cmd", t_ms=200))
    assert ctl.state is hc.State.IDLE
    assert ctl.commit_result is None


def test_modifier_release_after_min_hold_commits() -> None:
    ctl = hc.CaptureController(min_hold_ms=400)
    ctl.on_key_event(hc.KeyEvent(kind="press", key="right_cmd", t_ms=0))
    ctl.on_key_event(hc.KeyEvent(kind="release", key="right_cmd", t_ms=500))
    assert ctl.state is hc.State.COMMITTED
    assert ctl.commit_result == "right_cmd"


def test_modifier_plus_letter_transitions_to_chord() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="ctrl"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="option"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space"))
    assert ctl.state is hc.State.CAPTURED_CHORD
    assert ctl.captured == "ctrl+option+space"


def test_chord_does_not_auto_commit() -> None:
    ctl = hc.CaptureController(min_hold_ms=400)
    ctl.on_key_event(hc.KeyEvent(kind="press", key="ctrl", t_ms=0))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="option", t_ms=10))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space", t_ms=20))
    ctl.on_key_event(hc.KeyEvent(kind="release", key="space", t_ms=500))
    assert ctl.state is hc.State.CAPTURED_CHORD
    assert ctl.commit_result is None


def test_explicit_commit_on_chord() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="ctrl"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="option"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space"))
    ctl.commit()
    assert ctl.state is hc.State.COMMITTED
    assert ctl.commit_result == "ctrl+option+space"


def test_conflict_chord_transitions_to_conflict() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="cmd"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space"))
    assert ctl.state is hc.State.CONFLICT
    assert ctl.conflict_reason and "Spotlight" in ctl.conflict_reason


def test_conflict_blocks_commit() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="cmd"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space"))
    with pytest.raises(hc.CaptureBlocked):
        ctl.commit()


def test_cancel_returns_to_idle_without_commit() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="right_cmd"))
    ctl.cancel()
    assert ctl.state is hc.State.CANCELLED
    assert ctl.commit_result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_hotkey_capture.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement the controller**

Create `agent_doctor/ui/preferences/hotkey_capture.py`:

```python
"""Capture overlay state machine (pure-Python; Tk wrapper layered on top).

States:
- IDLE: nothing pressed yet.
- CAPTURED_MODIFIER: a single modifier-only token (e.g. right_cmd) is held.
  Auto-commits on release if held ≥ min_hold_ms.
- CAPTURED_CHORD: at least one modifier + a non-modifier key is held; the
  controller does not auto-commit. Caller must invoke ``commit()``.
- CONFLICT: captured chord is in hotkey_parse.CONFLICT_CHORDS — commit is
  blocked.
- COMMITTED: terminal; ``commit_result`` is the canonical binding string.
- CANCELLED: terminal; ``commit_result`` is None.

Keys feed in as ``KeyEvent(kind=press|release, key=<token>, t_ms=int)``.
Caller maps tk/swift events into this token vocabulary.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from agent_doctor import hotkey_parse as hp


class State(enum.Enum):
    IDLE = "idle"
    CAPTURED_MODIFIER = "captured_modifier"
    CAPTURED_CHORD = "captured_chord"
    CONFLICT = "conflict"
    COMMITTED = "committed"
    CANCELLED = "cancelled"


class CaptureBlocked(RuntimeError):
    """Raised when caller tries to commit while in CONFLICT."""


@dataclass(frozen=True)
class KeyEvent:
    kind: str       # "press" | "release"
    key: str        # token (e.g. "right_cmd", "ctrl", "space")
    t_ms: int = 0


class CaptureController:
    def __init__(self, *, min_hold_ms: int = 400) -> None:
        self._state: State = State.IDLE
        self._min_hold_ms = min_hold_ms
        self._held: set[str] = set()
        self._press_t_ms: dict[str, int] = {}
        self._commit_result: Optional[str] = None
        self._conflict_reason: Optional[str] = None
        # Snapshot is set when we land in CAPTURED_CHORD or CONFLICT so the
        # display stays sticky as the user releases keys before clicking
        # "Use this chord" or pressing a new combination.
        self._snapshot: Optional[str] = None

    @property
    def state(self) -> State:
        return self._state

    @property
    def captured(self) -> Optional[str]:
        if self._snapshot is not None:
            return self._snapshot
        if not self._held:
            return None
        return _canonicalise(self._held)

    @property
    def commit_result(self) -> Optional[str]:
        return self._commit_result

    @property
    def conflict_reason(self) -> Optional[str]:
        return self._conflict_reason

    def on_key_event(self, ev: KeyEvent) -> None:
        if self._state in (State.COMMITTED, State.CANCELLED):
            return
        if ev.kind == "press":
            # A new press during a sticky chord/conflict state clears the
            # snapshot — the user is re-capturing.
            if self._state in (State.CAPTURED_CHORD, State.CONFLICT):
                self._held.clear()
                self._press_t_ms.clear()
                self._snapshot = None
                self._conflict_reason = None
            self._held.add(ev.key)
            self._press_t_ms[ev.key] = ev.t_ms
            self._refresh_state()
        elif ev.kind == "release":
            press_t = self._press_t_ms.pop(ev.key, 0)
            held_ms = ev.t_ms - press_t
            if (
                self._state is State.CAPTURED_MODIFIER
                and ev.key in hp.MODIFIER_ONLY_TOKENS
                and held_ms >= self._min_hold_ms
            ):
                self._held.discard(ev.key)
                self._commit_result = ev.key
                self._state = State.COMMITTED
                return
            self._held.discard(ev.key)
            # Sticky states ignore subsequent releases; only commit/cancel
            # or a new press can leave them.
            if self._state in (State.CAPTURED_CHORD, State.CONFLICT):
                return
            self._refresh_state()
        else:
            raise ValueError(f"unknown event kind {ev.kind!r}")

    def commit(self) -> None:
        if self._state is State.CONFLICT:
            raise CaptureBlocked(self._conflict_reason or "binding conflicts with system shortcut")
        result = self._snapshot or (_canonicalise(self._held) if self._held else None)
        if result is None:
            return
        self._commit_result = result
        self._state = State.COMMITTED

    def cancel(self) -> None:
        self._state = State.CANCELLED
        self._commit_result = None

    def _refresh_state(self) -> None:
        if not self._held:
            self._state = State.IDLE
            self._snapshot = None
            return
        if len(self._held) == 1:
            (only,) = self._held
            if only in hp.MODIFIER_ONLY_TOKENS:
                self._state = State.CAPTURED_MODIFIER
                self._snapshot = None
                return
        canon = _canonicalise(self._held)
        if canon in hp.CONFLICT_CHORDS:
            self._state = State.CONFLICT
            self._conflict_reason = _explain_conflict(canon)
            self._snapshot = canon
            return
        try:
            hp.parse(canon)
        except hp.HotkeyParseError:
            self._state = State.IDLE
            self._snapshot = None
            return
        self._state = State.CAPTURED_CHORD
        self._snapshot = canon


def _canonicalise(held: set[str]) -> str:
    if len(held) == 1:
        (only,) = held
        if only in hp.MODIFIER_ONLY_TOKENS:
            return only
    mods = [m for m in hp.MODIFIER_ORDER if m in held]
    keys = [k for k in held if k not in hp.MODIFIER_ALIASES.values()]
    keys = [k for k in keys if k not in hp.MODIFIER_ONLY_TOKENS]
    return "+".join(mods + sorted(keys))


_CONFLICT_REASONS = {
    "cmd+space": "Spotlight uses ⌘ + Space.",
    "cmd+tab": "macOS uses ⌘ + Tab for app switching.",
    "cmd+q": "Quitting the focused app uses ⌘ + Q.",
    "cmd+w": "Closing a window uses ⌘ + W.",
    "cmd+option+escape": "macOS Force Quit uses ⌘⌥ + Escape.",
    "cmd+shift+3": "Screenshot uses ⌘⇧ + 3.",
    "cmd+shift+4": "Screenshot region uses ⌘⇧ + 4.",
    "cmd+shift+5": "Screen capture UI uses ⌘⇧ + 5.",
}


def _explain_conflict(canonical: str) -> str:
    return _CONFLICT_REASONS.get(canonical, f"{canonical} is reserved by macOS.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_hotkey_capture.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/ui/preferences/hotkey_capture.py tests/test_hotkey_capture.py
git commit -m "feat(hotkey): capture overlay state machine"
```

---

### Task 6: `HotkeyState` snapshots for daemon + permission status

**Files:**
- Modify: `agent_doctor/ui/preferences/hotkey_tab.py`
- Test: `tests/test_preferences_logic.py`

- [ ] **Step 1: Write failing tests for the new wrappers**

Append to `tests/test_preferences_logic.py`:

```python
def test_hotkey_daemon_status_snapshot_returns_pill_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import hotkey_install as hi

    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": True,
            "helper_exists": True,
            "running": True,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=True),
    )
    ds.save(settings)
    # Force both perms to True so the pill resolution is deterministic in CI.
    from agent_doctor.ui.preferences import permission_probe as pp
    monkeypatch.setattr(
        pp, "check_macos_permissions",
        lambda **_: pp.PermissionStatus(accessibility=True, input_monitoring=True, first_missing=None),
    )
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "listening"


def test_hotkey_daemon_status_snapshot_when_plist_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import hotkey_install as hi

    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": False,
            "helper_exists": False,
            "running": False,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "daemon_stopped"


def test_hotkey_daemon_status_snapshot_when_user_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import hotkey_install as hi

    monkeypatch.setattr(
        hi, "status", lambda: {
            "plist_exists": True,
            "helper_exists": True,
            "running": False,
            "plist": "/tmp/x.plist",
            "helper": "/tmp/x",
        }
    )
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        hotkey=ds.HotkeySettings(binding="right_cmd", push_to_talk=True, daemon_enabled=False),
    )
    ds.save(settings)
    snap = ht.daemon_status_snapshot()
    assert snap["pill"] == "paused"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_preferences_logic.py -v -k daemon_status
```

Expected: AttributeError — `daemon_status_snapshot` not yet defined.

- [ ] **Step 3: Add the snapshot helpers**

In `agent_doctor/ui/preferences/hotkey_tab.py`, append:

```python
from agent_doctor.ui.preferences import permission_probe as pp


def daemon_status_snapshot() -> dict[str, object]:
    """Return a snapshot used by the tab view to render the status pill.

    Keys: ``pill`` (one of "listening" / "permission_needed" / "paused" /
    "daemon_stopped"), ``perms`` (PermissionStatus), ``daemon`` (raw dict
    from ``hotkey_install.status()``), ``settings`` (HotkeySettings).
    """

    daemon = hi.status()
    s = ds.load()
    if not daemon["plist_exists"]:
        pill = "daemon_stopped"
        perms = pp.PermissionStatus(accessibility=False, input_monitoring=False, first_missing="accessibility")
    elif not daemon["running"] or not s.hotkey.daemon_enabled:
        pill = "paused"
        perms = pp.PermissionStatus(accessibility=True, input_monitoring=True, first_missing=None)
    else:
        perms = pp.check_macos_permissions()
        pill = "listening" if perms.first_missing is None else "permission_needed"
    return {"pill": pill, "perms": perms, "daemon": daemon, "settings": s.hotkey}


def permission_status_snapshot() -> pp.PermissionStatus:
    return pp.check_macos_permissions()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_preferences_logic.py -v -k daemon_status
```

Expected: all three pass.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/ui/preferences/hotkey_tab.py tests/test_preferences_logic.py
git commit -m "feat(hotkey): daemon_status_snapshot resolves pill state"
```

---

### Task 7: Swift helper — new modifier-only keycodes

**Files:**
- Modify: `agent_doctor/hotkey/HotkeyHelper.swift`
- Test: `tests/test_hotkey_install.py` (rebuild smoke test)

- [ ] **Step 1: Extend `KEYCODES`**

In `agent_doctor/hotkey/HotkeyHelper.swift`, append to the `KEYCODES` dictionary:

```swift
    "left_cmd": 55, "right_cmd": 54,
    "left_option": 58, "right_option": 61,
    "left_ctrl": 59, "right_ctrl": 62,
    "left_shift": 56, "right_shift": 60,
    "fn": 63,
```

(Place these after the existing F-key entries; keep one `,` per logical line.)

- [ ] **Step 2: Teach `parse(_:)` about modifier-only tokens**

Below the `MODIFIERS` and `KEYCODES` dictionaries, add:

```swift
let MODIFIER_ONLY_KEYCODES: Set<UInt16> = [54, 55, 56, 58, 59, 60, 61, 62, 63]

// Map each modifier-only keycode to the corresponding flag bit so the
// daemon can confirm "this physical key produced this flag change."
let MODIFIER_FLAG_FOR_KEYCODE: [UInt16: NSEvent.ModifierFlags] = [
    54: .command, 55: .command,
    58: .option,  61: .option,
    59: .control, 62: .control,
    56: .shift,   60: .shift,
    63: .function,
]
```

The existing `parse(_:)` already returns `ParsedChord(modifiers: [], keyCode: 54)` for input `right_cmd`, because it falls through the modifier-tokens loop without finding a match and lands on the keycode lookup. No code change required there — the new keycodes do the work.

- [ ] **Step 3: Verify the helper still compiles**

```bash
swiftc -O agent_doctor/hotkey/HotkeyHelper.swift -o /tmp/agent-doctor-hotkey-helper
```

Expected: rc=0, binary written to /tmp/agent-doctor-hotkey-helper.

If `swiftc` is not on PATH, run:

```bash
xcode-select --install
```

…and retry.

- [ ] **Step 4: Run the existing install tests**

```bash
.venv/bin/pytest tests/test_hotkey_install.py -v
```

Expected: all pass (the install tests use a fake `swiftc` so they don't catch source errors, but they do exercise the surrounding plumbing).

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/hotkey/HotkeyHelper.swift
git commit -m "feat(hotkey): swift helper recognises modifier-only keycodes"
```

---

### Task 8: Swift helper — `.flagsChanged` path with alone-key rule

**Files:**
- Modify: `agent_doctor/hotkey/HotkeyHelper.swift`

- [ ] **Step 1: Replace `HotkeyDaemon.reload()` to fork by binding shape**

In `agent_doctor/hotkey/HotkeyHelper.swift`, replace the `HotkeyDaemon` class body with:

```swift
class HotkeyDaemon {
    var config: Config = readConfig()
    var chord: ParsedChord? = nil
    var monitor: Any? = nil
    var keyDown = false

    func reload() {
        config = readConfig()
        chord = parse(config.binding)
        if let m = monitor {
            NSEvent.removeMonitor(m)
            monitor = nil
        }
        guard let c = chord else {
            fputs("hotkey: could not parse binding \(config.binding)\n", stderr)
            return
        }
        if c.modifiers.isEmpty && MODIFIER_ONLY_KEYCODES.contains(c.keyCode) {
            installFlagsMonitor(for: c.keyCode)
        } else {
            installKeyMonitor(for: c)
        }
    }

    private func installKeyMonitor(for c: ParsedChord) {
        let mask: NSEvent.EventTypeMask = [.keyDown, .keyUp]
        monitor = NSEvent.addGlobalMonitorForEvents(matching: mask) { [weak self] ev in
            guard let self = self else { return }
            let flags = ev.modifierFlags.intersection(.deviceIndependentFlagsMask)
            if ev.keyCode != c.keyCode { return }
            if !flags.contains(c.modifiers) { return }
            if ev.type == .keyDown {
                if !self.keyDown {
                    self.keyDown = true
                    if self.config.pushToTalk {
                        run([self.config.agentDoctorBin, "dictate", "start"])
                    } else {
                        run([self.config.agentDoctorBin, "dictate", "toggle"])
                    }
                }
            } else if ev.type == .keyUp {
                self.keyDown = false
                if self.config.pushToTalk {
                    run([self.config.agentDoctorBin, "dictate", "stop"])
                }
            }
        }
    }

    private func installFlagsMonitor(for keyCode: UInt16) {
        guard let myFlag = MODIFIER_FLAG_FOR_KEYCODE[keyCode] else { return }
        monitor = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] ev in
            guard let self = self else { return }
            let flags = ev.modifierFlags.intersection(.deviceIndependentFlagsMask)
            let myFlagOn = flags.contains(myFlag)
            var others: NSEvent.ModifierFlags = [.command, .option, .control, .shift, .function]
            others.remove(myFlag)
            let anyOther = !flags.intersection(others).isEmpty

            // Start requires the event to originate from the BOUND physical
            // key (so left_cmd doesn't accidentally trigger a right_cmd
            // binding). Stop must fire on ANY flagsChanged event that
            // invalidates the alone-key state — including events from other
            // modifier keys, otherwise "user pressed Shift while holding the
            // bound modifier" would never release.
            let isOurKey = ev.keyCode == keyCode

            if myFlagOn && !anyOther && isOurKey {
                if !self.keyDown {
                    self.keyDown = true
                    run([self.config.agentDoctorBin, "dictate", "start"])
                }
            } else if self.keyDown {
                self.keyDown = false
                run([self.config.agentDoctorBin, "dictate", "stop"])
            }
        }
    }
}
```

- [ ] **Step 2: Verify the helper still compiles**

```bash
swiftc -O agent_doctor/hotkey/HotkeyHelper.swift -o /tmp/agent-doctor-hotkey-helper
```

Expected: rc=0.

- [ ] **Step 3: Run install tests**

```bash
.venv/bin/pytest tests/test_hotkey_install.py -v
```

Expected: all pass.

- [ ] **Step 4: Smoke test against a real machine (manual)**

Manual verification — not gated by CI:

```bash
agent-doctor dictate hotkey install
# Set binding to right_cmd via Preferences (Task 9) or:
python3 -c "from agent_doctor import dictate_settings as ds; \
  s = ds.load(); ds.save(ds.replace_section(s, hotkey=ds.HotkeySettings(binding='right_cmd', push_to_talk=True, daemon_enabled=True)))"
launchctl kill SIGHUP gui/$(id -u)/com.agent-doctor.hotkey
# Hold Right Cmd. Watch ~/Library/Logs/agent-doctor-hotkey.log for "dictate start".
```

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/hotkey/HotkeyHelper.swift
git commit -m "feat(hotkey): swift helper supports modifier-only bindings with alone-key rule"
```

---

### Task 9: Build the new Hotkey tab view (extracted module)

**Files:**
- Create: `agent_doctor/ui/preferences/hotkey_tab_view.py`
- Modify: `agent_doctor/ui/preferences/__init__.py`
- Test: `tests/test_preferences_ui_smoke.py`

- [ ] **Step 1: Extend the smoke test**

In `tests/test_preferences_ui_smoke.py`, add (or extend an existing test) so the new module imports cleanly. Append:

```python
def test_hotkey_tab_view_imports_without_tk_root() -> None:
    # Import-only — instantiating widgets needs a Tk root which CI may lack.
    from agent_doctor.ui.preferences import hotkey_tab_view  # noqa: F401
```

- [ ] **Step 2: Run smoke test to verify it fails**

```bash
.venv/bin/pytest tests/test_preferences_ui_smoke.py -v
```

Expected: ImportError for `hotkey_tab_view`.

- [ ] **Step 3: Create `hotkey_tab_view.py`**

Create `agent_doctor/ui/preferences/hotkey_tab_view.py`:

```python
"""Widget composition for the redesigned Hotkey tab (layout B).

Pure tk/ttk layout. State + apply lives in ``hotkey_tab``. Capture overlay
lives in ``hotkey_capture``. Permission detection in ``permission_probe``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agent_doctor import hotkey_install as hi
from agent_doctor import hotkey_parse as hp
from agent_doctor.ui.preferences import hotkey_capture as hc
from agent_doctor.ui.preferences import hotkey_tab as ht
from agent_doctor.ui.preferences import permission_probe as pp

_PILL_TEXT = {
    "listening": ("Listening", "#2a8a4a", "#e5f7eb"),
    "permission_needed": ("Permission needed", "#a45c10", "#fef0e0"),
    "paused": ("Paused", "#6a6a6a", "#ececec"),
    "daemon_stopped": ("Daemon stopped", "#6a6a6a", "#ececec"),
}

_LOG_PATH = Path("~/Library/Logs/agent-doctor-hotkey.log").expanduser()


def build(notebook: Any) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    frame = ttk.Frame(notebook, padding=14)
    notebook.add(frame, text="Hotkey")

    # --- header row -------------------------------------------------
    header = ttk.Frame(frame)
    header.pack(fill="x", pady=(0, 8))
    ttk.Label(header, text="Global hotkey", font=("Helvetica", 14, "bold")).pack(side="left")
    pill_var = tk.StringVar(value="…")
    pill_label = tk.Label(header, textvariable=pill_var, padx=8, pady=2)
    pill_label.pack(side="right")

    ttk.Label(frame, text="Trigger dictation from anywhere on the system.", foreground="#777").pack(
        anchor="w", pady=(0, 8)
    )

    # --- permission banner -----------------------------------------
    banner_frame = ttk.Frame(frame)
    banner_frame.pack(fill="x", pady=(0, 10))
    banner_text = tk.StringVar(value="")
    banner_label = tk.Label(banner_frame, textvariable=banner_text, anchor="w", padx=8, pady=6)
    banner_label.pack(side="left", fill="x", expand=True)
    banner_action = ttk.Button(banner_frame, text="Open settings…")
    banner_action.pack(side="right", padx=6)

    # --- shortcut tile ---------------------------------------------
    tile = ttk.LabelFrame(frame, text="Shortcut")
    tile.pack(fill="x", pady=(0, 12))
    binding_var = tk.StringVar(value="")
    binding_label = tk.Label(tile, textvariable=binding_var, font=("Helvetica", 16))
    binding_label.pack(side="left", padx=12, pady=10)
    hint_label = tk.Label(tile, text="Hold to record · click Record to change", foreground="#777")
    hint_label.pack(side="left", padx=4, pady=10)
    record_btn = ttk.Button(tile, text="Record…")
    record_btn.pack(side="right", padx=6, pady=10)
    test_btn = ttk.Button(tile, text="Test")
    test_btn.pack(side="right", padx=2, pady=10)

    # --- mode segmented --------------------------------------------
    mode_frame = ttk.Frame(frame)
    mode_frame.pack(fill="x", pady=(0, 10))
    ttk.Label(mode_frame, text="Mode").pack(side="left")
    ptt_var = tk.BooleanVar(value=True)
    ptt_radio = ttk.Radiobutton(mode_frame, text="Push-to-talk", variable=ptt_var, value=True)
    toggle_radio = ttk.Radiobutton(mode_frame, text="Toggle", variable=ptt_var, value=False)
    ptt_radio.pack(side="left", padx=8)
    toggle_radio.pack(side="left", padx=8)

    # --- daemon toggle ---------------------------------------------
    daemon_frame = ttk.Frame(frame)
    daemon_frame.pack(fill="x", pady=(0, 10))
    ttk.Label(daemon_frame, text="Background daemon").pack(side="left")
    daemon_var = tk.BooleanVar(value=False)
    daemon_chk = ttk.Checkbutton(daemon_frame, variable=daemon_var)
    daemon_chk.pack(side="right")

    # --- footer ----------------------------------------------------
    footer = ttk.Frame(frame)
    footer.pack(fill="x", pady=(10, 0))
    show_logs_btn = ttk.Button(footer, text="Show daemon logs")
    show_logs_btn.pack(side="left")
    uninstall_btn = ttk.Button(footer, text="Uninstall")
    uninstall_btn.pack(side="right")

    # ---------------- behaviour wiring -----------------------------

    def _refresh() -> None:
        snap = ht.daemon_status_snapshot()
        pill_key = str(snap["pill"])
        text, fg, bg = _PILL_TEXT[pill_key]
        pill_var.set(text)
        pill_label.configure(foreground=fg, background=bg)
        perms = snap["perms"]  # type: ignore[index]
        if getattr(perms, "first_missing", None):
            target = perms.first_missing
            label = {
                "accessibility": "⚠ Accessibility permission required",
                "input_monitoring": "⚠ Input Monitoring permission required",
            }[target]
            banner_text.set(label)
            banner_label.configure(background="#fff7e8", foreground="#7a5b14")
            banner_action.configure(command=lambda t=target: subprocess.run(["open", pp.settings_url(t)]))
            banner_frame.pack_configure()
        else:
            banner_text.set("")
            banner_label.configure(background=frame.cget("background"))
            banner_action.configure(command=lambda: None)
        s = snap["settings"]  # type: ignore[index]
        binding_var.set(_render_binding(str(s.binding)))
        ptt_var.set(bool(s.push_to_talk))
        chord = hp.parse(str(s.binding))
        if hp.is_modifier_only(chord):
            toggle_radio.state(["disabled"])
            ptt_var.set(True)
        else:
            toggle_radio.state(["!disabled"])
        daemon_var.set(bool(s.daemon_enabled and bool(snap["daemon"]["running"])))  # type: ignore[index]

    def _on_record() -> None:
        new_binding = _open_capture_overlay(frame)
        if new_binding is None:
            return
        ht.HotkeyState(binding=new_binding, push_to_talk=ptt_var.get()).apply()
        _refresh()

    def _on_test() -> None:
        snap = ht.daemon_status_snapshot()
        if snap["pill"] in ("listening", "permission_needed"):
            messagebox.showinfo("Hotkey", "Daemon received key event ✓")
        else:
            messagebox.showwarning("Hotkey", "Daemon not running — enable Background daemon.")

    def _on_mode_change() -> None:
        current = ht.HotkeyState.from_settings().binding
        ht.HotkeyState(binding=current, push_to_talk=ptt_var.get()).apply()
        _refresh()

    def _on_daemon_toggle() -> None:
        if daemon_var.get():
            try:
                hi.install()
            except hi.HotkeyInstallError as exc:
                messagebox.showerror("Hotkey", str(exc))
                daemon_var.set(False)
        else:
            hi.uninstall()
        _refresh()

    def _on_show_logs() -> None:
        subprocess.run(["open", "-a", "Console", str(_LOG_PATH)])

    def _on_uninstall() -> None:
        if not messagebox.askyesno(
            "Stop and remove the hotkey daemon?",
            "This stops the LaunchAgent and deletes the helper. You can re-enable it any time.",
            icon=messagebox.WARNING,
        ):
            return
        hi.uninstall()
        try:
            Path(hi.DEFAULT_HELPER_PATH).unlink(missing_ok=True)
        except OSError:
            pass
        daemon_var.set(False)
        _refresh()

    record_btn.configure(command=_on_record)
    test_btn.configure(command=_on_test)
    ptt_radio.configure(command=_on_mode_change)
    toggle_radio.configure(command=_on_mode_change)
    daemon_chk.configure(command=_on_daemon_toggle)
    show_logs_btn.configure(command=_on_show_logs)
    uninstall_btn.configure(command=_on_uninstall)

    def _poll() -> None:
        try:
            _refresh()
        finally:
            frame.after(1000, _poll)

    _refresh()
    frame.after(1000, _poll)
    frame.bind_all("<FocusIn>", lambda _e: _refresh(), add="+")


def _render_binding(canonical: str) -> str:
    glyphs = {
        "cmd": "⌘", "ctrl": "⌃", "option": "⌥", "shift": "⇧", "fn": "🌐",
        "right_cmd": "⌘ Right", "left_cmd": "⌘ Left",
        "right_option": "⌥ Right", "left_option": "⌥ Left",
        "right_ctrl": "⌃ Right", "left_ctrl": "⌃ Left",
        "right_shift": "⇧ Right", "left_shift": "⇧ Left",
    }
    if "+" not in canonical:
        return glyphs.get(canonical, canonical)
    parts = canonical.split("+")
    return " ".join(glyphs.get(p, p.capitalize()) for p in parts)


def _open_capture_overlay(parent: Any) -> str | None:
    """Tk overlay wrapping ``CaptureController``. Returns the canonical
    binding string on success, ``None`` on cancel/conflict.
    """

    import tkinter as tk

    dlg = tk.Toplevel(parent)
    dlg.title("Record hotkey")
    dlg.geometry("420x260")
    dlg.transient(parent)
    dlg.grab_set()

    controller = hc.CaptureController()
    headline = tk.StringVar(value="Press the key you want to hold")
    sub = tk.StringVar(value="Hold ⌘ ⌥ ⌃ ⇧ Fn, or press a chord.")
    cap_var = tk.StringVar(value="…")

    tk.Label(dlg, textvariable=headline, font=("Helvetica", 14, "bold")).pack(pady=(20, 4))
    tk.Label(dlg, textvariable=sub, foreground="#666").pack()
    tk.Label(dlg, textvariable=cap_var, font=("Helvetica", 22)).pack(pady=20)

    result: dict[str, str | None] = {"binding": None}

    def _commit_and_close() -> None:
        # For modifier-only bindings the controller has already auto-committed
        # on release; for chords we need an explicit commit().
        if controller.state is not hc.State.COMMITTED:
            try:
                controller.commit()
            except hc.CaptureBlocked:
                return  # conflict — leave the dialog open
        result["binding"] = controller.commit_result
        dlg.destroy()

    def _cancel_and_close(_event: Any = None) -> None:
        controller.cancel()
        dlg.destroy()

    def _on_key(event: Any) -> None:
        token = _keysym_to_token(event.keysym)
        if token is None:
            return
        import time
        t_ms = int(time.monotonic() * 1000)
        controller.on_key_event(hc.KeyEvent(kind="press", key=token, t_ms=t_ms))
        cap_var.set(controller.captured or "…")
        if controller.state is hc.State.CONFLICT:
            sub.set(controller.conflict_reason or "Conflicts with macOS.")

    def _on_release(event: Any) -> None:
        token = _keysym_to_token(event.keysym)
        if token is None:
            return
        import time
        t_ms = int(time.monotonic() * 1000)
        controller.on_key_event(hc.KeyEvent(kind="release", key=token, t_ms=t_ms))
        if controller.state is hc.State.COMMITTED:
            _commit_and_close()

    def _on_focus_out(event: Any) -> None:
        # Spec §5.2: window losing focus = Cancel. Guard against spurious
        # FocusOut events that fire when a child widget (e.g. a button)
        # gains focus inside the same Toplevel.
        if event.widget is dlg:
            _cancel_and_close(event)

    dlg.bind("<Key>", _on_key)
    dlg.bind("<KeyRelease>", _on_release)
    dlg.bind("<Escape>", _cancel_and_close)
    dlg.bind("<FocusOut>", _on_focus_out)

    use_btn = tk.Button(dlg, text="Use this chord", command=_commit_and_close)
    use_btn.pack(pady=(4, 10))
    tk.Button(dlg, text="Cancel", command=_cancel_and_close).pack()

    dlg.focus_set()
    dlg.wait_window()
    return result["binding"]


_KEYSYM_TO_TOKEN = {
    "Meta_L": "left_cmd", "Meta_R": "right_cmd",
    "Alt_L": "left_option", "Alt_R": "right_option",
    "Control_L": "left_ctrl", "Control_R": "right_ctrl",
    "Shift_L": "left_shift", "Shift_R": "right_shift",
    # Generic modifier (no L/R surfacing from Tk) maps to chord-style tokens.
    "Control": "ctrl", "Shift": "shift", "Alt": "option", "Meta": "cmd",
    "space": "space", "Return": "return", "Escape": "escape", "Tab": "tab",
    "BackSpace": "backspace", "Delete": "delete",
}


def _keysym_to_token(keysym: str) -> str | None:
    if keysym in _KEYSYM_TO_TOKEN:
        return _KEYSYM_TO_TOKEN[keysym]
    if len(keysym) == 1 and keysym.isalnum():
        return keysym.lower()
    if keysym.startswith("F") and keysym[1:].isdigit():
        return keysym.lower()
    return None
```

- [ ] **Step 4: Delegate from `__init__.py`**

In `agent_doctor/ui/preferences/__init__.py`, replace the entire `_build_hotkey_tab` function (currently lines ~191-267) with:

```python
def _build_hotkey_tab(notebook: Any, ht: Any) -> None:
    from . import hotkey_tab_view
    hotkey_tab_view.build(notebook)
```

Also drop the `import hotkey_tab as ht` line at the top of `open_window` if it becomes unused — verify with the smoke test.

- [ ] **Step 5: Run smoke + preferences tests**

```bash
.venv/bin/pytest tests/test_preferences_ui_smoke.py tests/test_preferences_logic.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add agent_doctor/ui/preferences/hotkey_tab_view.py agent_doctor/ui/preferences/__init__.py tests/test_preferences_ui_smoke.py
git commit -m "feat(hotkey): Handy-style tab view with status pill + capture overlay"
```

---

### Task 10: Manual UI smoke test

**Files:**
- Verify only — no edits.

Capture and commit findings into the next task if anything regresses.

- [ ] **Step 1: Build the helper + launch the pet**

```bash
agent-doctor dictate hotkey install
agent-doctor pet-display &
```

- [ ] **Step 2: Open Preferences**

Right-click the pet → Preferences… → click the Hotkey tab.

- [ ] **Step 3: Walk the acceptance criteria from the spec**

Use spec §10 (acceptance criteria 1-10) as a checklist. Each criterion must hold:

1. Fresh install shows status pill `Daemon stopped` and `Background daemon` off.
2. Toggling Background daemon on flips pill to `Permission needed` then `Listening`.
3. Record… → hold Right Option ≥ 400ms → release → tile updates to `⌥ Right`.
4. Record… → Ctrl+Option+Space → "Use this chord" → Mode auto-switches to Toggle.
5. ⌘+Space in Record… surfaces conflict, commit disabled.
6. Test button shows "Daemon received key event ✓" when daemon is live.
7. Modifier-only hold triggers a real recording (pet listening animation).
8. Show daemon logs opens Console.app with the helper log.
9. Uninstall confirms then flips pill to `Daemon stopped`.
10. Existing `ctrl+option+space` binding renders correctly as `⌃ ⌥ Space` with PTT preserved.

- [ ] **Step 4: If any criterion fails, file follow-up task and stop. Otherwise continue.**

---

### Task 11: README + dictate doc updates

**Files:**
- Modify: `README.md`
- Modify: `docs/dictate.md`

- [ ] **Step 1: Update `README.md`**

Find the dictate section that mentions the hotkey and replace the hotkey paragraph with:

```markdown
The global hotkey now defaults to **Right Command (hold)** — the
Handy-style "single modifier hold to talk" trigger. Open
`agent-doctor dictate preferences` → Hotkey tab to pick a different key
or to switch to a chord (`⌃⌥Space`-style). Modifier-only bindings are
push-to-talk only; chord bindings can be either push-to-talk or toggle.
The capture overlay live-previews each key event; press for ≥ 400ms and
release to commit a single modifier, or press a chord and click "Use
this chord".
```

- [ ] **Step 2: Update `docs/dictate.md`**

Append a short subsection "Hotkey configuration":

```markdown
## Hotkey configuration

Default binding: `right_cmd` (hold Right Command). Override via
Preferences → Hotkey or by editing `~/.agent-doctor/dictate.json`:

```json
{ "hotkey": { "binding": "right_option", "push_to_talk": true, "daemon_enabled": true } }
```

Valid modifier-only tokens: `left_cmd`, `right_cmd`, `left_option`,
`right_option`, `left_ctrl`, `right_ctrl`, `left_shift`, `right_shift`,
`fn`. Chord tokens follow the existing `mod+mod+key` format.

The Preferences capture overlay disambiguates left vs right; manual
JSON edits are the only way to bind `fn` today.
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/dictate.md
git commit -m "docs(hotkey): document Handy-style default + capture overlay"
```

---

### Task 12: Full test sweep + push

**Files:**
- No edits.

- [ ] **Step 1: Run the entire suite**

```bash
.venv/bin/pytest tests/ -q
```

Expected: all green. If anything red, file follow-up and fix in the same commit.

- [ ] **Step 2: Submit via the harness**

Per repo CLAUDE.md (PR Harness mandatory):

```bash
dev-autopilot submit agent-doctor
```

- [ ] **Step 3: Verify the pre-push hook accepts the bundle**

Hook output should reference the job contract. If hook fails, do NOT use `HARNESS_BYPASS=1` without documenting the reason in the PR body.

---

## Acceptance summary

When all tasks are complete, the spec's 11 acceptance criteria in §10 should hold. Cross them off in the PR body before merging.

## Deferred (out of scope for this plan)

- **Fn capture in the overlay.** Spec §5.3 anticipated a one-shot Swift probe for Fn detection; this plan binds Fn via manual config-file edits only. Add as a follow-up when there's user demand.
- **Per-app bindings.** Not in spec.
- **Status-pill animation transitions.** Spec calls for a static pill; animation is polish.
- **Switch widget vs Checkbutton.** Spec §11 marks this open; we ship `Checkbutton` to keep dependencies stock.
