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
