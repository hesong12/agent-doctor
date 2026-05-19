"""Tests for the auto-paste helper."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_doctor import dictate_paste as dp


@pytest.fixture
def fake_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Simulate the Swift helper's paste-request file watcher.

    Background thread polls for the paste-request file and deletes it
    on appearance, exactly like the real helper. Tests using this
    fixture can call ``dp.paste()`` and have it succeed without a
    running daemon. Returns the simulator object so tests can also
    assert "request landed" if they care.
    """

    import sys as _sys
    import threading
    monkeypatch.setattr(_sys, "platform", "darwin")
    req = tmp_path / "paste-request"
    monkeypatch.setattr(dp, "PASTE_REQUEST_PATH", req)

    consumed_count = [0]
    stop_event = threading.Event()

    def watcher() -> None:
        while not stop_event.is_set():
            if req.exists():
                try:
                    req.unlink()
                    consumed_count[0] += 1
                except FileNotFoundError:
                    pass
            time.sleep(0.01)

    t = threading.Thread(target=watcher, daemon=True)
    t.start()
    yield {"path": req, "consumed_count": consumed_count}
    stop_event.set()


def test_paste_writes_helper_request_file(fake_helper) -> None:
    """The Python paste API drops a sidecar file the Swift helper
    polls. Switched from osascript because macOS Accessibility rejects
    keystroke requests from launchd-spawned osascript chains (error
    1002 in field), but the helper itself has Accessibility so
    CGEventPost works."""

    dp.paste(delay_seconds=0.0)
    assert fake_helper["consumed_count"][0] >= 1


def test_paste_raises_when_helper_does_not_respond(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the helper isn't running (no one consumes the request file
    within the timeout) paste must raise with a clear error so the
    caller can surface a notification."""

    import sys as _sys
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.setattr(dp, "PASTE_REQUEST_PATH", tmp_path / "paste-request")
    # Shorten timeout for the test so it doesn't take 500ms.
    monkeypatch.setattr(dp, "_PASTE_REQUEST_TIMEOUT_S", 0.1)

    with pytest.raises(dp.PasteError, match="helper did not respond"):
        dp.paste(delay_seconds=0.0)


def test_permission_test_records_timestamp_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_helper
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")

    def fake_pbcopy(_argv: list[str], _data: bytes) -> int:
        return 0

    ok = dp.permission_test(clipboard_runner=fake_pbcopy)
    assert ok is True
    settings = ds.load()
    assert settings.paste.last_permission_check is not None


def test_enable_requires_passing_permission_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When permission_test fails (no pet-display picks up the paste
    request within timeout), enable() must raise and NOT flip
    auto_paste on. Tests isolate the request file to tmp_path so the
    developer's real pet-display can't accidentally consume it.
    """

    import sys as _sys
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    monkeypatch.setattr(dp, "PASTE_REQUEST_PATH", tmp_path / "paste-request")
    # Tight timeout so the test finishes quickly. No fake_helper means
    # nothing consumes the file → paste() raises → permission_test
    # returns False → enable() raises.
    monkeypatch.setattr(dp, "_PASTE_REQUEST_TIMEOUT_S", 0.1)

    with pytest.raises(dp.PasteError, match="permission"):
        dp.enable(clipboard_runner=lambda *_: 0)
    settings = ds.load()
    assert settings.paste.auto_paste is False


def test_enable_flips_settings_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_helper
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    dp.enable(clipboard_runner=lambda *_: 0)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_helper
) -> None:
    from agent_doctor import dictate_settings as ds

    monkeypatch.setattr(ds, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ds, "CONFIG_FILE", tmp_path / "dictate.json")
    settings = ds.replace_section(
        ds.default_settings(),
        paste=ds.PasteSettings(auto_paste=True, paste_delay_ms=0),
    )
    ds.save(settings)
    dp.maybe_auto_paste()
    assert fake_helper["consumed_count"][0] >= 1


def test_dictate_finish_calls_auto_paste_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_helper
) -> None:
    """End-to-end: when auto_paste is on and pipeline succeeds, paste
    fires. The helper-driven path drops a request file the fake helper
    fixture consumes, so the assertion checks the consume count
    instead of an osascript argv list."""

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

    rc = cli.main(["dictate", "stop"])
    assert rc == 0
    assert fake_helper["consumed_count"][0] >= 1  # paste fired
