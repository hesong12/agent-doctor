"""Tests for the auto-paste helper."""

from __future__ import annotations

import subprocess
import sys
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


def test_dictate_finish_calls_auto_paste_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: when auto_paste is on and pipeline succeeds, paste fires."""

    if sys.platform != "darwin":
        pytest.skip("auto-paste short-circuits to no-op off Darwin (dictate_paste.py)")

    import os, time

    from agent_doctor import cli, dictate as _d, dictate_paste as dp, dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        paste=ds.PasteSettings(auto_paste=True, paste_delay_ms=0),
    )
    ds.save(settings)

    # Stub the full pipeline like in Phase 3.
    state = _d.DictateState(
        pid=12345,
        audio_path=str(tmp_path / "x.wav"),
        mode="optimize",
        started_at=0.0,
        recorder="sox",
    )
    (tmp_path / "x.wav").write_bytes(b"\x00")
    monkeypatch.setattr(_d, "default_state_dir", lambda: tmp_path)
    _d.write_state(state, state_dir=tmp_path)

    monkeypatch.setattr(_d, "is_pid_alive", lambda _pid: False)
    monkeypatch.setattr(_d, "stop_recording", lambda **_k: Path(state.audio_path))
    monkeypatch.setattr(_d, "transcribe", lambda *a, **k: "hello")
    monkeypatch.setattr(_d, "enhance_prompt", lambda *a, **k: "Hello.")
    monkeypatch.setattr(_d, "copy_to_clipboard", lambda *a, **k: None)
    monkeypatch.setattr(_d, "record_history", lambda **_k: 0)
    monkeypatch.setattr(_d, "notify", lambda *a, **k: None)
    monkeypatch.setattr(_d, "play_sound", lambda *a, **k: None)

    calls: list[list[str]] = []
    monkeypatch.setattr(dp, "_default_osascript", lambda argv: (calls.append(argv), 0)[1])
    rc = cli.main(["dictate", "stop"])
    assert rc == 0
    assert calls  # paste fired
