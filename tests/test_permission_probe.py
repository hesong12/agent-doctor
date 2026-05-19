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


def test_default_input_monitoring_probe_no_startup_legacy_fallback(tmp_path) -> None:
    """Pre-PRα-3 helpers write only heartbeat. The probe must NOT
    suddenly start reporting "permission needed" for users who upgrade
    the Python package without re-installing the LaunchAgent — that
    would manufacture a false negative on every upgrade. Codex P2
    regression caught during PRα review.
    """

    hb = tmp_path / "im-heartbeat"
    hb.write_text("1\n")
    assert pp._default_input_monitoring_probe(
        heartbeat_path=hb, startup_path=tmp_path / "absent-startup"
    ) is True


def test_default_input_monitoring_probe_no_heartbeat(tmp_path) -> None:
    # Daemon started but no event yet = not confirmed.
    sp = tmp_path / "im-startup"
    sp.write_text("1\n")
    assert pp._default_input_monitoring_probe(
        heartbeat_path=tmp_path / "absent", startup_path=sp
    ) is False


def test_default_input_monitoring_probe_heartbeat_predates_startup(tmp_path) -> None:
    """The exact false-positive that motivated PRα-3: a leftover
    heartbeat from a previous daemon lifetime is on disk, but the
    current daemon's startup stamp is newer — meaning the user may have
    revoked IM since the last events arrived. Probe MUST return False.
    """

    import os

    hb = tmp_path / "im-heartbeat"
    hb.write_text("1\n")
    sp = tmp_path / "im-startup"
    sp.write_text("1\n")
    # Backdate heartbeat to before startup.
    hb_old = sp.stat().st_mtime - 60
    os.utime(hb, (hb_old, hb_old))
    assert pp._default_input_monitoring_probe(
        heartbeat_path=hb, startup_path=sp
    ) is False


def test_default_input_monitoring_probe_fresh_heartbeat_after_startup(tmp_path) -> None:
    """Heartbeat strictly newer than startup AND within freshness window
    = real event flow = True."""

    import os

    sp = tmp_path / "im-startup"
    sp.write_text("1\n")
    hb = tmp_path / "im-heartbeat"
    hb.write_text("1\n")
    # Move heartbeat slightly ahead.
    sp_mtime = sp.stat().st_mtime
    os.utime(hb, (sp_mtime + 1, sp_mtime + 1))
    assert pp._default_input_monitoring_probe(
        heartbeat_path=hb, startup_path=sp
    ) is True


def test_default_input_monitoring_probe_stale_heartbeat(tmp_path) -> None:
    """Heartbeat newer than startup but older than freshness window =
    daemon used to receive events but has gone silent → revoked or
    crashed → False."""

    import os

    sp = tmp_path / "im-startup"
    sp.write_text("1\n")
    # Backdate startup so we can put heartbeat after it but still in the
    # stale window.
    sp_old = sp.stat().st_mtime - 600
    os.utime(sp, (sp_old, sp_old))
    hb = tmp_path / "im-heartbeat"
    hb.write_text("1\n")
    hb_old = sp_old + 60  # 60s after startup, but 540s ago
    os.utime(hb, (hb_old, hb_old))
    assert pp._default_input_monitoring_probe(
        heartbeat_path=hb, startup_path=sp
    ) is False


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
