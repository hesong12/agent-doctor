"""Tests for digester."""
import json
import time
from pathlib import Path

import pytest

from agent_doctor.digester import WeeklyDigest, build_weekly_digest


def test_build_digest_returns_zero_when_no_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    digest = build_weekly_digest("openclaw")
    assert isinstance(digest, WeeklyDigest)
    assert digest.events == 0
    assert digest.proposed == 0
    assert digest.applied == 0
    assert digest.measured_better == 0
    assert digest.host == "openclaw"


def test_build_digest_counts_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / ".agent-doctor" / "openclaw"
    out.mkdir(parents=True)
    events_path = out / "events.jsonl"
    now = time.time()
    events_path.write_text(
        "\n".join([
            json.dumps({"id": "e1", "platform": "openclaw", "trigger": "user_frustration_signal", "session_id": "s1", "ts": now - 3600}),
            json.dumps({"id": "e2", "platform": "openclaw", "trigger": "tool_failure_or_hidden_error", "session_id": "s2", "ts": now - 7200}),
            json.dumps({"id": "e3", "platform": "openclaw", "trigger": "user_frustration_signal", "session_id": "s3", "ts": now - (8 * 86400)}),  # too old
        ]) + "\n",
        encoding="utf-8",
    )

    digest = build_weekly_digest("openclaw", since_ts=now - 7 * 86400)
    assert digest.events == 2  # only the 2 within 7 days


def test_build_digest_counts_proposals_and_applies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / ".agent-doctor" / "openclaw"
    out.mkdir(parents=True)
    now = time.time()
    proposals = out / "proposals.jsonl"
    proposals.write_text(
        "\n".join([
            json.dumps({
                "id": f"p{i}",
                "session_id": "s1",
                "finding_id": "f1",
                "target_kind": "memory",
                "target_file_hint": "",
                "patch_body": "x",
                "reason_summary": "x",
                "baseline_hash": None,
                "state": state,
                "message_id": None,
                "target_host": "openclaw",
                "target_channel": "inbox",
                "target_recipient": "",
                "created_at": now - 3600,
                "ttl_at": now + 3600,
                "resolved_at": None,
            })
            for i, state in enumerate(["pending", "applied", "applied", "dismissed"])
        ]) + "\n",
        encoding="utf-8",
    )

    digest = build_weekly_digest("openclaw", since_ts=now - 7 * 86400)
    assert digest.proposed == 4
    assert digest.applied == 2
    assert digest.dismissed == 1


def test_build_digest_counts_measured_better(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / ".agent-doctor" / "openclaw"
    out.mkdir(parents=True)
    now = time.time()
    measurements = out / "measurements.jsonl"
    measurements.write_text(
        "\n".join([
            json.dumps({"patch_id": "p1", "session_id": "s1", "target_kind": "memory", "score": 0.9, "judged_by_model": "x", "rationale": "y", "measured_at": now}),
            json.dumps({"patch_id": "p2", "session_id": "s1", "target_kind": "memory", "score": 0.5, "judged_by_model": "x", "rationale": "y", "measured_at": now}),
            json.dumps({"patch_id": "p3", "session_id": "s1", "target_kind": "memory", "score": 0.85, "judged_by_model": "x", "rationale": "y", "measured_at": now}),
        ]) + "\n",
        encoding="utf-8",
    )

    digest = build_weekly_digest("openclaw", since_ts=now - 7 * 86400)
    assert digest.measured_better == 2  # 0.9 and 0.85 are >= 0.7


def test_build_digest_top_patterns_returns_most_common(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / ".agent-doctor" / "openclaw"
    out.mkdir(parents=True)
    now = time.time()
    events = out / "events.jsonl"
    rows = (
        [json.dumps({"id": f"e{i}", "trigger": "user_frustration_signal", "session_id": f"s{i}", "ts": now}) for i in range(5)]
        + [json.dumps({"id": f"e{i+10}", "trigger": "memory_failure", "session_id": f"s{i+10}", "ts": now}) for i in range(3)]
        + [json.dumps({"id": f"e{i+20}", "trigger": "tool_failure_or_hidden_error", "session_id": f"s{i+20}", "ts": now}) for i in range(1)]
    )
    events.write_text("\n".join(rows) + "\n", encoding="utf-8")

    digest = build_weekly_digest("openclaw", since_ts=now - 7 * 86400)
    assert digest.top_patterns[0] == "user_frustration_signal"  # most common


def test_digest_cli_now_runs_and_prints(tmp_path: Path) -> None:
    """`agent-doctor digest --now --host generic` should run without error."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli", "digest", "--now", "--host", "generic"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # Should print something containing 🩺 or "digest" or counts
    assert "🩺" in result.stdout or "digest" in result.stdout.lower() or "0" in result.stdout
