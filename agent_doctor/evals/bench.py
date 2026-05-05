"""Detection benchmark: scan a labeled corpus and emit P/R/F1.

Inputs:

- A corpus directory produced by ``agent-doctor eval generate``. The corpus
  contains ``transcripts/`` (JSONL files, one per scenario) and ``labels/``
  (JSON ground truth, one per scenario), plus an ``INDEX.json`` manifest.

Outputs (under ``--out``):

- ``bench.json`` — full metrics report (see :mod:`metrics`).
- ``bench.md`` — short, human-readable summary suitable for PR descriptions.

The bench is fully deterministic and does not call out to any LLM. It runs in
CI as a regression gate against detector changes; ``--gate-precision`` and
``--gate-recall`` exit non-zero when thresholds are missed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..detectors import detect_findings
from ..ingest import ingest_path_with_errors
from ..schema import Finding
from .metrics import evaluate


@dataclass(frozen=True)
class BenchResult:
    per_mode: dict[str, dict[str, float]]
    total: dict[str, float]
    severity_match_rate: float
    confusion: dict[str, dict[str, int]]
    transcript_count: int
    label_count: int
    finding_count: int

    def to_summary(self) -> dict[str, Any]:
        return {
            "transcripts": self.transcript_count,
            "labels": self.label_count,
            "findings": self.finding_count,
            "total": self.total,
            "severity_match_rate": self.severity_match_rate,
            "per_mode": self.per_mode,
            "confusion": self.confusion,
        }


def run_benchmark(corpus_dir: Path, out_dir: Path) -> BenchResult:
    corpus_dir = corpus_dir.expanduser()
    if not corpus_dir.exists():
        raise FileNotFoundError(f"Corpus directory not found: {corpus_dir}")
    index_path = corpus_dir / "INDEX.json"
    if not index_path.exists():
        raise FileNotFoundError(
            f"INDEX.json missing in {corpus_dir}; did you run `agent-doctor eval generate`?"
        )

    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    index = json.loads(index_path.read_text(encoding="utf-8"))

    findings_by_session: dict[str, list[Finding]] = defaultdict(list)
    labels_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)

    transcript_count = 0
    finding_count = 0
    label_count = 0

    for entry in index.get("items", []):
        transcript_path = corpus_dir / entry["transcript"]
        label_path = corpus_dir / entry["labels"]
        labels_doc = json.loads(label_path.read_text(encoding="utf-8"))
        session_id = labels_doc["session_id"]

        messages, _ = ingest_path_with_errors(transcript_path)
        findings = detect_findings(messages)
        findings_by_session[session_id].extend(findings)
        labels_by_session[session_id].extend(labels_doc["labels"])

        transcript_count += 1
        finding_count += len(findings)
        label_count += len(labels_doc["labels"])

    metrics = evaluate(findings_by_session, labels_by_session)
    result = BenchResult(
        per_mode=metrics["per_mode"],
        total=metrics["total"],
        severity_match_rate=metrics["severity_match_rate"],
        confusion=metrics["confusion"],
        transcript_count=transcript_count,
        label_count=label_count,
        finding_count=finding_count,
    )

    bench_json_path = out_dir / "bench.json"
    bench_json_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    bench_md_path = out_dir / "bench.md"
    bench_md_path.write_text(_render_markdown(result), encoding="utf-8")

    return result


def _render_markdown(result: BenchResult) -> str:
    total = result.total
    lines = [
        "# Agent Doctor — Detection Benchmark",
        "",
        f"- Transcripts: {result.transcript_count}",
        f"- Labels: {result.label_count}",
        f"- Findings: {result.finding_count}",
        f"- Severity-match rate (within 1): {result.severity_match_rate:.2%}",
        "",
        "## Total",
        "",
        f"- Precision: {total['precision']:.2f}",
        f"- Recall:    {total['recall']:.2f}",
        f"- F1:        {total['f1']:.2f}",
        f"- TP / FP / FN: {total['tp']} / {total['fp']} / {total['fn']}",
        "",
        "## Per-mode",
        "",
        "| mode | precision | recall | F1 | TP | FP | FN |",
        "|------|-----------|--------|----|----|----|----|",
    ]
    for mode, metrics in sorted(result.per_mode.items()):
        lines.append(
            f"| {mode} | {metrics['precision']:.2f} | {metrics['recall']:.2f} | "
            f"{metrics['f1']:.2f} | {metrics['tp']} | {metrics['fp']} | {metrics['fn']} |"
        )
    if result.confusion:
        lines.extend(["", "## Confusion (rows = expected mode, cols = predicted mode)", ""])
        for expected, row in sorted(result.confusion.items()):
            lines.append(
                f"- `{expected}` →  "
                + ", ".join(f"{predicted}: {count}" for predicted, count in sorted(row.items()))
            )
    lines.append("")
    return "\n".join(lines)
