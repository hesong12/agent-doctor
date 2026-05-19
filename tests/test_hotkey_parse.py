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


def test_parse_chord_with_escape_key() -> None:
    chord = hp.parse("ctrl+escape")
    assert chord.canonical() == "ctrl+escape"
    assert chord.key == "escape"
