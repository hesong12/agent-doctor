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
    """``resume()`` is an alias for ``install()`` and must produce a
    bootstrapped helper. The first call always builds (helper absent);
    rebuild-or-skip decisions on subsequent calls are covered by the
    idempotency tests below."""

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


def test_install_skips_rebuild_when_sidecar_matches_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the sidecar SHA-256 file next to the helper matches the current
    Swift source hash, install() must not recompile. This preserves the
    binary's cdhash so macOS Input Monitoring (TCC) does not silently
    invalidate the user's granted permission on every toggle of the
    Background daemon switch.
    """

    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    helper = tmp_path / "helper"
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", helper)
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    src = tmp_path / "HotkeyHelper.swift"
    monkeypatch.setattr(hi, "SWIFT_SOURCE", src)
    src.write_text("// fake source")
    # Pre-existing helper + matching sidecar from a previous successful
    # install (this is the state install() leaves behind on first run).
    helper.write_text("#!/bin/sh\necho fake-prebuilt\n")
    helper.chmod(0o755)
    sidecar = helper.with_name(helper.name + ".source-sha256")
    sidecar.write_text(hi._source_fingerprint(src), encoding="utf-8")
    prebuilt_bytes = helper.read_bytes()
    prebuilt_ino = helper.stat().st_ino

    monkeypatch.setattr(
        hi,
        "_run_launchctl",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    result = hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    # Helper bytes and inode are unchanged — same physical file,
    # so cdhash is identical and TCC permission remains valid.
    assert helper.read_bytes() == prebuilt_bytes
    assert helper.stat().st_ino == prebuilt_ino
    assert result.get("rebuilt") is False


def test_install_rebuilds_when_source_hash_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the Swift source content has changed (upgrade path), install()
    must recompile so a stale on-disk helper from a previous agent-doctor
    version gets the current Swift source compiled in. Content-based so
    it survives mtime perturbations (tarball restore, scp -p, etc.) —
    motivated by codex review feedback against the initial mtime-only
    implementation.
    """

    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    helper = tmp_path / "helper"
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", helper)
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    src = tmp_path / "HotkeyHelper.swift"
    monkeypatch.setattr(hi, "SWIFT_SOURCE", src)
    helper.write_text("#!/bin/sh\necho stale\n")
    helper.chmod(0o755)
    stale_bytes = helper.read_bytes()
    # Old sidecar recorded a different hash (last build was from a
    # different source revision).
    sidecar = helper.with_name(helper.name + ".source-sha256")
    sidecar.write_text("0" * 64, encoding="utf-8")
    # Current source content differs from what built the on-disk helper.
    src.write_text("// upgraded source")

    monkeypatch.setattr(
        hi,
        "_run_launchctl",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    result = hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    assert helper.read_bytes() != stale_bytes  # fake swiftc rewrote it
    assert result.get("rebuilt") is True
    # Post-rebuild the sidecar records the new source hash.
    assert sidecar.read_text(encoding="utf-8").strip() == hi._source_fingerprint(src)


def test_install_rebuilds_when_sidecar_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing helper without a sidecar (e.g. left over from a
    pre-v0.4 install) is treated as unknown provenance and rebuilt. After
    one rebuild the sidecar is written and subsequent toggles are
    idempotent.
    """

    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    helper = tmp_path / "helper"
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", helper)
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    src = tmp_path / "HotkeyHelper.swift"
    monkeypatch.setattr(hi, "SWIFT_SOURCE", src)
    src.write_text("// source")
    helper.write_text("#!/bin/sh\necho legacy\n")
    helper.chmod(0o755)
    sidecar = helper.with_name(helper.name + ".source-sha256")
    assert not sidecar.exists()

    monkeypatch.setattr(
        hi,
        "_run_launchctl",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    result = hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    assert result.get("rebuilt") is True
    # Sidecar was written so the next call can be idempotent.
    assert sidecar.exists()
    assert sidecar.read_text(encoding="utf-8").strip() == hi._source_fingerprint(src)


def test_install_rebuilds_when_helper_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    helper = tmp_path / "helper"
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", helper)
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    src = tmp_path / "HotkeyHelper.swift"
    monkeypatch.setattr(hi, "SWIFT_SOURCE", src)
    src.write_text("// source")
    assert not helper.exists()

    monkeypatch.setattr(
        hi,
        "_run_launchctl",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    result = hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    assert helper.exists()
    assert result.get("rebuilt") is True


def test_install_rebuilds_when_helper_lost_executable_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a previous tarball restore or scp -p stripped the executable
    bit, ``install()`` must rebuild so launchd doesn't bootstrap a
    non-executable helper. Caught by codex review on the initial
    mtime-only check.
    """

    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    helper = tmp_path / "helper"
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", helper)
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    src = tmp_path / "HotkeyHelper.swift"
    monkeypatch.setattr(hi, "SWIFT_SOURCE", src)
    src.write_text("// source")
    # Helper exists and is "fresh" by mtime but missing the executable bit.
    helper.write_text("#!/bin/sh\necho broken\n")
    helper.chmod(0o644)
    import os
    src_mtime = src.stat().st_mtime - 60
    os.utime(src, (src_mtime, src_mtime))

    monkeypatch.setattr(
        hi,
        "_run_launchctl",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    result = hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    assert result.get("rebuilt") is True
    # build() chmods 0o755, so post-install the helper is executable.
    assert helper.stat().st_mode & 0o100


def test_install_rebuilds_when_helper_is_not_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive: if the helper path resolves to a directory (e.g. user
    rm'd the file and ran `mkdir` by mistake while debugging), install()
    cannot reuse it. ``_should_rebuild`` must signal a rebuild rather
    than silently bootstrap launchd on a non-executable target.
    """

    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    helper = tmp_path / "helper"
    # Simulate "not a regular file" via a directory at the helper path.
    # _should_rebuild must short-circuit before we ever call swiftc to
    # overwrite it, so we use a no-op fake swiftc that just records calls.
    helper.mkdir()
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", helper)
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    src = tmp_path / "HotkeyHelper.swift"
    monkeypatch.setattr(hi, "SWIFT_SOURCE", src)
    src.write_text("// source")

    # We only check that _should_rebuild reported "rebuild needed" — the
    # actual swiftc call would fail on a directory target, but that's
    # acceptable: the user gets a clear HotkeyInstallError instead of a
    # silently bootstrapped broken daemon.
    assert hi._should_rebuild(src, helper) is True


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


def test_install_preserves_existing_agent_doctor_bin_when_none_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When agent_doctor_bin=None and a plist exists, install() must
    preserve the existing AGENT_DOCTOR_BIN env var instead of falling
    back to `which agent-doctor`. This protects power users who installed
    with --agent-doctor-bin /custom/... and later pause/resume via the UI.
    """

    # Set up an existing plist with a custom binary path.
    plist_path = tmp_path / "existing.plist"
    plistlib.dump(
        {
            "Label": "com.agent-doctor.hotkey",
            "ProgramArguments": ["/tmp/old-helper"],
            "EnvironmentVariables": {"AGENT_DOCTOR_BIN": "/custom/path/agent-doctor"},
        },
        plist_path.open("wb"),
    )
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", plist_path)
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", tmp_path / "helper")

    bin_dir = _make_fake_swiftc(tmp_path)
    import os
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(hi, "SWIFT_SOURCE", tmp_path / "src.swift")
    (tmp_path / "src.swift").write_text("// fake")
    monkeypatch.setattr(
        hi,
        "_run_launchctl",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, b"", b""),
    )

    result = hi.install()  # no agent_doctor_bin kwarg
    assert result["agent_doctor_bin"] == "/custom/path/agent-doctor"

    # The written plist must also contain the preserved custom path.
    parsed = plistlib.loads(plist_path.read_bytes())
    assert (
        parsed["EnvironmentVariables"]["AGENT_DOCTOR_BIN"]
        == "/custom/path/agent-doctor"
    )
