import json
import subprocess
import sys
from pathlib import Path

from agent_doctor.detectors import detect_findings
from agent_doctor.ingest import ingest_path
from agent_doctor.install import install_skill
from agent_doctor.report import write_reports


FIXTURES = Path(__file__).parent / "fixtures"


def test_report_files_are_written(tmp_path: Path) -> None:
    messages = ingest_path(FIXTURES)
    findings = detect_findings(messages)

    paths = write_reports(tmp_path, messages, findings)

    assert paths["report"].read_text(encoding="utf-8").startswith("# Agent Doctor Report")
    findings_json = json.loads(paths["findings"].read_text(encoding="utf-8"))
    assert findings_json[0]["evidence"][0]["quote"]
    eval_cases = paths["eval_cases"].read_text(encoding="utf-8")
    assert "cases:" in eval_cases
    assert "failure_mode:" in eval_cases


def test_install_skill_writes_safe_text(tmp_path: Path) -> None:
    path = install_skill("hermes", tmp_path)
    text = path.read_text(encoding="utf-8")

    assert "agent-doctor scan --hermes" in text
    assert "local-only" in text
    assert "dry-run by default" in text
    assert "Ask before applying patches" in text
    assert "Never paste full transcripts to a remote LLM" in text


def test_cli_doctor_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent_doctor.cli", "doctor"],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Agent Doctor:" in result.stdout
    assert "Privacy mode: local-only" in result.stdout


def test_cli_scan_smoke(tmp_path: Path) -> None:
    result = subprocess.run(
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
            str(tmp_path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    summary = json.loads(result.stdout)

    assert summary["messages"] == 10
    assert summary["sessions"] == 3
    assert summary["findings"] >= 6
    assert (tmp_path / "report.md").exists()
    assert (tmp_path / "findings.json").exists()
    assert (tmp_path / "eval-cases.yaml").exists()


def test_cli_install_skill_smoke(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_doctor.cli",
            "install-skill",
            "--target",
            "openclaw",
            "--out",
            str(tmp_path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "agent-doctor-openclaw-sop.md" in result.stdout
    assert (tmp_path / "agent-doctor-openclaw-sop.md").exists()
