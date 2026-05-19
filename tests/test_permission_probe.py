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
    assert (
        pp._default_input_monitoring_probe(
            heartbeat_path=tmp_path / "absent",
            startup_path=tmp_path / "absent-startup",
        )
        is False
    )


def test_default_input_monitoring_probe_fresh_heartbeat_legacy(tmp_path) -> None:
    """Legacy fallback: a helper without the im-startup stamp (pre-this-PR)
    still passes the probe on a fresh heartbeat alone. Without this, every
    user upgrading in place would see a false 'Permission needed' until
    their next helper rebuild."""

    hb = tmp_path / "im-heartbeat"
    hb.write_text("1\n")
    assert (
        pp._default_input_monitoring_probe(
            heartbeat_path=hb,
            startup_path=tmp_path / "absent-startup",
        )
        is True
    )


def test_default_input_monitoring_probe_stale_heartbeat(tmp_path) -> None:
    import os

    hb = tmp_path / "im-heartbeat"
    hb.write_text("1\n")
    # Backdate mtime by 5 minutes.
    old = hb.stat().st_mtime - 300
    os.utime(hb, (old, old))
    assert (
        pp._default_input_monitoring_probe(
            heartbeat_path=hb,
            startup_path=tmp_path / "absent-startup",
        )
        is False
    )


def test_default_input_monitoring_probe_heartbeat_after_startup_is_fresh(
    tmp_path,
) -> None:
    """Happy path: helper relaunched, monitor installed (startup stamp
    written), then a global event arrived (heartbeat rewritten). The
    heartbeat is younger than the startup stamp, so the probe trusts it."""

    import os

    startup = tmp_path / "im-startup"
    startup.write_text("1\n")
    # Backdate startup to ensure heartbeat ends up strictly newer.
    old = startup.stat().st_mtime - 10
    os.utime(startup, (old, old))

    hb = tmp_path / "im-heartbeat"
    hb.write_text("2\n")  # touched after startup
    assert (
        pp._default_input_monitoring_probe(
            heartbeat_path=hb, startup_path=startup
        )
        is True
    )


def test_default_input_monitoring_probe_heartbeat_before_startup_is_stale(
    tmp_path,
) -> None:
    """Regression for the post-revoke leftover-heartbeat scenario:

    1. Helper runs with IM granted. Heartbeat written.
    2. User revokes IM in System Settings.
    3. Install flow re-bootstraps the LaunchAgent. New helper writes a
       fresh ``im-startup`` stamp before installing the monitor.
    4. No event arrives (because IM is revoked). The heartbeat from step 1
       is still on disk and is technically less than 60s old.

    Without the heartbeat>startup comparison the probe would say
    "Listening" for up to 60s, even though IM is not granted. With the
    comparison the probe correctly returns False because the leftover
    heartbeat is older than the just-written startup stamp.
    """

    import os

    hb = tmp_path / "im-heartbeat"
    hb.write_text("from-previous-run\n")
    # Backdate heartbeat by 10 seconds (still inside the 60s freshness
    # window so the legacy code path would have said True).
    old = hb.stat().st_mtime - 10
    os.utime(hb, (old, old))

    startup = tmp_path / "im-startup"
    startup.write_text("just-installed\n")  # newer than the heartbeat

    assert (
        pp._default_input_monitoring_probe(
            heartbeat_path=hb, startup_path=startup
        )
        is False
    )


def test_default_input_monitoring_probe_equal_mtimes_is_stale(
    tmp_path,
) -> None:
    """Strictly-greater comparison: heartbeat == startup is not enough.

    If the user happened to press a key in the same millisecond the
    monitor came up, both files could share an mtime. We require the
    heartbeat to be strictly newer to avoid a confounding edge case
    where touchHeartbeat skipped its write (rate-limited to once per
    5s) and the cached mtime equals startup's mtime by coincidence.
    """

    import os

    hb = tmp_path / "im-heartbeat"
    hb.write_text("1\n")
    startup = tmp_path / "im-startup"
    startup.write_text("1\n")

    # Force identical mtimes.
    when = hb.stat().st_mtime
    os.utime(hb, (when, when))
    os.utime(startup, (when, when))

    assert (
        pp._default_input_monitoring_probe(
            heartbeat_path=hb, startup_path=startup
        )
        is False
    )


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
