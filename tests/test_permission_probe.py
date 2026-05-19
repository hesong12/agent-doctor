"""Headless tests for macOS permission detection."""

from __future__ import annotations

from typing import Any

import pytest

from agent_doctor.ui.preferences import permission_probe as pp


def test_both_granted_returns_no_missing() -> None:
    status = pp.check_macos_permissions(
        accessibility_probe=lambda: True,
        input_monitoring_probe=lambda: True,
    )
    assert status.accessibility is True
    assert status.input_monitoring is True
    assert status.first_missing is None


def test_only_accessibility_missing() -> None:
    status = pp.check_macos_permissions(
        accessibility_probe=lambda: False,
        input_monitoring_probe=lambda: True,
    )
    assert status.first_missing == "accessibility"


def test_only_input_monitoring_missing() -> None:
    status = pp.check_macos_permissions(
        accessibility_probe=lambda: True,
        input_monitoring_probe=lambda: False,
    )
    assert status.first_missing == "input_monitoring"


def test_both_missing_picks_accessibility_first() -> None:
    status = pp.check_macos_permissions(
        accessibility_probe=lambda: False,
        input_monitoring_probe=lambda: False,
    )
    assert status.first_missing == "accessibility"


def test_settings_url_for_known_panes() -> None:
    assert pp.settings_url("accessibility").startswith("x-apple.systempreferences:")
    assert "Accessibility" in pp.settings_url("accessibility")
    assert "ListenEvent" in pp.settings_url("input_monitoring")


def test_default_input_monitoring_probe_no_heartbeat(tmp_path) -> None:
    # No heartbeat = events not flowing = IM not confirmed.
    assert pp._default_input_monitoring_probe(heartbeat_path=tmp_path / "absent") is False


def test_default_input_monitoring_probe_fresh_heartbeat(tmp_path) -> None:
    hb = tmp_path / "im-heartbeat"
    hb.write_text("1\n")
    assert pp._default_input_monitoring_probe(heartbeat_path=hb) is True


def test_default_input_monitoring_probe_stale_heartbeat(tmp_path) -> None:
    import os

    hb = tmp_path / "im-heartbeat"
    hb.write_text("1\n")
    # Backdate mtime by 5 minutes.
    old = hb.stat().st_mtime - 300
    os.utime(hb, (old, old))
    assert pp._default_input_monitoring_probe(heartbeat_path=hb) is False


def test_settings_url_unknown_pane_raises() -> None:
    with pytest.raises(KeyError):
        pp.settings_url("camera")


def test_default_accessibility_probe_handles_missing_osascript(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise FileNotFoundError("osascript")
    monkeypatch.setattr(pp.subprocess, "run", boom)
    assert pp._default_accessibility_probe() is False


def test_default_accessibility_probe_handles_timeout(monkeypatch) -> None:
    def slow(*_a, **_k):
        import subprocess as _sp
        raise _sp.TimeoutExpired(cmd="osascript", timeout=2.0)
    monkeypatch.setattr(pp.subprocess, "run", slow)
    assert pp._default_accessibility_probe() is False
