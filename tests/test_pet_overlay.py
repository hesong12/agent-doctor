"""Tests for transient overlay logic in pet_display."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_doctor import pet_display, pet_transient as pt


def test_overlay_replaces_state_with_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pt, "default_transient_file", lambda: tmp_path / "pet-transient.json")
    pt.write_transient("listening", ttl_seconds=10.0)

    snapshot = pet_display.snapshot_from_payload(
        {"state": "intervening", "headline": "x", "severity": "high"}
    )
    overlaid = pet_display.apply_transient_overlay(snapshot)
    assert overlaid.state == "listening"
    assert overlaid.headline == snapshot.headline


def test_overlay_does_nothing_when_no_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pt, "default_transient_file", lambda: tmp_path / "nope.json")
    snapshot = pet_display.snapshot_from_payload({"state": "watching"})
    overlaid = pet_display.apply_transient_overlay(snapshot)
    assert overlaid.state == "watching"


def test_overlay_ignores_unsupported_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pt, "default_transient_file", lambda: tmp_path / "pet-transient.json")
    (tmp_path / "pet-transient.json").write_text(
        '{"state": "nope", "owner": "dictate", "started_at": 0, "expires_at": 9999999999}'
    )
    snapshot = pet_display.snapshot_from_payload({"state": "watching"})
    overlaid = pet_display.apply_transient_overlay(snapshot)
    assert overlaid.state == "watching"
