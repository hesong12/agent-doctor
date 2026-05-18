"""Headless tests for macOS permission detection."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agent_doctor.ui.preferences import permission_probe as pp


def _fake_run(return_codes: dict[str, int]):
    def _runner(argv, *args, **kwargs):
        class Result:
            returncode = return_codes.get(" ".join(argv), 0)
            stdout = b""
            stderr = b""
        return Result()
    return _runner


def test_both_granted_returns_no_missing() -> None:
    with patch.object(pp.subprocess, "run", _fake_run({})):
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
