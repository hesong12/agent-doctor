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
