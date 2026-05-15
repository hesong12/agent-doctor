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
