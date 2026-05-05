"""Replay-loop scaffold for closed-loop measurement.

Goal: measure whether applying Agent Doctor's recommendations actually
improves the next session. The scaffold takes (1) an original frustrating
transcript and (2) a staging directory of patches produced by ``apply``.
It then asks an LLM to "replay" the user turns of the original transcript
against a *patched* assistant that has the staged patches injected into its
system prompt. We re-run the production detectors against the replay, and
report the delta:

- finding count, by mode and severity,
- promised-but-unexecuted incidents,
- hidden-tool-error incidents,
- communication-mismatch / repeated-correction occurrences.

Without an ``ANTHROPIC_API_KEY`` (or the ``anthropic`` package installed),
the replay is skipped gracefully and the summary explains how to enable it.
That keeps the CLI surface honest: the command never silently produces
fabricated metrics.

Replay is the cheap-but-real cousin of the live closed-loop benchmark: it
relies on an LLM to play the patched agent rather than wiring up Hermes /
OpenClaw, which makes it cheap enough to run regularly while still grounding
the metric in real model behavior.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..detectors import detect_findings
from ..ingest import ingest_path_with_errors
from ..schema import Finding


@dataclass(frozen=True)
class ReplaySummary:
    enabled: bool
    reason: str
    baseline: dict[str, Any]
    replay: dict[str, Any]
    delta: dict[str, Any]
    artifacts: dict[str, str]


def run_replay(
    transcript_path: Path,
    patches_dir: Path,
    out_dir: Path,
    *,
    model: str = "claude-sonnet-4-6",
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    transcript_path = transcript_path.expanduser()
    patches_dir = patches_dir.expanduser()

    baseline_messages, _ = ingest_path_with_errors(transcript_path)
    baseline_findings = detect_findings(baseline_messages)
    baseline_stats = _stats(baseline_findings)

    enabled, reason, replay_findings = _attempt_replay(
        transcript_path, baseline_messages, patches_dir, out_dir, model
    )
    replay_stats = _stats(replay_findings) if enabled else _empty_stats()
    delta = _compute_delta(baseline_stats, replay_stats) if enabled else {}

    summary = ReplaySummary(
        enabled=enabled,
        reason=reason,
        baseline=baseline_stats,
        replay=replay_stats,
        delta=delta,
        artifacts={
            "baseline_transcript": str(transcript_path),
            "patches": str(patches_dir),
            "replay_transcript": str(out_dir / "replay.jsonl") if enabled else "",
        },
    )

    summary_path = out_dir / "replay-summary.json"
    summary_path.write_text(
        json.dumps(_summary_to_dict(summary), indent=2) + "\n", encoding="utf-8"
    )
    return _summary_to_dict(summary)


def _stats(findings: list[Finding]) -> dict[str, Any]:
    counts_by_mode: Counter[str] = Counter()
    counts_by_severity: Counter[str] = Counter()
    for finding in findings:
        counts_by_mode[finding.failure_mode] += finding.count
        counts_by_severity[finding.severity] += 1
    return {
        "findings": len(findings),
        "occurrences_by_mode": dict(counts_by_mode),
        "findings_by_severity": dict(counts_by_severity),
    }


def _empty_stats() -> dict[str, Any]:
    return {"findings": 0, "occurrences_by_mode": {}, "findings_by_severity": {}}


def _compute_delta(baseline: dict[str, Any], replay: dict[str, Any]) -> dict[str, Any]:
    modes = set(baseline["occurrences_by_mode"]) | set(replay["occurrences_by_mode"])
    by_mode = {}
    for mode in sorted(modes):
        before = baseline["occurrences_by_mode"].get(mode, 0)
        after = replay["occurrences_by_mode"].get(mode, 0)
        by_mode[mode] = {
            "before": before,
            "after": after,
            "delta": after - before,
            "relative": _relative_change(before, after),
        }
    return {
        "findings_before": baseline["findings"],
        "findings_after": replay["findings"],
        "findings_delta": replay["findings"] - baseline["findings"],
        "by_mode": by_mode,
    }


def _relative_change(before: int, after: int) -> float | None:
    if before == 0:
        return None
    return round((after - before) / before, 4)


def _summary_to_dict(summary: ReplaySummary) -> dict[str, Any]:
    return {
        "enabled": summary.enabled,
        "reason": summary.reason,
        "baseline": summary.baseline,
        "replay": summary.replay,
        "delta": summary.delta,
        "artifacts": summary.artifacts,
    }


def _attempt_replay(
    transcript_path: Path,
    baseline_messages: list[Any],
    patches_dir: Path,
    out_dir: Path,
    model: str,
) -> tuple[bool, str, list[Finding]]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return False, "ANTHROPIC_API_KEY not set; replay skipped.", []
    try:
        import anthropic  # type: ignore[import-untyped]
    except ImportError:
        return (
            False,
            "anthropic package not installed; install with `pip install agent-doctor[llm]`.",
            [],
        )

    system_prompt = _build_system_prompt(patches_dir)
    user_turns = [
        message.content
        for message in baseline_messages
        if getattr(message, "role", "") == "user"
    ]
    if not user_turns:
        return False, "Baseline transcript contains no user turns to replay.", []

    try:
        client = anthropic.Anthropic(api_key=api_key)
        replay_rows = _drive_conversation(client, model, system_prompt, user_turns)
    except Exception as exc:  # pragma: no cover — network/runtime path
        return False, f"Replay failed: {exc}", []

    replay_path = out_dir / "replay.jsonl"
    session_id = transcript_path.stem + "-replay"
    with replay_path.open("w", encoding="utf-8") as handle:
        for row in replay_rows:
            handle.write(
                json.dumps(
                    {
                        "session_id": session_id,
                        "role": row["role"],
                        "content": row["content"],
                    }
                )
                + "\n"
            )
    os.chmod(replay_path, 0o600)
    replay_messages, _ = ingest_path_with_errors(replay_path)
    return True, "ok", detect_findings(replay_messages)


def _drive_conversation(
    client: Any,
    model: str,
    system_prompt: str,
    user_turns: list[str],
) -> list[dict[str, str]]:
    transcript: list[dict[str, str]] = []
    history: list[dict[str, str]] = []
    for user_text in user_turns:
        transcript.append({"role": "user", "content": user_text})
        history.append({"role": "user", "content": user_text})
        message = client.messages.create(
            model=model,
            max_tokens=512,
            system=system_prompt,
            messages=history,
        )
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        transcript.append({"role": "assistant", "content": text})
        history.append({"role": "assistant", "content": text})
    return transcript


def _build_system_prompt(patches_dir: Path) -> str:
    parts = [
        "You are a coding assistant. Apply the following operating rules learned",
        "from prior session postmortems. Be concise, perform promised actions",
        "before claiming results, and never claim success when a tool reported an error.",
        "",
    ]
    for filename in ("memory.md", "sop.md", "identity.md", "tool-discipline.md"):
        path = patches_dir / filename
        if path.exists():
            parts.append(f"# {filename}")
            parts.append(path.read_text(encoding="utf-8"))
            parts.append("")
    return "\n".join(parts).strip()
