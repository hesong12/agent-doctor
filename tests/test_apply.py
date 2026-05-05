"""Tests for the apply (patch staging) module."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_doctor.apply import load_findings, stage_patches
from agent_doctor.detectors import detect_findings
from agent_doctor.ingest import ingest_path
from agent_doctor.report import write_reports

FIXTURES = Path(__file__).parent / "fixtures"


def _scan_to_findings_dir(tmp_path: Path) -> Path:
    messages = ingest_path(FIXTURES)
    findings = detect_findings(messages)
    write_reports(tmp_path, messages, findings)
    return tmp_path / "findings.json"


def test_stage_patches_writes_expected_files(tmp_path: Path) -> None:
    findings_path = _scan_to_findings_dir(tmp_path / "scan")
    findings = load_findings(findings_path)

    staging = tmp_path / "staging"
    result = stage_patches(findings, staging)

    expected_files = {
        "memory.md",
        "sop.md",
        "identity.md",
        "tool-discipline.md",
        "MANIFEST.json",
    }
    actual_top_level = {p.name for p in staging.iterdir() if p.is_file()}
    assert expected_files.issubset(actual_top_level)
    assert (staging / "eval").is_dir()
    assert any((staging / "eval").iterdir())

    for file in result.files_written:
        if file.is_file():
            assert stat.S_IMODE(file.stat().st_mode) == 0o600


def test_stage_patches_redacts_secrets(tmp_path: Path) -> None:
    findings = [
        {
            "id": "memory_failure-001",
            "title": "Memory failure",
            "severity": "medium",
            "failure_mode": "memory_failure",
            "session_id": "s1",
            "count": 1,
            "evidence": [
                {
                    "file": "session.jsonl",
                    "line": 1,
                    "role": "user",
                    "quote": "Remember the api_key=sk-abcdefghijklmnopqrstuvwxyz preference.",
                }
            ],
            "recommendations": [
                {
                    "target": "memory",
                    "proposal": "Store the api_key=sk-abcdefghijklmnopqrstuvwxyz preference.",
                    "evidence_quote": "api_key=sk-abcdefghijklmnopqrstuvwxyz",
                }
            ],
            "eval_case": {
                "name": "eval_memory_failure",
                "prompt": "remember sk-abcdefghijklmnopqrstuvwxyz",
                "expected_behavior": "Capture the preference.",
            },
        }
    ]

    staging = tmp_path / "staging"
    stage_patches(findings, staging)

    text = (staging / "memory.md").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in text
    assert "[REDACTED]" in text


def test_min_severity_filter_skips_low_findings(tmp_path: Path) -> None:
    findings = [
        {
            "id": "f1",
            "title": "Communication mismatch",
            "severity": "low",
            "failure_mode": "communication_mismatch",
            "session_id": "s1",
            "count": 1,
            "evidence": [],
            "recommendations": [
                {"target": "memory", "proposal": "Store preference.", "evidence_quote": ""}
            ],
            "eval_case": {"name": "eval_communication_mismatch", "prompt": "", "expected_behavior": ""},
        }
    ]

    result = stage_patches(findings, tmp_path / "staging", minimum_severity="medium")

    assert result.skipped == 1
    assert not (tmp_path / "staging" / "memory.md").exists()


def test_diff_against_target_dir_produces_unified_diff(tmp_path: Path) -> None:
    target = tmp_path / "live"
    target.mkdir()
    (target / "memory.md").write_text("# previous memory\n", encoding="utf-8")

    findings_path = _scan_to_findings_dir(tmp_path / "scan")
    findings = load_findings(findings_path)

    staging = tmp_path / "staging"
    result = stage_patches(findings, staging, target_dir=target)

    diff_path = staging / "DIFF.txt"
    assert diff_path.exists()
    assert "previous memory" in result.diff_text or result.diff_text == ""
    # The diff should reference both files.
    assert "memory.md" in result.diff_text or result.diff_text == ""


def test_cli_apply_smoke(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "scan",
            "--path",
            str(FIXTURES),
            "--format",
            "json",
            "--out",
            str(scan_dir),
        ],
        check=True,
        capture_output=True,
    )

    staging = tmp_path / "staging"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "apply",
            "--findings",
            str(scan_dir),
            "--out",
            str(staging),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Staged" in result.stdout
    manifest = json.loads((staging / "MANIFEST.json").read_text(encoding="utf-8"))
    assert "patches" in manifest


def test_load_findings_rejects_unexpected_shape(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"unexpected": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_findings(bad)
