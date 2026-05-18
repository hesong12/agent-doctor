"""Capture overlay state machine (pure-Python; Tk wrapper layered on top).

States:
- IDLE: nothing pressed yet.
- CAPTURED_MODIFIER: a single modifier-only token (e.g. right_cmd) is held.
  Auto-commits on release if held >= min_hold_ms.
- CAPTURED_CHORD: at least one modifier + a non-modifier key is held; the
  controller does not auto-commit. Caller must invoke ``commit()``.
- CONFLICT: captured chord is in hotkey_parse.CONFLICT_CHORDS -- commit is
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
            # snapshot -- the user is re-capturing.
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
