"""Chord string parser for the global hotkey helper.

Canonical form: ``mod1+mod2+key`` with modifiers in this order: cmd, ctrl,
option, shift. Lowercase. Key tokens are short identifiers (``space``,
``return``, ``f1``, single letters, digits).

Conflicts are explicitly refused for chords that collide with macOS system
shortcuts (cmd+space spotlight, cmd+tab app-switcher, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Tuple

MODIFIER_ALIASES = {
    "cmd": "cmd",
    "command": "cmd",
    "ctrl": "ctrl",
    "control": "ctrl",
    "option": "option",
    "opt": "option",
    "alt": "option",
    "shift": "shift",
}
MODIFIER_ORDER = ("cmd", "ctrl", "option", "shift")

KEY_TOKENS = frozenset(
    {"space", "return", "enter", "escape", "tab", "delete", "backspace"}
    | {chr(c) for c in range(ord("a"), ord("z") + 1)}
    | {chr(c) for c in range(ord("0"), ord("9") + 1)}
    | {f"f{n}" for n in range(1, 13)}
)

CONFLICT_CHORDS: FrozenSet[str] = frozenset(
    {
        "cmd+space",
        "cmd+tab",
        "cmd+q",
        "cmd+w",
        "cmd+option+escape",
        "cmd+shift+3",
        "cmd+shift+4",
        "cmd+shift+5",
    }
)


class HotkeyParseError(ValueError):
    pass


@dataclass(frozen=True)
class Chord:
    modifiers: Tuple[str, ...]
    key: str

    def canonical(self) -> str:
        return "+".join((*self.modifiers, self.key))


def parse(raw: str) -> Chord:
    if not raw or not raw.strip():
        raise HotkeyParseError("empty hotkey")
    tokens = [t.strip().lower() for t in raw.replace(",", "+").split("+") if t.strip()]
    if not tokens:
        raise HotkeyParseError("empty hotkey")

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
    return Chord(modifiers=ordered, key=keys[0])
