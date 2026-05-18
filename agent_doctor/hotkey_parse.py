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
    key: str | None = None
    side: str | None = None

    def canonical(self) -> str:
        if self.key is None:
            mod = self.modifiers[0]
            if mod == "fn":
                return "fn"
            return f"{self.side}_{mod}"
        return "+".join((*self.modifiers, self.key))


def is_modifier_only(chord: "Chord") -> bool:
    return chord.key is None


def parse(raw: str) -> Chord:
    if not raw or not raw.strip():
        raise HotkeyParseError("empty hotkey")
    tokens = [t.strip().lower() for t in raw.replace(",", "+").split("+") if t.strip()]
    if not tokens:
        raise HotkeyParseError("empty hotkey")

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
