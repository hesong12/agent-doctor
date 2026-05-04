"""Report writers for scan results."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .redaction import redact_text, redact_value
from .schema import Finding, Message, ScanResult


def write_reports(out_dir: Path, messages: list[Message], findings: list[Finding]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = ScanResult(messages=messages, findings=findings)

    findings_path = out_dir / "findings.json"
    report_path = out_dir / "report.md"
    eval_path = out_dir / "eval-cases.yaml"

    findings_payload = redact_value([finding.to_dict() for finding in findings])
    _write_private_text(
        findings_path,
        json.dumps(findings_payload, indent=2, ensure_ascii=False) + "\n",
    )
    _write_private_text(report_path, redact_text(_render_markdown(result)))
    _write_private_text(eval_path, redact_text(_render_eval_cases(findings)))

    return {
        "report": report_path,
        "findings": findings_path,
        "eval_cases": eval_path,
    }


def render_summary(messages: list[Message], findings: list[Finding]) -> str:
    result = ScanResult(messages=messages, findings=findings)
    return (
        f"Scanned {len(messages)} messages across {result.session_count} session(s). "
        f"Found {len(findings)} finding(s)."
    )


def render_json_summary(messages: list[Message], findings: list[Finding], paths: dict[str, Path]) -> str:
    result = ScanResult(messages=messages, findings=findings)
    payload = {
        "messages": len(messages),
        "sessions": result.session_count,
        "findings": len(findings),
        "outputs": {name: str(path) for name, path in paths.items()},
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _render_markdown(result: ScanResult) -> str:
    lines = [
        "# Agent Doctor Report",
        "",
        "## Summary",
        "",
        f"- Messages scanned: {len(result.messages)}",
        f"- Sessions scanned: {result.session_count}",
        f"- Findings: {len(result.findings)}",
        "",
        "## Privacy",
        "",
        "Evidence quotes are redacted by default, but they are still transcript excerpts. Review artifacts before sharing them outside your machine.",
        "",
        "## Findings",
        "",
    ]
    if not result.findings:
        lines.append("No deterministic findings detected.")
        lines.append("")
        return "\n".join(lines)

    for finding in result.findings:
        lines.extend(
            [
                f"### {finding.id}: {finding.title}",
                "",
                f"- Severity: {finding.severity}",
                f"- Failure mode: `{finding.failure_mode}`",
                f"- Confidence: {finding.confidence:.2f}",
                f"- Session: `{finding.session_id}`",
                "",
                f"Diagnosis: {finding.diagnosis}",
                "",
                "Evidence:",
            ]
        )
        for item in finding.evidence:
            lines.append(f"- `{item.file}:{item.line}` {item.role}: \"{item.quote}\"")
        lines.extend(["", "Recommendations:"])
        for recommendation in finding.recommendations:
            target = recommendation.get("target", "review")
            proposal = recommendation.get("proposal", "")
            lines.append(f"- `{target}`: {proposal}")
        lines.append("")
    return "\n".join(lines)


def _render_eval_cases(findings: list[Finding]) -> str:
    lines = ["cases:"]
    if not findings:
        lines.append("  []")
        return "\n".join(lines) + "\n"
    for finding in findings:
        case = finding.eval_case
        lines.extend(
            [
                f"  - id: {_yaml_scalar(finding.id)}",
                f"    failure_mode: {_yaml_scalar(finding.failure_mode)}",
                f"    name: {_yaml_scalar(case.get('name', ''))}",
                "    prompt: |-",
            ]
        )
        lines.extend(_block(case.get("prompt", ""), indent="      "))
        lines.append("    expected_behavior: |-")
        lines.extend(_block(case.get("expected_behavior", ""), indent="      "))
    return "\n".join(lines) + "\n"


def _block(text: str, indent: str) -> list[str]:
    if not text:
        return [indent]
    return [indent + line for line in text.splitlines()]


def _yaml_scalar(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _write_private_text(path: Path, text: str) -> None:
    try:
        os.chmod(path, 0o600)
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        os.chmod(path, 0o600)
