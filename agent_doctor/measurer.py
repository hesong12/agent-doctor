"""Measurer: judge whether an applied patch addresses the user's complaint.

Simplified eval-replay: rather than actually replaying the agent's
response with vs without the patch (which requires a working host
inference and complex prompt construction), the measurer asks the host
LLM to judge whether the patch is likely to address the user's
expressed complaint.

Returns a 0.0-1.0 score. Stays graceful: if adapter has no inference
capability, score=0.5 (neutral / unmeasured).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Measurement:
    patch_id: str
    session_id: str
    target_kind: str
    score: float
    judged_by_model: str
    rationale: str
    measured_at: float

    def to_dict(self) -> dict:
        return asdict(self)


_JUDGE_PROMPT = """A user expressed frustration with an AI agent. Agent Doctor proposed a patch. Judge whether the patch is likely to address the user's frustration.

User message: {evidence_text}
Patch type: {target_kind}
Patch body: {patch_body}

Reply with strict JSON only: {{"score": 0.0-1.0, "rationale": "..."}}.
- 0.0: patch is unrelated to the user's complaint
- 0.5: unclear or partial fix
- 1.0: patch clearly addresses what the user is complaining about"""


def measure_patch(
    patch_log_entry: dict,
    *,
    evidence_text: str,
    adapter,
    judge_model: Optional[str] = None,
) -> Measurement:
    """Judge whether a patch addresses the originating failure.

    Returns Measurement with score 0.0-1.0. If adapter can't infer, returns
    score=0.5 (neutral / unmeasured).
    """
    caps = adapter.capabilities()
    patch_id = str(patch_log_entry.get("id", ""))
    session_id = str(patch_log_entry.get("session_id", ""))
    target_kind = str(patch_log_entry.get("target_kind", ""))
    patch_body = str(patch_log_entry.get("patch_body", ""))

    if not caps.can_infer_text:
        return Measurement(
            patch_id=patch_id,
            session_id=session_id,
            target_kind=target_kind,
            score=0.5,
            judged_by_model="",
            rationale="adapter has no infer; unmeasured",
            measured_at=time.time(),
        )

    prompt = _JUDGE_PROMPT.format(
        evidence_text=evidence_text[:1000],
        target_kind=target_kind,
        patch_body=patch_body[:1500],
    )

    try:
        response = adapter.infer_text(prompt, model=judge_model)
    except (RuntimeError, OSError, NotImplementedError):
        return Measurement(
            patch_id=patch_id,
            session_id=session_id,
            target_kind=target_kind,
            score=0.5,
            judged_by_model=judge_model or "",
            rationale="adapter error; unmeasured",
            measured_at=time.time(),
        )

    parsed = _parse_judge_response(response)
    if parsed is None:
        return Measurement(
            patch_id=patch_id,
            session_id=session_id,
            target_kind=target_kind,
            score=0.5,
            judged_by_model=judge_model or "",
            rationale="judge returned malformed response; unmeasured",
            measured_at=time.time(),
        )

    return Measurement(
        patch_id=patch_id,
        session_id=session_id,
        target_kind=target_kind,
        score=parsed["score"],
        judged_by_model=judge_model or caps.default_inference_model or "host-default",
        rationale=parsed["rationale"],
        measured_at=time.time(),
    )


def append_measurement(path: Path, measurement: Measurement) -> None:
    """Append to measurements.jsonl with 0o600."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as h:
            h.write(json.dumps(measurement.to_dict(), ensure_ascii=False) + "\n")
    finally:
        os.chmod(path, 0o600)


def _parse_judge_response(text: str) -> dict | None:
    """Parse judge JSON response. Tolerates fenced code blocks / leading noise."""
    try:
        d = json.loads(text.strip())
    except json.JSONDecodeError:
        stripped = text.strip()
        s = stripped.find("{")
        e = stripped.rfind("}")
        if s >= 0 and e > s:
            try:
                d = json.loads(stripped[s:e + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None
    if not isinstance(d, dict) or "score" not in d:
        return None
    try:
        score = float(d["score"])
    except (TypeError, ValueError):
        return None
    score = max(0.0, min(1.0, score))  # clamp to [0,1]
    return {
        "score": score,
        "rationale": str(d.get("rationale", "")),
    }
