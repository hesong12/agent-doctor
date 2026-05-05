"""Detection metrics for the bench harness.

The benchmark compares two streams: aggregated findings emitted by the
production detectors, and ground-truth labels recorded when the corpus was
generated. A correct match is "the detector produced an aggregated finding for
mode M in session S whose evidence covers a labeled line for that same mode in
S". That definition lets a single aggregated finding satisfy multiple labels
(which is what we want — aggregation is a feature, not a bug to be punished
in metrics).

We compute:

- per-mode TP / FP / FN with a precision / recall / F1 trio,
- a micro-averaged total across modes (the headline number),
- a confusion matrix where each labeled occurrence is attributed either to
  its expected mode or to whichever other mode the detector produced for the
  same session. This makes it visible when modes leak into each other.
- a severity-match rate (within-1 ordinal distance counts as match).

Numbers serialize to JSON so the bench output is diffable in PRs.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..schema import Finding, Severity

_SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Match:
    finding_id: str
    mode: str
    session_id: str
    label_indices: tuple[int, ...]


@dataclass(frozen=True)
class BenchTotals:
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p + r) else 0.0


def evaluate(
    findings_by_session: dict[str, list[Finding]],
    labels_by_session: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Compare findings vs labels and return a serializable metrics report."""

    per_mode_tp: dict[str, int] = defaultdict(int)
    per_mode_fp: dict[str, int] = defaultdict(int)
    per_mode_fn: dict[str, int] = defaultdict(int)
    matches: list[Match] = []
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    severity_hits = 0
    severity_total = 0

    all_sessions = set(findings_by_session) | set(labels_by_session)

    for session in sorted(all_sessions):
        finds = findings_by_session.get(session, [])
        labs = labels_by_session.get(session, [])
        used_label_indices: set[int] = set()

        # Match each finding against labels in the same session and mode.
        for finding in finds:
            matching_label_indices = [
                idx
                for idx, label in enumerate(labs)
                if label["mode"] == finding.failure_mode
                and idx not in used_label_indices
            ]
            if matching_label_indices:
                per_mode_tp[finding.failure_mode] += 1
                used_label_indices.update(matching_label_indices)
                matches.append(
                    Match(
                        finding_id=finding.id,
                        mode=finding.failure_mode,
                        session_id=session,
                        label_indices=tuple(matching_label_indices),
                    )
                )
                # Severity match (ordinal-distance ≤ 1 counts).
                for idx in matching_label_indices:
                    expected = labs[idx].get("severity", "medium")
                    severity_total += 1
                    if _severity_close(expected, finding.severity):
                        severity_hits += 1
            else:
                per_mode_fp[finding.failure_mode] += 1
                wrong_target = _attribute_to_other_mode(labs, finding.failure_mode, used_label_indices)
                if wrong_target:
                    confusion[wrong_target][finding.failure_mode] += 1
                else:
                    confusion["__no_label__"][finding.failure_mode] += 1

        for idx, label in enumerate(labs):
            if idx in used_label_indices:
                continue
            per_mode_fn[label["mode"]] += 1

    modes = (
        set(per_mode_tp)
        | set(per_mode_fp)
        | set(per_mode_fn)
    )
    per_mode: dict[str, dict[str, float]] = {}
    for mode in sorted(modes):
        totals = BenchTotals(
            tp=per_mode_tp[mode],
            fp=per_mode_fp[mode],
            fn=per_mode_fn[mode],
        )
        per_mode[mode] = {
            "tp": totals.tp,
            "fp": totals.fp,
            "fn": totals.fn,
            "precision": round(totals.precision, 4),
            "recall": round(totals.recall, 4),
            "f1": round(totals.f1, 4),
        }
    total_totals = BenchTotals(
        tp=sum(per_mode_tp.values()),
        fp=sum(per_mode_fp.values()),
        fn=sum(per_mode_fn.values()),
    )
    severity_rate = severity_hits / severity_total if severity_total else 0.0
    confusion_serial = {
        expected: dict(predicted) for expected, predicted in confusion.items()
    }

    return {
        "per_mode": per_mode,
        "total": {
            "tp": total_totals.tp,
            "fp": total_totals.fp,
            "fn": total_totals.fn,
            "precision": round(total_totals.precision, 4),
            "recall": round(total_totals.recall, 4),
            "f1": round(total_totals.f1, 4),
        },
        "severity_match_rate": round(severity_rate, 4),
        "confusion": confusion_serial,
        "matches": [
            {
                "finding_id": match.finding_id,
                "mode": match.mode,
                "session_id": match.session_id,
                "label_indices": list(match.label_indices),
            }
            for match in matches
        ],
    }


def _severity_close(expected: str, actual: Severity) -> bool:
    if expected not in _SEVERITY_RANK:
        return False
    return abs(_SEVERITY_RANK[expected] - _SEVERITY_RANK[actual]) <= 1


def _attribute_to_other_mode(
    labels: list[dict[str, Any]],
    detector_mode: str,
    used_label_indices: set[int],
) -> str | None:
    for idx, label in enumerate(labels):
        if idx in used_label_indices:
            continue
        if label["mode"] != detector_mode:
            return label["mode"]
    return None
