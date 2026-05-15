"""Tests for the auto-paste helper."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_doctor import dictate_paste as dp


def test_paste_invokes_osascript_keystroke() -> None:
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    dp.paste(runner=fake_runner, delay_seconds=0.0)
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "osascript"
    assert "-e" in argv
    script_idx = argv.index("-e") + 1
    assert 'keystroke "v" using {command down}' in argv[script_idx]


def test_paste_propagates_failure() -> None:
    def fake_runner(_argv: list[str]) -> int:
        return 17

    with pytest.raises(dp.PasteError, match="osascript"):
        dp.paste(runner=fake_runner, delay_seconds=0.0)


def test_permission_test_records_timestamp_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")

    def fake_runner(_argv: list[str]) -> int:
        return 0

    def fake_pbcopy(_argv: list[str], _data: bytes) -> int:
        return 0

    ok = dp.permission_test(runner=fake_runner, clipboard_runner=fake_pbcopy)
    assert ok is True
    settings = ds.load()
    assert settings.paste.last_permission_check is not None


def test_enable_requires_passing_permission_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")

    def failing_runner(_argv: list[str]) -> int:
        return 1

    with pytest.raises(dp.PasteError, match="permission"):
        dp.enable(runner=failing_runner, clipboard_runner=lambda *_: 0)
    settings = ds.load()
    assert settings.paste.auto_paste is False


def test_enable_flips_settings_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    dp.enable(runner=lambda _a: 0, clipboard_runner=lambda *_: 0)
    settings = ds.load()
    assert settings.paste.auto_paste is True


def test_disable_flips_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        paste=ds.PasteSettings(auto_paste=True),
    )
    ds.save(settings)
    dp.disable()
    assert ds.load().paste.auto_paste is False


def test_maybe_auto_paste_noop_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    called: list[bool] = []
    dp.maybe_auto_paste(runner=lambda _a: called.append(True) or 0)
    assert called == []


def test_maybe_auto_paste_runs_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        paste=ds.PasteSettings(auto_paste=True, paste_delay_ms=0),
    )
    ds.save(settings)
    called: list[list[str]] = []
    dp.maybe_auto_paste(runner=lambda argv: (called.append(argv), 0)[1])
    assert called
