"""Tests for hotkey_install build / plist / launchctl wiring."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_doctor import hotkey_install as hi


@pytest.fixture
def stub_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real openssl/security/codesign shell-outs in install()
    tests. The signing path is exercised by its own dedicated tests
    (test_sign_helper_*, test_ensure_signing_identity_*); install-flow
    tests just need the signing hook to be a no-op so they can run on
    machines without the cert provisioned.
    """

    monkeypatch.setattr(hi, "ensure_signing_identity", lambda *_a, **_kw: None)
    monkeypatch.setattr(hi, "sign_helper", lambda *_a, **_kw: None)


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
    monkeypatch.setattr(hi, "ensure_signing_identity", lambda *_a, **_kw: None)
    monkeypatch.setattr(hi, "sign_helper", lambda *_a, **_kw: None)

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_signing: None
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_signing: None
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_signing: None
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_signing: None
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_signing: None
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_signing: None
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


def test_install_signs_helper_when_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install() must call ensure_signing_identity() + sign_helper() right
    after every successful rebuild. Without that the freshly-built
    binary is adhoc-signed (default swiftc behaviour) and TCC tracks it
    by cdhash, defeating the stable-identity contract this PR ships.
    """

    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    helper = tmp_path / "helper"
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", helper)
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    src = tmp_path / "HotkeyHelper.swift"
    monkeypatch.setattr(hi, "SWIFT_SOURCE", src)
    src.write_text("// source")

    signing_calls: list[str] = []
    monkeypatch.setattr(
        hi, "ensure_signing_identity",
        lambda *_a, **_kw: signing_calls.append("ensure")
    )
    monkeypatch.setattr(
        hi, "sign_helper",
        lambda *_a, **_kw: signing_calls.append("sign")
    )
    monkeypatch.setattr(
        hi,
        "_run_launchctl",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    assert signing_calls == ["ensure", "sign"]


def test_install_does_not_sign_when_marker_already_records_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a sibling ``<helper>.signed-by`` marker already records the
    stable identity, install() must not re-sign. Re-signing changes
    cdhash, which is fine for TCC (it tracks by identity, not cdhash)
    but pointless work — and on a future agent-doctor that introduces
    notarization or stapling, idempotency matters more.
    """

    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    helper = tmp_path / "helper"
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", helper)
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    src = tmp_path / "HotkeyHelper.swift"
    monkeypatch.setattr(hi, "SWIFT_SOURCE", src)
    src.write_text("// source")
    helper.write_text("#!/bin/sh\necho prebuilt\n")
    helper.chmod(0o755)
    sidecar = helper.with_name(helper.name + ".source-sha256")
    sidecar.write_text(hi._source_fingerprint(src), encoding="utf-8")
    # Marker says we've already signed with the stable identity.
    helper.with_name(helper.name + ".signed-by").write_text(
        hi.SIGNING_IDENTITY, encoding="utf-8"
    )

    signing_calls: list[str] = []
    monkeypatch.setattr(
        hi, "ensure_signing_identity",
        lambda *_a, **_kw: signing_calls.append("ensure")
    )
    monkeypatch.setattr(
        hi, "sign_helper",
        lambda *_a, **_kw: signing_calls.append("sign")
    )
    monkeypatch.setattr(
        hi,
        "_run_launchctl",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    result = hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    assert signing_calls == []  # neither call fired
    assert result.get("signed") is False


def test_install_signs_existing_unsigned_helper_for_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user on PR #35 with an adhoc-signed helper (no signature marker)
    must get migrated to stable signing without first forcing a
    recompile. This is the upgrade path that converts adhoc-cdhash-based
    TCC tracking to (cert, identifier)-based TCC tracking.
    """

    bin_dir = _make_fake_swiftc(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    helper = tmp_path / "helper"
    monkeypatch.setattr(hi, "DEFAULT_HELPER_PATH", helper)
    monkeypatch.setattr(hi, "DEFAULT_PLIST_PATH", tmp_path / "plist")
    src = tmp_path / "HotkeyHelper.swift"
    monkeypatch.setattr(hi, "SWIFT_SOURCE", src)
    src.write_text("// source")
    # Helper from a previous (PR #35) install — fresh sidecar but no
    # signature marker.
    helper.write_text("#!/bin/sh\necho prebuilt\n")
    helper.chmod(0o755)
    sidecar = helper.with_name(helper.name + ".source-sha256")
    sidecar.write_text(hi._source_fingerprint(src), encoding="utf-8")
    marker = helper.with_name(helper.name + ".signed-by")
    assert not marker.exists()

    signing_calls: list[str] = []
    monkeypatch.setattr(
        hi, "ensure_signing_identity",
        lambda *_a, **_kw: signing_calls.append("ensure")
    )
    monkeypatch.setattr(
        hi, "sign_helper",
        lambda *_a, **_kw: signing_calls.append("sign")
    )
    monkeypatch.setattr(
        hi,
        "_run_launchctl",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    result = hi.install(agent_doctor_bin="/usr/local/bin/agent-doctor")
    assert signing_calls == ["ensure", "sign"]
    assert result.get("rebuilt") is False  # skipped per PR #35 sidecar match
    assert result.get("signed") is True
    # Marker was written.
    assert marker.read_text(encoding="utf-8").strip() == hi.SIGNING_IDENTITY


def test_ensure_signing_identity_skips_when_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user (or a previous install) already has the cert, the
    openssl/security shell-outs are pure overhead and must not run.
    """

    monkeypatch.setattr(
        hi, "_signing_identity_exists", lambda *_a, **_kw: True
    )
    openssl_called = [False]

    def boom_subprocess(*_a: Any, **_kw: Any) -> subprocess.CompletedProcess:
        openssl_called[0] = True
        raise AssertionError("ensure_signing_identity must short-circuit")

    monkeypatch.setattr(subprocess, "run", boom_subprocess)
    hi.ensure_signing_identity()
    assert openssl_called[0] is False


def test_sign_helper_invokes_codesign_with_stable_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codesign argv MUST include the stable bundle identifier — TCC keys
    on it as part of the (cert, identifier) tuple. Drift here would
    silently re-invalidate every user's grant.
    """

    helper = tmp_path / "helper"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codesign")
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    hi.sign_helper(helper)
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "/usr/bin/codesign"
    assert "--force" in argv
    assert "-s" in argv
    assert hi.SIGNING_IDENTITY in argv
    assert "--identifier" in argv
    assert hi.HELPER_BUNDLE_ID in argv
    assert str(helper) == argv[-1]


def test_sign_helper_surfaces_codesign_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signing failure must raise. Silently continuing would ship an
    adhoc-signed helper after we promised the caller a stable signature,
    which would silently regress this PR's whole purpose.
    """

    helper = tmp_path / "helper"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codesign")

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 1, b"", b"keychain locked")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(hi.HotkeyInstallError, match="codesign failed"):
        hi.sign_helper(helper)


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_signing: None
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
