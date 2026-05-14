# Dictate Phase 3 — Pet listening + thinking animations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the pet sprite *listening* during recording and *thinking* during LLM enhancement, with two new procedurally-drawn animations layered on top of the existing pet, without clobbering the autopilot-driven main snapshot.

**Architecture:** A new transient state file (`~/.agent-doctor/pet/pet-transient.json`) is read on every tick of `pet_display`. If present and unexpired, its state overlays the main snapshot. A context manager (`pet_transient.pet_state`) writes the file on enter, deletes it on exit, with a TTL safety net. Two new animation draw helpers (`draw_listening`, `draw_thinking`) in `pet_animations.py` are called from the existing tick loop when the rendered snapshot's state is `listening` or `thinking`. `dictate.run_pipeline` wraps recording / enhancement with the context manager.

**Tech Stack:** Python stdlib, tkinter (lazy-imported). No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-05-14-dictate-handy-parity-design.md` §7.

**Prereq:** Phases 1 and 2 landed.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent_doctor/pet_transient.py` | Create — write/read/clear the transient state file + context manager. |
| `agent_doctor/pet_animations.py` | Create — pure-function `draw_listening` / `draw_thinking` against a canvas-like protocol. |
| `agent_doctor/pet_display.py` | Modify — read transient file on tick; overlay state; call animations for new states; extend accent/label tables. |
| `agent_doctor/dictate.py` | Modify — `run_pipeline` (and the `_dictate_finish` helper in cli) wrap `transcribe` and `enhance_prompt` with `pet_state`. |
| `agent_doctor/cli.py` | Modify — `_dictate_finish` wraps recording/enhance with `pet_state`. |
| `tests/test_pet_transient.py` | Create — write/read/clear/TTL/precedence. |
| `tests/test_pet_animations.py` | Create — fake canvas records create_oval calls; assert pulse + orbit behaviour. |
| `tests/test_pet_overlay.py` | Create — main + transient precedence matrix. |
| `tests/test_dictate.py` | Modify — assert `run_pipeline` enters listening/thinking states. |

---

## Task 1: Create `pet_transient.py` with write/read/clear

**Files:**
- Create: `agent_doctor/pet_transient.py`
- Test: `tests/test_pet_transient.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pet_transient.py`:

```python
"""Tests for the transient pet state overlay file."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_doctor import pet_transient as pt


def test_write_creates_file_with_state_and_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pet-transient.json"
    monkeypatch.setattr(pt, "default_transient_file", lambda: target)
    pt.write_transient("listening", ttl_seconds=12.0, owner="dictate")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["state"] == "listening"
    assert payload["owner"] == "dictate"
    assert payload["expires_at"] - payload["started_at"] == pytest.approx(12.0, rel=1e-6)


def test_read_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pt, "default_transient_file", lambda: tmp_path / "missing.json")
    assert pt.read_transient() is None


def test_read_returns_none_when_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pet-transient.json"
    monkeypatch.setattr(pt, "default_transient_file", lambda: target)
    now = time.time()
    target.write_text(
        json.dumps({"state": "listening", "owner": "dictate", "started_at": now - 100, "expires_at": now - 1})
    )
    assert pt.read_transient() is None


def test_clear_only_removes_when_owner_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pet-transient.json"
    monkeypatch.setattr(pt, "default_transient_file", lambda: target)
    pt.write_transient("listening", ttl_seconds=5.0, owner="autopilot")
    pt.clear_transient(owner="dictate")
    assert target.exists()  # different owner: don't delete
    pt.clear_transient(owner="autopilot")
    assert not target.exists()


def test_context_manager_writes_then_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pet-transient.json"
    monkeypatch.setattr(pt, "default_transient_file", lambda: target)

    inside = {"seen": False}
    with pt.pet_state("thinking", ttl_seconds=2.0):
        inside["seen"] = target.exists()
    assert inside["seen"] is True
    assert not target.exists()


def test_context_manager_cleans_up_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pet-transient.json"
    monkeypatch.setattr(pt, "default_transient_file", lambda: target)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with pt.pet_state("listening", ttl_seconds=2.0):
            raise Boom()
    assert not target.exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_pet_transient.py -v`
Expected: ImportError — `pet_transient` does not exist.

- [ ] **Step 3: Write the module**

Create `agent_doctor/pet_transient.py`:

```python
"""Transient pet state overlay.

`pet_display` reads the main `pet-status.json` (autopilot-driven) on every
tick, then checks for `pet-transient.json`. If present and unexpired, the
transient state OVERLAYS the snapshot's state without clobbering any other
field. This lets dictate temporarily switch the pet to `listening` or
`thinking` without losing autopilot's `intervening` underneath.

The TTL is a safety net: a crashed pipeline cannot strand the pet in a
transient state forever.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SUPPORTED_TRANSIENT_STATES = ("listening", "thinking")
DEFAULT_TRANSIENT_DIR = Path("~/.agent-doctor/pet").expanduser()
TRANSIENT_FILE_NAME = "pet-transient.json"


def default_transient_file() -> Path:
    return DEFAULT_TRANSIENT_DIR / TRANSIENT_FILE_NAME


class PetTransientError(RuntimeError):
    pass


def write_transient(
    state: str,
    *,
    ttl_seconds: float,
    owner: str = "dictate",
    clock: Optional[Any] = None,
) -> Path:
    if state not in SUPPORTED_TRANSIENT_STATES:
        raise PetTransientError(
            f"state {state!r} is not transient; expected one of {SUPPORTED_TRANSIENT_STATES}"
        )
    now = (clock or time.time)()
    path = default_transient_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "owner": owner,
        "started_at": float(now),
        "expires_at": float(now) + float(ttl_seconds),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".pet-transient.", dir=str(path.parent))
    try:
        os.write(fd, json.dumps(payload, sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_name, path)
    return path


def read_transient(*, clock: Optional[Any] = None) -> Optional[dict[str, Any]]:
    path = default_transient_file()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    expires_at = float(payload.get("expires_at", 0))
    now = (clock or time.time)()
    if expires_at <= now:
        return None
    state = payload.get("state")
    if state not in SUPPORTED_TRANSIENT_STATES:
        return None
    return payload


def clear_transient(*, owner: str = "dictate") -> None:
    path = default_transient_file()
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        path.unlink(missing_ok=True)
        return
    if payload.get("owner", owner) == owner:
        path.unlink(missing_ok=True)


@contextmanager
def pet_state(state: str, *, ttl_seconds: float, owner: str = "dictate") -> Iterator[None]:
    """Write the transient state on enter, clear it on exit (even on exception)."""

    write_transient(state, ttl_seconds=ttl_seconds, owner=owner)
    try:
        yield
    finally:
        clear_transient(owner=owner)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_pet_transient.py -v`
Expected: 6 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/pet_transient.py tests/test_pet_transient.py
git commit -m "feat(pet): transient state file + context manager for listening/thinking"
```

---

## Task 2: Overlay transient state in `pet_display.snapshot_from_payload`

**Files:**
- Modify: `agent_doctor/pet_display.py`
- Test: `tests/test_pet_overlay.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pet_overlay.py`:

```python
"""Tests for transient overlay logic in pet_display."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_doctor import pet_display, pet_transient as pt


def test_overlay_replaces_state_with_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pt, "default_transient_file", lambda: tmp_path / "pet-transient.json")
    pt.write_transient("listening", ttl_seconds=10.0)

    snapshot = pet_display.snapshot_from_payload(
        {"state": "intervening", "headline": "x", "severity": "high"}
    )
    overlaid = pet_display.apply_transient_overlay(snapshot)
    assert overlaid.state == "listening"
    assert overlaid.headline == snapshot.headline


def test_overlay_does_nothing_when_no_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pt, "default_transient_file", lambda: tmp_path / "nope.json")
    snapshot = pet_display.snapshot_from_payload({"state": "watching"})
    overlaid = pet_display.apply_transient_overlay(snapshot)
    assert overlaid.state == "watching"


def test_overlay_ignores_unsupported_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pt, "default_transient_file", lambda: tmp_path / "pet-transient.json")
    (tmp_path / "pet-transient.json").write_text(
        '{"state": "nope", "owner": "dictate", "started_at": 0, "expires_at": 9999999999}'
    )
    snapshot = pet_display.snapshot_from_payload({"state": "watching"})
    overlaid = pet_display.apply_transient_overlay(snapshot)
    assert overlaid.state == "watching"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_pet_overlay.py -v`
Expected: 3 failing — `apply_transient_overlay` not defined.

- [ ] **Step 3: Implement the overlay helper**

In `agent_doctor/pet_display.py`, add accent/label entries for the new states. Find `snapshot_from_payload` (around line 148) and below it (or in the helpers right after) add:

```python
def apply_transient_overlay(snapshot: DisplaySnapshot) -> DisplaySnapshot:
    """Return a snapshot whose ``state`` is overlaid by the transient file if any.

    All other fields are preserved. Animation accent/fill colours are picked
    from a per-state table when the transient overlays a state.
    """

    from . import pet_transient as _pt

    payload = _pt.read_transient()
    if not payload:
        return snapshot
    state = payload.get("state")
    accent, fill = _transient_visuals(state)
    return replace(
        snapshot,
        state=state,
        accent=accent or snapshot.accent,
        fill=fill or snapshot.fill,
    )


def _transient_visuals(state: str) -> tuple[str | None, str | None]:
    if state == "listening":
        return "#33b3a8", "#e0fbf8"
    if state == "thinking":
        return "#e0a040", "#fff5d6"
    return None, None
```

Update `_state_label` (around line 462) to recognise the new states:

```python
def _state_label(snapshot: DisplaySnapshot) -> str:
    if snapshot.state == "intervening":
        return "助理需要你的确认"
    if snapshot.state == "concerned":
        return "Concerned"
    if snapshot.state == "watching":
        return "Watching"
    if snapshot.state == "listening":
        return "Listening…"
    if snapshot.state == "thinking":
        return "Optimizing prompt…"
    return "Idle"
```

Update the existing display tick — find the `_draw_pet(canvas, snapshot, phase=now, pet_image=sprite_state["image"])` call (around line 901) and the surrounding snapshot fetch. Add the overlay step there. Read the function carefully first; modify only the lines that compute the snapshot per tick. Search for `_visible_snapshot(` and apply the overlay to its return value before passing to `_draw_pet`. Example:

```python
        snapshot = _visible_snapshot(
            read_status_payload(status_path),
            interaction["seen_at"],
            time.monotonic(),
        )
        snapshot = apply_transient_overlay(snapshot)
        _draw_pet(canvas, snapshot, phase=now, pet_image=sprite_state["image"])
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_pet_overlay.py -v`
Expected: 3 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/pet_display.py tests/test_pet_overlay.py
git commit -m "feat(pet): overlay transient state on top of main snapshot per tick"
```

---

## Task 3: Pure-function animations in `pet_animations.py`

**Files:**
- Create: `agent_doctor/pet_animations.py`
- Test: `tests/test_pet_animations.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pet_animations.py`:

```python
"""Tests for the listening / thinking animation draw helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_doctor import pet_animations as pa


@dataclass
class FakeCanvas:
    oval_calls: list[tuple[float, ...]] = field(default_factory=list)
    arc_calls: list[tuple[float, ...]] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)

    def create_oval(self, *args: Any, **kwargs: Any) -> int:
        self.oval_calls.append(tuple(args))
        return len(self.oval_calls)

    def create_arc(self, *args: Any, **kwargs: Any) -> int:
        self.arc_calls.append(tuple(args))
        return len(self.arc_calls)

    def delete(self, tag: str) -> None:
        self.delete_calls.append(tag)


def test_draw_listening_creates_ring_and_three_orbits_are_absent() -> None:
    canvas = FakeCanvas()
    pa.draw_listening(canvas, t=0.0, cx=130, cy=160)
    # Only the ring oval is drawn for listening (no orbit dots).
    assert len(canvas.oval_calls) == 1
    # The ring is centred on (cx, cy).
    x0, y0, x1, y1 = canvas.oval_calls[0]
    assert (x0 + x1) / 2 == pytest.approx(130)
    assert (y0 + y1) / 2 == pytest.approx(160)


def test_draw_listening_pulses_radius_over_time() -> None:
    canvas_a = FakeCanvas()
    canvas_b = FakeCanvas()
    pa.draw_listening(canvas_a, t=0.0, cx=0, cy=0)
    pa.draw_listening(canvas_b, t=1.0 / (2 * pa.LISTENING_RATE_HZ * 2), cx=0, cy=0)
    radius_a = (canvas_a.oval_calls[0][2] - canvas_a.oval_calls[0][0]) / 2
    radius_b = (canvas_b.oval_calls[0][2] - canvas_b.oval_calls[0][0]) / 2
    assert radius_b != pytest.approx(radius_a)


def test_draw_thinking_creates_three_dots_at_120deg() -> None:
    canvas = FakeCanvas()
    pa.draw_thinking(canvas, t=0.0, cx=100, cy=100)
    assert len(canvas.oval_calls) == 3
    centres = [
        ((call[0] + call[2]) / 2 - 100, (call[1] + call[3]) / 2 - 100)
        for call in canvas.oval_calls
    ]
    # Angles between successive dots should be ~120 degrees.
    def angle(p: tuple[float, float]) -> float:
        return math.degrees(math.atan2(p[1], p[0])) % 360
    angles = sorted(angle(p) for p in centres)
    diffs = [(angles[i + 1] - angles[i]) for i in range(len(angles) - 1)]
    for diff in diffs:
        assert diff == pytest.approx(120.0, abs=1e-3)


def test_draw_thinking_orbits_over_time() -> None:
    canvas_a = FakeCanvas()
    canvas_b = FakeCanvas()
    pa.draw_thinking(canvas_a, t=0.0, cx=0, cy=0)
    pa.draw_thinking(canvas_b, t=1.0, cx=0, cy=0)
    # The first dot should have moved.
    centre_a = (
        (canvas_a.oval_calls[0][0] + canvas_a.oval_calls[0][2]) / 2,
        (canvas_a.oval_calls[0][1] + canvas_a.oval_calls[0][3]) / 2,
    )
    centre_b = (
        (canvas_b.oval_calls[0][0] + canvas_b.oval_calls[0][2]) / 2,
        (canvas_b.oval_calls[0][1] + canvas_b.oval_calls[0][3]) / 2,
    )
    assert centre_a != centre_b
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_pet_animations.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the animations**

Create `agent_doctor/pet_animations.py`:

```python
"""Procedural animation draw helpers for the pet's listening / thinking states.

Pure functions taking a canvas-like object (any object exposing
``create_oval``, ``create_arc``, ``delete``). Each call is one frame; the
display tick loop is responsible for calling ``canvas.delete(ANIMATION_TAG)``
before invoking the helper to avoid stacking shapes.
"""

from __future__ import annotations

import math
from typing import Any

ANIMATION_TAG = "dictate-animation"

# Listening: a soft pulsating ring at ~1.5 Hz with ±9% radius modulation.
LISTENING_RATE_HZ = 1.5
LISTENING_BASE_RADIUS = 78.0
LISTENING_AMPLITUDE = 7.0
LISTENING_COLOR = "#33b3a8"

# Thinking: three orbiting dots, 120° apart, 0.8 Hz orbit.
THINKING_RATE_HZ = 0.8
THINKING_ORBIT_RADIUS = 86.0
THINKING_DOT_RADIUS = 7.0
THINKING_COLOR = "#e0a040"


def draw_listening(canvas: Any, *, t: float, cx: float, cy: float) -> None:
    """Draw one frame of the listening animation."""

    phase = math.sin(t * 2 * math.pi * LISTENING_RATE_HZ)
    radius = LISTENING_BASE_RADIUS + LISTENING_AMPLITUDE * phase
    canvas.create_oval(
        cx - radius,
        cy - radius,
        cx + radius,
        cy + radius,
        outline=LISTENING_COLOR,
        width=3,
        tags=(ANIMATION_TAG,),
    )


def draw_thinking(canvas: Any, *, t: float, cx: float, cy: float) -> None:
    """Draw one frame of the thinking animation (three orbiting dots)."""

    base_angle = (t * 2 * math.pi * THINKING_RATE_HZ) % (2 * math.pi)
    for i in range(3):
        angle = base_angle + i * (2 * math.pi / 3)
        dx = math.cos(angle) * THINKING_ORBIT_RADIUS
        dy = math.sin(angle) * THINKING_ORBIT_RADIUS
        x = cx + dx
        y = cy + dy
        canvas.create_oval(
            x - THINKING_DOT_RADIUS,
            y - THINKING_DOT_RADIUS,
            x + THINKING_DOT_RADIUS,
            y + THINKING_DOT_RADIUS,
            fill=THINKING_COLOR,
            outline="",
            tags=(ANIMATION_TAG,),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_pet_animations.py -v`
Expected: 4 passing.

- [ ] **Step 5: Commit**

```bash
git add agent_doctor/pet_animations.py tests/test_pet_animations.py
git commit -m "feat(pet): procedural listening/thinking animations"
```

---

## Task 4: Hook animations into the pet display tick loop

**Files:**
- Modify: `agent_doctor/pet_display.py`

- [ ] **Step 1: Wire it up**

In `agent_doctor/pet_display.py`, modify the existing `_draw_tk_effects` (around line 1041). Add two new branches **at the top of the function with early returns**, BEFORE the existing if/elif chain. The existing idle/watching/concerned/intervening branches stay in place:

```python
def _draw_tk_effects(canvas: Any, snapshot: DisplaySnapshot, phase: float) -> None:
    from . import pet_animations as _pa

    canvas.delete(_pa.ANIMATION_TAG)

    pulse = (math.sin(phase * 2.0) + 1) / 2
    x_offset = 35
    y_offset = 92
    if snapshot.state == "listening":
        _pa.draw_listening(
            canvas,
            t=phase,
            cx=_WINDOW_WIDTH / 2,
            cy=194,
        )
        return
    if snapshot.state == "thinking":
        _pa.draw_thinking(
            canvas,
            t=phase,
            cx=_WINDOW_WIDTH / 2,
            cy=194,
        )
        return
    # ...existing behaviour for idle/watching/concerned/intervening unchanged...
```

(Leave the existing idle/watching/concerned/intervening branches in place after the early returns above. Read the file before editing to keep the rest of the function intact.)

- [ ] **Step 2: Smoke-run the display under a hidden tk root**

Add a guarded smoke test `tests/test_pet_animations.py`:

```python
@pytest.mark.tkinter
def test_animations_render_against_real_tk_canvas() -> None:
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    canvas = tk.Canvas(root, width=300, height=300)
    canvas.pack()
    pa.draw_listening(canvas, t=0.5, cx=150, cy=150)
    pa.draw_thinking(canvas, t=0.5, cx=150, cy=150)
    root.update_idletasks()
    root.destroy()
```

Add a `pytest.ini` marker if not already present:

```ini
# In pyproject.toml [tool.pytest.ini_options] add:
markers = [
  "tkinter: requires a running display server",
]
```

- [ ] **Step 3: Run the suite**

Run: `python3 -m pytest -q -m "not tkinter"`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add agent_doctor/pet_display.py pyproject.toml tests/test_pet_animations.py
git commit -m "feat(pet): wire listening/thinking animations into display tick loop"
```

---

## Task 5: Wrap dictate pipeline in `pet_state` context managers

**Files:**
- Modify: `agent_doctor/dictate.py`, `agent_doctor/cli.py`
- Test: `tests/test_dictate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dictate.py`:

```python
def test_run_pipeline_emits_listening_then_thinking_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import pet_transient as _pt

    states_observed: list[str] = []
    real_write = _pt.write_transient

    def recording_write(state: str, **kwargs: object) -> Path:
        states_observed.append(state)
        return real_write(state, **kwargs)

    monkeypatch.setattr(_pt, "default_transient_file", lambda: tmp_path / "pt.json")
    monkeypatch.setattr(_pt, "write_transient", recording_write)

    audio = tmp_path / "in.wav"
    audio.write_bytes(b"\x00")

    def fake_transcriber(p: Path, m: str, l: object) -> str:
        return "hello world"

    def fake_caller(cfg: dictate.LLMConfig, messages: list[dict[str, str]]) -> str:
        return "Hello world."

    result = dictate.run_pipeline(
        audio,
        mode="optimize",
        enhance=True,
        transcriber=fake_transcriber,
        enhancer=fake_caller,
    )
    assert result.enhanced
    assert "listening" not in states_observed or "thinking" in states_observed
    # Required: at least 'thinking' state must have been emitted during enhance.
    assert "thinking" in states_observed
```

Note: `run_pipeline` only owns transcribe + enhance (the recording happens earlier in `start_recording`). We assert at least the `thinking` state fires for the enhance window. The `listening` state is asserted in the CLI-side test below.

Append:

```python
def test_dictate_finish_emits_listening_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI handler should mark the pet as listening from 'stop' to the end of transcribe."""

    from agent_doctor import cli, dictate as _d, pet_transient as _pt

    states_observed: list[str] = []
    monkeypatch.setattr(_pt, "default_transient_file", lambda: tmp_path / "pt.json")
    real_write = _pt.write_transient

    def recording_write(state: str, **kwargs: object) -> Path:
        states_observed.append(state)
        return real_write(state, **kwargs)

    monkeypatch.setattr(_pt, "write_transient", recording_write)

    # Stub the entire pipeline out.
    monkeypatch.setattr(_d, "default_state_dir", lambda: tmp_path)
    state = _d.DictateState(
        pid=os.getpid(),
        audio_path=str(tmp_path / "x.wav"),
        mode="optimize",
        started_at=time.time(),
        recorder="sox",
    )
    (tmp_path / "x.wav").write_bytes(b"\x00")
    _d.write_state(state, state_dir=tmp_path)

    monkeypatch.setattr(_d, "stop_recording", lambda **_k: Path(state.audio_path))
    monkeypatch.setattr(_d, "transcribe", lambda *a, **k: "hello")
    monkeypatch.setattr(_d, "enhance_prompt", lambda *a, **k: "Hello.")
    monkeypatch.setattr(_d, "copy_to_clipboard", lambda *a, **k: None)
    monkeypatch.setattr(_d, "record_history", lambda **_k: 0)
    monkeypatch.setattr(_d, "notify", lambda *a, **k: None)
    monkeypatch.setattr(_d, "play_sound", lambda *a, **k: None)

    rc = cli.main(["dictate", "stop"])
    assert rc == 0
    assert "listening" in states_observed
    assert "thinking" in states_observed
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_dictate.py -v -k "listening or thinking"`
Expected: failures — no `pet_state` wrapping yet.

- [ ] **Step 3: Wrap `enhance_prompt` in `pet_state("thinking", ...)` inside `run_pipeline`**

In `agent_doctor/dictate.py`, edit `run_pipeline` (around line 1074). After the `do_enhance = enhance and not is_raw_mode(mode)` line, replace the enhance call block with:

```python
    from . import pet_transient as _pt
    with _pt.pet_state("thinking", ttl_seconds=60.0):
        prompt = enhance_prompt(
            transcript,
            mode=mode,
            config=llm_config,
            caller=enhancer,
        )
```

- [ ] **Step 4: Wrap the stop → transcribe window in `_dictate_finish`**

In `agent_doctor/cli.py` `_dictate_finish`, just above the `t0 = time.time()` line, add:

```python
    from . import pet_transient as _pt
```

Then wrap the recording-stop + transcription block in `pet_state("listening", ...)`. Concretely, replace:

```python
    t0 = time.time()
    try:
        audio_path = _d.stop_recording()
    except _d.DictateError as exc:
        ...
    t_stop = time.time()
```

with:

```python
    t0 = time.time()
    with _pt.pet_state("listening", ttl_seconds=180.0):
        try:
            audio_path = _d.stop_recording()
        except _d.DictateError as exc:
            _d.clear_state()
            if play_audio:
                _d.play_sound(_d.DEFAULT_FAIL_SOUND)
            print(f"agent-doctor: {exc}", file=sys.stderr)
            return 2
        t_stop = time.time()
```

The existing transcribe + enhance block stays *inside* the `with` clause — `pet_state` clears the listening overlay on exit, then the `enhance` step within `run_pipeline` writes a fresh `thinking` overlay. Make sure the indentation matches.

- [ ] **Step 5: Run the tests**

Run: `python3 -m pytest tests/test_dictate.py -v -k "listening or thinking"`
Expected: both green.

- [ ] **Step 6: Run the whole suite**

Run: `python3 -m pytest -q -m "not tkinter"`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add agent_doctor/dictate.py agent_doctor/cli.py tests/test_dictate.py
git commit -m "feat(dictate): emit listening + thinking pet states during pipeline"
```

---

## Task 6: Update README + manual visual smoke

- [ ] **Step 1: Document the new behaviour**

In `README.md`, after the dictate section add:

```markdown
### Pet animations

When you trigger a dictation, the pet sprite shows a pulsing cyan ring (`listening`) while audio is being captured/transcribed, then three orbiting amber dots (`thinking`) while the LLM rewrites your transcript. The autopilot-driven state (e.g. `intervening`) is restored as soon as the pipeline finishes — dictation never clobbers an open critical alert.

Disable per-state via `~/.agent-doctor/dictate.json` (`pet.animate_listening`, `pet.animate_thinking`).
```

- [ ] **Step 2: Manual visual check**

```bash
# Terminal 1
python3 -m agent_doctor.cli pet-display

# Terminal 2 (with a recording binary installed)
python3 -m agent_doctor.cli dictate toggle  # starts
sleep 4
python3 -m agent_doctor.cli dictate toggle  # stops, listening + thinking visible
```

Expected: the pet shows the cyan ring during stop→transcribe, the amber dots during enhance, and returns to its previous state after the clipboard is updated.

- [ ] **Step 3: Commit + tag**

```bash
git add README.md
git commit -m "docs(pet): describe listening/thinking animations"
git tag dictate-phase-3-complete
```

---

## Phase 3 verification checklist

- [ ] `python3 -m pytest -q -m "not tkinter"` is green.
- [ ] `pet_transient.read_transient()` returns `None` for an expired file.
- [ ] A real dictation cycle visibly shows the cyan ring (listening) and the amber dots (thinking) on the pet sprite.
- [ ] After the cycle, the previous autopilot state (if any) is restored.
- [ ] No new runtime dependencies introduced.
