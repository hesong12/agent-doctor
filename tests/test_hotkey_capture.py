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


def test_side_modifier_plus_key_normalises_to_canonical_modifier() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="right_cmd"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="a"))
    assert ctl.state is hc.State.CAPTURED_CHORD
    assert ctl.captured == "cmd+a"


def test_two_side_modifiers_plus_key() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="right_cmd"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="left_option"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="a"))
    assert ctl.state is hc.State.CAPTURED_CHORD
    assert ctl.captured == "cmd+option+a"


def test_captured_returns_none_in_idle_after_bare_modifier_press() -> None:
    # A bare canonical-modifier press (e.g. just "shift") is not enough to
    # form a usable binding -- controller stays IDLE and captured is None.
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="shift"))
    assert ctl.state is hc.State.IDLE
    assert ctl.captured is None


def test_commit_in_idle_raises() -> None:
    ctl = hc.CaptureController()
    with pytest.raises(RuntimeError, match="nothing captured"):
        ctl.commit()


def test_commit_terminal_is_idempotent() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="ctrl"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space"))
    ctl.commit()
    assert ctl.state is hc.State.COMMITTED
    # second commit should be a no-op, not raise
    ctl.commit()
    assert ctl.commit_result == "ctrl+space"


def test_negative_held_ms_is_clamped() -> None:
    ctl = hc.CaptureController(min_hold_ms=400)
    ctl.on_key_event(hc.KeyEvent(kind="press", key="right_cmd", t_ms=1000))
    # Release at earlier timestamp -- should not commit (clamped to 0 < 400).
    ctl.on_key_event(hc.KeyEvent(kind="release", key="right_cmd", t_ms=500))
    assert ctl.state is hc.State.IDLE
    assert ctl.commit_result is None


def test_chord_sticky_on_modifier_release() -> None:
    # After a chord lands, releasing the modifier (not the key) keeps the
    # snapshot stable so the UI can keep showing "ctrl+space" while the user
    # moves to click Use this chord.
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="ctrl", t_ms=0))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space", t_ms=10))
    ctl.on_key_event(hc.KeyEvent(kind="release", key="ctrl", t_ms=20))
    assert ctl.state is hc.State.CAPTURED_CHORD
    assert ctl.captured == "ctrl+space"


def test_key_repeat_preserves_captured_chord() -> None:
    """Tk emits key-repeat presses for held keys -- must not reset capture."""
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="ctrl", t_ms=0))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space", t_ms=10))
    assert ctl.state is hc.State.CAPTURED_CHORD
    assert ctl.captured == "ctrl+space"
    # Simulate space key-repeat while still held.
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space", t_ms=200))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="space", t_ms=300))
    assert ctl.state is hc.State.CAPTURED_CHORD
    assert ctl.captured == "ctrl+space"


def test_capture_chord_with_escape_key() -> None:
    ctl = hc.CaptureController()
    ctl.on_key_event(hc.KeyEvent(kind="press", key="ctrl"))
    ctl.on_key_event(hc.KeyEvent(kind="press", key="escape"))
    assert ctl.state is hc.State.CAPTURED_CHORD
    assert ctl.captured == "ctrl+escape"
