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


def test_default_input_monitoring_probe_returns_true(tmp_path) -> None:
    # The probe is intentionally optimistic — see docstring. We cannot
    # reliably detect Input Monitoring revocation without private APIs,
    # so the probe returns True regardless of log presence/freshness.
    assert pp._default_input_monitoring_probe(log_path=tmp_path / "irrelevant.log") is True
    log = tmp_path / "exists.log"
    log.write_text("data")
    assert pp._default_input_monitoring_probe(log_path=log) is True


def test_settings_url_unknown_pane_raises() -> None:
    with pytest.raises(KeyError):
        pp.settings_url("camera")
