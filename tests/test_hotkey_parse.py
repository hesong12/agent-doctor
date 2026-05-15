"""Tests for chord parsing + conflict detection."""

from __future__ import annotations

import pytest

from agent_doctor import hotkey_parse as hp


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ctrl+option+space", "ctrl+option+space"),
        ("CTRL + OPT + SPACE", "ctrl+option+space"),
        ("control + alt + space", "ctrl+option+space"),
        ("cmd+shift+d", "cmd+shift+d"),
        ("option+space", "option+space"),
    ],
)
def test_parse_canonical(raw: str, expected: str) -> None:
    chord = hp.parse(raw)
    assert chord.canonical() == expected


def test_parse_empty_raises() -> None:
    with pytest.raises(hp.HotkeyParseError, match="empty"):
        hp.parse("")


def test_parse_requires_a_modifier() -> None:
    with pytest.raises(hp.HotkeyParseError, match="modifier"):
        hp.parse("space")


def test_parse_rejects_unknown_token() -> None:
    with pytest.raises(hp.HotkeyParseError, match="unknown"):
        hp.parse("ctrl+banana")


@pytest.mark.parametrize(
    "raw",
    ["cmd+space", "cmd+tab", "cmd+q", "cmd+w"],
)
def test_known_conflicts_are_rejected(raw: str) -> None:
    with pytest.raises(hp.HotkeyParseError, match="conflict"):
        hp.parse(raw)
