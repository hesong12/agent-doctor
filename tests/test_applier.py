"""Tests for applier."""
import hashlib
import time
from pathlib import Path

import pytest

from agent_doctor.adapters import OpenClawAdapter
from agent_doctor.applier import (
    AppliedPatch,
    apply_proposal,
    backup_target,
    undo_patch,
)
from agent_doctor.proposer import Proposal


def _proposal(
    target_kind: str = "memory",
    baseline_hash: str | None = None,
    body: str = "- entry",
) -> Proposal:
    return Proposal(
        id="p-test",
        session_id="s-1",
        finding_id="f-1",
        target_kind=target_kind,
        target_file_hint="",
        patch_body=body,
        reason_summary="x",
        baseline_hash=baseline_hash,
        state="pending",
        message_id="msg-1",
        target_host="openclaw",
        target_channel="tui",
        target_recipient="local",
        created_at=time.time(),
        ttl_at=time.time() + 3600,
    )


def test_apply_appends_memory_entry(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "openclaw-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    adapter = OpenClawAdapter()
    proposal = _proposal(target_kind="memory", body="- User dislikes verbose output.")

    result = apply_proposal(proposal, adapter)

    assert result.state == "applied"
    memory = home / "memory" / "MEMORY.md"
    assert memory.exists()
    text = memory.read_text(encoding="utf-8")
    assert "User dislikes verbose output." in text


def test_apply_creates_target_with_perms_when_missing(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "openclaw-home"
    home.mkdir()
    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    adapter = OpenClawAdapter()
    apply_proposal(_proposal(target_kind="memory", body="- item"), adapter)

    memory = home / "memory" / "MEMORY.md"
    import stat
    assert stat.S_IMODE(memory.stat().st_mode) == 0o600


def test_apply_backs_up_target_before_writing(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "openclaw-home"
    home.mkdir()
    (home / "memory").mkdir(parents=True)
    memory = home / "memory" / "MEMORY.md"
    memory.write_text("# old content\n", encoding="utf-8")

    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    adapter = OpenClawAdapter()
    result = apply_proposal(_proposal(target_kind="memory", body="- new"), adapter)

    assert result.state == "applied"
    backup = result.backup_path
    assert backup is not None
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "# old content\n"


def test_apply_detects_baseline_hash_conflict(tmp_path: Path, monkeypatch) -> None:
    """Edit-style patch (identity) — if file changed since draft, conflict."""
    home = tmp_path / "openclaw-home"
    home.mkdir()
    (home / "identity").mkdir(parents=True)
    identity = home / "identity" / "identity.md"
    identity.write_text("# original identity\n", encoding="utf-8")

    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Proposal carries OLD baseline hash
    stale_hash = hashlib.sha256(b"# stale identity\n").hexdigest()
    proposal = _proposal(target_kind="identity", baseline_hash=stale_hash, body="patch")

    adapter = OpenClawAdapter()
    result = apply_proposal(proposal, adapter)

    assert result.state == "conflict"
    # File should be unchanged
    assert identity.read_text(encoding="utf-8") == "# original identity\n"


def test_apply_memory_kind_skips_baseline_hash(tmp_path: Path, monkeypatch) -> None:
    """Memory is append-only — even if baseline_hash is set, it's ignored."""
    home = tmp_path / "openclaw-home"
    home.mkdir()
    (home / "memory").mkdir(parents=True)
    memory = home / "memory" / "MEMORY.md"
    memory.write_text("# original\n", encoding="utf-8")

    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Even with a stale hash, memory should still apply
    stale_hash = hashlib.sha256(b"unrelated").hexdigest()
    proposal = _proposal(target_kind="memory", baseline_hash=stale_hash, body="- new entry")

    adapter = OpenClawAdapter()
    result = apply_proposal(proposal, adapter)

    assert result.state == "applied"
    assert "new entry" in memory.read_text(encoding="utf-8")


def test_undo_restores_target_from_backup(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "openclaw-home"
    home.mkdir()
    (home / "memory").mkdir(parents=True)
    memory = home / "memory" / "MEMORY.md"
    memory.write_text("# original\n", encoding="utf-8")

    monkeypatch.setattr("agent_doctor.adapters.openclaw.OPENCLAW_HOME", home)
    monkeypatch.setattr("agent_doctor.adapters.openclaw._resolve_openclaw_or_none", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    adapter = OpenClawAdapter()
    proposal = _proposal(target_kind="memory", body="- entry")
    applied = apply_proposal(proposal, adapter)
    assert applied.state == "applied"
    assert "entry" in memory.read_text(encoding="utf-8")

    undo_patch(applied.patch_id, applied.backup_path, applied.target_file)

    # Original content should be restored
    assert memory.read_text(encoding="utf-8") == "# original\n"


def test_apply_without_writable_target_returns_degraded(tmp_path: Path, monkeypatch) -> None:
    """SOP kind on OpenClaw with sop_writable=None → state=degraded_to_staging."""
    from agent_doctor.adapters import GenericAdapter

    monkeypatch.setenv("HOME", str(tmp_path))

    adapter = GenericAdapter()  # no host-specific writable surfaces
    proposal = _proposal(target_kind="sop", body="patch")

    result = apply_proposal(proposal, adapter)

    assert result.state == "degraded_to_staging"
    assert result.error is not None


def test_backup_target_writes_restore_sh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "MEMORY.md"
    target.write_text("# content\n", encoding="utf-8")

    backup = backup_target(target, "patch-x")

    assert backup.exists()
    restore_sh = backup.parent / "restore.sh"
    assert restore_sh.exists()
    import stat
    assert stat.S_IMODE(restore_sh.stat().st_mode) & 0o100  # executable bit set


def test_undo_raises_when_backup_missing(tmp_path: Path) -> None:
    target = tmp_path / "x"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="backup not found"):
        undo_patch("nope", tmp_path / "missing.bak", target)
