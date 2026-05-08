"""Tests for the closed-loop CLI commands."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_agent_doctor(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "agent_doctor.cli"] + args
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_patches_list_returns_empty_when_none_applied(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["patches", "list", "--json"], env=env)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    payload = json.loads(result.stdout or "[]")
    assert payload == []


def test_patches_list_text_output_when_empty(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["patches", "list"], env=env)
    assert result.returncode == 0
    # Some indication of "no patches" — either text or empty
    assert result.stdout is not None


def test_undo_with_unknown_id_returns_error(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["undo", "nope-12"], env=env)
    assert result.returncode != 0


def test_approve_unknown_id_returns_error(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["approve", "no-such-id"], env=env)
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_dismiss_unknown_id_returns_error(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["dismiss", "no-such-id"], env=env)
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_redraft_unknown_id_returns_error(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["redraft", "no-such-id"], env=env)
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_dismiss_marks_proposal_dismissed(tmp_path: Path) -> None:
    """Write a fake proposal, run dismiss, verify state changed."""
    import time
    proposals_dir = tmp_path / ".agent-doctor" / "generic"
    proposals_dir.mkdir(parents=True)
    proposals_path = proposals_dir / "proposals.jsonl"
    proposals_path.write_text(json.dumps({
        "id": "p-test-dismiss",
        "session_id": "s",
        "finding_id": "f",
        "target_kind": "memory",
        "target_file_hint": "",
        "patch_body": "- x",
        "reason_summary": "x",
        "baseline_hash": None,
        "state": "pending",
        "message_id": None,
        "target_host": "generic",
        "target_channel": "inbox",
        "target_recipient": "",
        "created_at": time.time(),
        "ttl_at": time.time() + 3600,
        "resolved_at": None,
    }) + "\n", encoding="utf-8")

    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["dismiss", "p-test-dismiss"], env=env)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # Verify state changed
    raw = proposals_path.read_text(encoding="utf-8")
    line = json.loads([l for l in raw.splitlines() if l.strip()][0])
    assert line["state"] == "dismissed"
    assert line["resolved_at"] is not None


def test_redraft_marks_proposal_refining(tmp_path: Path) -> None:
    import time
    proposals_dir = tmp_path / ".agent-doctor" / "generic"
    proposals_dir.mkdir(parents=True)
    proposals_path = proposals_dir / "proposals.jsonl"
    proposals_path.write_text(json.dumps({
        "id": "p-test-redraft",
        "session_id": "s", "finding_id": "f", "target_kind": "memory",
        "target_file_hint": "", "patch_body": "- x", "reason_summary": "x",
        "baseline_hash": None, "state": "pending", "message_id": None,
        "target_host": "generic", "target_channel": "inbox", "target_recipient": "",
        "created_at": time.time(), "ttl_at": time.time() + 3600, "resolved_at": None,
    }) + "\n", encoding="utf-8")

    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["redraft", "p-test-redraft"], env=env)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    raw = proposals_path.read_text(encoding="utf-8")
    line = json.loads([l for l in raw.splitlines() if l.strip()][0])
    assert line["state"] == "refining"


def test_undo_signature_parses(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["undo", "--help"], env=env)
    assert result.returncode == 0


def test_patches_list_reads_patch_log_jsonl(tmp_path: Path) -> None:
    """When patch-log.jsonl exists, patches list shows entries."""
    patch_log = tmp_path / ".agent-doctor" / "patch-log.jsonl"
    patch_log.parent.mkdir(parents=True, exist_ok=True)
    patch_log.write_text(
        json.dumps({
            "id": "p-abc",
            "target_file": "/tmp/MEMORY.md",
            "applied_at": 1234.0,
            "backup_path": "/tmp/backup.bak",
        }) + "\n",
        encoding="utf-8",
    )

    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run_agent_doctor(["patches", "list", "--json"], env=env)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    assert payload[0]["id"] == "p-abc"
