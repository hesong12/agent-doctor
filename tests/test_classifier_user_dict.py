"""Tests for user_dict."""
import json
import stat
from pathlib import Path

import pytest

from agent_doctor.classifier.user_dict import UserDict, load_user_dict, save_user_dict


def test_add_phrases_dedupes() -> None:
    d = UserDict()
    d.add_positive("foo")
    d.add_positive("foo")
    d.add_positive("FOO")  # different case — kept (we lowercase only at score time)
    assert d.positive.count("foo") == 1


def test_add_negative_dedupes() -> None:
    d = UserDict()
    d.add_negative("bar")
    d.add_negative("bar")
    assert d.negative == ["bar"]


def test_score_adjustment_positive_match() -> None:
    d = UserDict(positive=["sluggish"])
    assert d.score_adjustment("the agent is sluggish today") == 1


def test_score_adjustment_negative_match() -> None:
    d = UserDict(negative=["interesting"])
    assert d.score_adjustment("interesting choice") == -1


def test_score_adjustment_caps_at_plus_minus_two() -> None:
    d = UserDict(positive=["a", "b", "c"])
    # 3 hits would normally be +3, cap at +2
    assert d.score_adjustment("a b c") == 2

    d = UserDict(negative=["x", "y", "z"])
    assert d.score_adjustment("x y z") == -2


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "user-dict.json"
    d = UserDict(positive=["sluggish", "again"], negative=["interesting"])
    save_user_dict(path, d)

    loaded = load_user_dict(path)
    assert loaded.positive == ["sluggish", "again"]
    assert loaded.negative == ["interesting"]


def test_save_writes_0o600(tmp_path: Path) -> None:
    path = tmp_path / "user-dict.json"
    save_user_dict(path, UserDict())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    d = load_user_dict(tmp_path / "missing.json")
    assert d.positive == []
    assert d.negative == []


def test_load_malformed_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    d = load_user_dict(path)
    assert d.positive == []
