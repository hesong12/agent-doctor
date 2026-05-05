"""Report writers for scan results.

Three artifacts come out of every scan:

- ``report.md`` — human-readable findings with redacted evidence quotes.
- ``findings.json`` — machine-readable findings for downstream tooling.
- ``eval-cases.yaml`` — starter eval cases mirroring detected failure modes.

All three are written with ``0o600`` permissions and pass through the same
secret-redaction pipeline as the in-memory representation. Reports include the
aggregated ``count`` per finding so reviewers can see at a glance whether a
pattern is a one-off or a structural problem.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .redaction import redact_text, redact_value
from .schema import Finding, Message, ScanResult


def write_reports(
    out_dir: Path,
    messages: list[Message],
    findings: list[Finding],
    *,
    parse_errors: int = 0,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = ScanResult(messages=messages, findings=findings, parse_errors=parse_errors)

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


def render_summary(
    messages: list[Message],
    findings: list[Finding],
    *,
    parse_errors: int = 0,
) -> str:
    result = ScanResult(messages=messages, findings=findings, parse_errors=parse_errors)
    severity_counts = _severity_counts(findings)
    parts = [
        f"Scanned {len(messages)} messages across {result.session_count} session(s).",
        f"Found {len(findings)} finding(s).",
    ]
    if findings:
        parts.append(
            "Severity: "
            + ", ".join(
                f"{count} {label}"
                for label, count in severity_counts.items()
                if count
            )
            + "."
        )
    if parse_errors:
        parts.append(f"Skipped {parse_errors} malformed line(s).")
    return " ".join(parts)


def render_json_summary(
    messages: list[Message],
    findings: list[Finding],
    paths: dict[str, Path],
    *,
    parse_errors: int = 0,
) -> str:
    result = ScanResult(messages=messages, findings=findings, parse_errors=parse_errors)
    payload = {
        "messages": len(messages),
        "sessions": result.session_count,
        "findings": len(findings),
        "parse_errors": parse_errors,
        "severity": _severity_counts(findings),
        "outputs": {name: str(path) for name, path in paths.items()},
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _severity_counts(findings: list[Finding]) -> dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


def _render_markdown(result: ScanResult) -> str:
    lines = [
        "# Agent Doctor Report",
        "",
        "## Summary",
        "",
        f"- Messages scanned: {len(result.messages)}",
        f"- Sessions scanned: {result.session_count}",
        f"- Findings: {len(result.findings)}",
    ]
    if result.findings:
        severities = _severity_counts(result.findings)
        lines.append(
            "- Severity breakdown: "
            + ", ".join(f"{count} {label}" for label, count in severities.items())
        )
    if result.parse_errors:
        lines.append(f"- Skipped malformed lines: {result.parse_errors}")
    lines.extend(
        [
            "",
            "## Privacy",
            "",
            "Evidence quotes are redacted by default, but they are still transcript "
            "excerpts. Review artifacts before sharing them outside your machine.",
            "",
            "## Findings",
            "",
        ]
    )
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
                f"- Occurrences: {finding.count}",
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
