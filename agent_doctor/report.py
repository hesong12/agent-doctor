"""Report writers for scan results."""

from __future__ import annotations

import json
from pathlib import Path

from .schema import Finding, Message, ScanResult


def write_reports(out_dir: Path, messages: list[Message], findings: list[Finding]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = ScanResult(messages=messages, findings=findings)

    findings_path = out_dir / "findings.json"
    report_path = out_dir / "report.md"
    eval_path = out_dir / "eval-cases.yaml"

    findings_path.write_text(
        json.dumps([finding.to_dict() for finding in findings], indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    report_path.write_text(_render_markdown(result), encoding="utf-8")
    eval_path.write_text(_render_eval_cases(findings), encoding="utf-8")

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
                f"  - id: {finding.id}",
                f"    failure_mode: {finding.failure_mode}",
                f"    name: {case.get('name', '')}",
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
