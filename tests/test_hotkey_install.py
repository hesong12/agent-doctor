"""Tests for hotkey_install build / plist / launchctl wiring."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_doctor import hotkey_install as hi


def _make_fake_swiftc(tmp_path: Path) -> Path:
    """Write a fake swiftc script that just writes a dummy binary to -o target."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "swiftc"
    fake.write_text(
        '#!/usr/bin/env bash\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then\n'
        '    shift\n'
        '    echo "#!/bin/sh\\necho ran helper $@" > "$1"\n'
        '    chmod +x "$1"\n'
        '    shift\n'
        '    continue\n'
        '  fi\n'
        '  shift\n'
        'done\n'
        'exit 0\n'
    )
    fake.chmod(0o755)
    return bin_dir


def test_build_runs_swiftc_and_writes_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{':' + str(tmp_path)}")
    src = tmp_path / "HotkeyHelper.swift"
    src.write_text("// fake source")
    dest = tmp_path / "out" / "agent-doctor-hotkey"
    hi.build(src, dest)
    assert dest.exists()
    assert dest.stat().st_mode & 0o111  # executable


def test_build_raises_without_swiftc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no swiftc on PATH
    src = tmp_path / "HotkeyHelper.swift"
    src.write_text("// fake")
    with pytest.raises(hi.HotkeyInstallError, match="swiftc"):
        hi.build(src, tmp_path / "out" / "bin")


def test_write_plist_content(tmp_path: Path) -> None:
    plist_path = tmp_path / "com.agent-doctor.hotkey.plist"
    helper = tmp_path / "helper"
    helper.write_text("# fake")
    helper.chmod(0o755)
    hi.write_plist(plist_path, helper, "/usr/local/bin/agent-doctor")
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == "com.agent-doctor.hotkey"
    assert parsed["ProgramArguments"] == [str(helper)]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["EnvironmentVariables"]["AGENT_DOCTOR_BIN"] == "/usr/local/bin/agent-doctor"


def test_install_calls_launchctl_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", tmp_path / "helper")
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    monkeypatch.setattr(hi, "SWIFT_SOURCE", tmp_path / "HotkeyHelper.swift")
    (tmp_path / "HotkeyHelper.swift").write_text("// fake source")

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(hi, "_run_launchctl", fake_run)
    hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    assert any(arg[:2] == ["launchctl", "bootstrap"] for arg in calls)


def test_uninstall_calls_launchctl_bootout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", tmp_path / "helper")
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    (tmp_path / "plist").write_text("<plist/>")

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(hi, "_run_launchctl", fake_run)
    hi.uninstall()
    assert any(arg[:2] == ["launchctl", "bootout"] for arg in calls)


def test_pause_runs_bootout_without_removing_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "x.plist")
    (tmp_path / "x.plist").write_text("dummy")

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(hi, "_run_launchctl", fake_run)
    assert hi.pause() is True
    assert any("bootout" in " ".join(c) for c in calls)
    assert (tmp_path / "x.plist").exists()


def test_resume_rebuilds_and_bootstraps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resume()`` is an alias for ``install()`` — both paths must rebuild
    the helper so upgraded users with stale on-disk binaries from previous
    versions get the current Swift source recompiled."""

    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", tmp_path / "helper")
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    monkeypatch.setattr(hi, "SWIFT_SOURCE", tmp_path / "HotkeyHelper.swift")
    (tmp_path / "HotkeyHelper.swift").write_text("// fake source")

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(hi, "_run_launchctl", fake_run)
    result = hi.resume()
    # Rebuilt helper exists.
    assert (tmp_path / "helper").exists()
    # Plist was written.
    assert (tmp_path / "plist").exists()
    # launchctl bootstrap was called.
    assert any(arg[:2] == ["launchctl", "bootstrap"] for arg in calls)
    assert "helper" in result and "plist" in result


def test_status_handles_missing_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status()`` must report ``running=False`` rather than crash when
    ``launchctl`` is unavailable (non-macOS, sandboxed env, broken PATH)."""

    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "absent.plist")
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", tmp_path / "absent.helper")

    def boom(*_a: Any, **_k: Any) -> subprocess.CompletedProcess:
        raise FileNotFoundError("launchctl not on PATH")

    monkeypatch.setattr(hi, "_run_launchctl", boom)
    status = hi.status()
    assert status["running"] is False
    assert status["plist_exists"] is False
    assert status["helper_exists"] is False


def test_read_agent_doctor_bin_from_existing_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "test.plist"
    plistlib.dump(
        {"EnvironmentVariables": {"AGENT_DOCTOR_BIN": "/custom/path/agent-doctor"}},
        plist_path.open("wb"),
    )
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", plist_path)
    assert hi.read_agent_doctor_bin() == "/custom/path/agent-doctor"


def test_read_agent_doctor_bin_returns_none_for_missing_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "absent.plist")
    assert hi.read_agent_doctor_bin() is None


def test_read_agent_doctor_bin_returns_none_when_env_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = tmp_path / "test.plist"
    plistlib.dump({"Label": "x"}, plist_path.open("wb"))
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", plist_path)
    assert hi.read_agent_doctor_bin() is None
