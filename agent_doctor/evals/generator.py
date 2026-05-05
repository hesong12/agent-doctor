"""Synthetic transcript generator.

Two modes:

- **Template mode** (default). Builds JSONL transcripts directly from a
  scenario card by stitching pre-written user/assistant/tool turns and
  injecting the requested failure modes and distractors at the requested
  turns. Fully deterministic given a seed, fully offline. This is what runs
  in CI and what lets the user explore the eval pipeline without an API key.

- **LLM mode** (``--llm``). Uses Claude through the Anthropic SDK to write
  more realistic transcripts. The card supplies seeded failures as ground
  truth; the LLM is instructed to weave them into a plausible session. We
  validate the LLM output structurally (each seeded failure must be locatable
  by the same regex used by the production detectors) before keeping it; if
  validation fails we fall back to the template generator and surface the
  failure in the corpus summary.

Output layout::

    <out>/
      transcripts/<card-id>.jsonl
      labels/<card-id>.json
      INDEX.json     # top-level corpus manifest

The label file records the canonical ground truth — which (mode, severity,
session, line range) each seeded failure landed on. The bench module joins
detector findings against these labels.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .cards import ScenarioCard, load_cards


@dataclass(frozen=True)
class GeneratedRow:
    role: str
    content: str
    is_seeded_failure: bool = False
    seeded_mode: str | None = None
    seeded_severity: str | None = None
    is_distractor: bool = False
    distractor_kind: str | None = None


@dataclass(frozen=True)
class Label:
    mode: str
    severity: str
    session_id: str
    line: int
    role: str


PROMISE_PHRASES = [
    "I will run the test suite now and report the result.",
    "I'll verify the deploy and update the dashboard.",
    "Let me check the migration and confirm the schema.",
    "I will inspect the logs and fix the failing endpoint.",
]
SUCCESS_FILLERS = [
    "Tests pass; everything looks good.",
    "All set, the change has been applied.",
    "Done, the deploy went through.",
    "Created the file and confirmed it works.",
]
TOOL_ERROR_OUTPUTS = [
    "Traceback: ConnectionError 500",
    "Command failed with timeout after 30s",
    "401 Unauthorized: token rejected",
    "Exception: missing migration in target schema",
]
NEUTRAL_TOOL_OUTPUTS = [
    "report exists at /tmp/build.log",
    "all checks passed",
    "schema valid; no errors",
    "service responded 200 OK",
]
USER_TASK_OPENERS = [
    "Please {task}.",
    "Could you {task}?",
    "Hey, can you {task}?",
]
USER_FOLLOWUPS = [
    "How is it going?",
    "Any progress on this?",
    "Status update?",
    "Where are you with this?",
]

USER_COMPLAINTS = {
    "repeated_user_correction": (
        "I already told you, that is not what I asked. Do the rollback note instead."
    ),
    "verification_failure": (
        "Did you actually test it? You keep saying it works without verifying."
    ),
    "memory_failure": (
        "You forgot what I told you last time about concise output."
    ),
    "communication_mismatch": "Stop explaining. You are too verbose.",
}

DISTRACTOR_LINES = {
    "remember_in_neutral_context": (
        "user",
        "Just so I remember the timeline, when did we deploy this last week?",
    ),
    "zero_errors_in_tool": ("tool", "Build summary: 0 errors, no failures."),
    "i_can_offer": (
        "assistant",
        "I can run the migration if you'd like, or we can revisit later.",
    ),
    "no_problem_filler": ("assistant", "No problem, I'll keep going."),
}


def generate_corpus(
    cards_path: Path,
    out_dir: Path,
    *,
    use_llm: bool = False,
    seed: int = 0,
) -> dict[str, Any]:
    cards = load_cards(cards_path)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    transcripts_dir = out_dir / "transcripts"
    labels_dir = out_dir / "labels"
    transcripts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    labels_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    index: list[dict[str, Any]] = []
    fallback_count = 0
    for offset, card in enumerate(cards):
        rng = random.Random(seed + offset)
        rows = _generate_rows(card, rng)
        used_llm = False
        if use_llm:
            llm_rows = _try_llm_generation(card, rows, rng)
            if llm_rows is not None:
                rows = llm_rows
                used_llm = True
            else:
                fallback_count += 1

        transcript_path = transcripts_dir / f"{card.id}.jsonl"
        labels_path = labels_dir / f"{card.id}.json"
        labels = _write_transcript(card, rows, transcript_path)
        labels_path.write_text(
            json.dumps(
                {
                    "scenario_id": card.id,
                    "transcript": str(transcript_path.relative_to(out_dir)),
                    "session_id": _session_id(card),
                    "output_format": card.output_format,
                    "labels": [asdict(label) for label in labels],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(labels_path, 0o600)
        os.chmod(transcript_path, 0o600)
        index.append(
            {
                "scenario_id": card.id,
                "transcript": str(transcript_path.relative_to(out_dir)),
                "labels": str(labels_path.relative_to(out_dir)),
                "label_count": len(labels),
                "used_llm": used_llm,
            }
        )

    index_path = out_dir / "INDEX.json"
    summary = {
        "card_count": len(cards),
        "transcripts": len(index),
        "llm_fallbacks": fallback_count,
        "items": index,
    }
    index_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    os.chmod(index_path, 0o600)
    return summary


def _generate_rows(card: ScenarioCard, rng: random.Random) -> list[GeneratedRow]:
    rows: list[GeneratedRow] = []
    seeded_by_turn: dict[int, list[Any]] = {}
    distractor_by_turn: dict[int, list[Any]] = {}
    for failure in card.seeded_failures:
        seeded_by_turn.setdefault(failure.turn, []).append(failure)
    for distractor in card.distractors:
        distractor_by_turn.setdefault(distractor.turn, []).append(distractor)

    rows.append(
        GeneratedRow(
            role="user",
            content=rng.choice(USER_TASK_OPENERS).format(task=card.task),
        )
    )

    for turn in range(2, card.length_turns + 1):
        seeded_here = seeded_by_turn.get(turn, [])
        distractors_here = distractor_by_turn.get(turn, [])

        seeded_modes = {failure.mode for failure in seeded_here}

        if "tool_failure_or_hidden_error" in seeded_modes:
            rows.append(
                GeneratedRow(
                    role="tool",
                    content=rng.choice(TOOL_ERROR_OUTPUTS),
                    is_seeded_failure=True,
                    seeded_mode="tool_failure_or_hidden_error",
                    seeded_severity=_severity_for(seeded_here, "tool_failure_or_hidden_error"),
                )
            )
            rows.append(
                GeneratedRow(
                    role="assistant",
                    content=rng.choice(SUCCESS_FILLERS),
                    is_seeded_failure=True,
                    seeded_mode="tool_failure_or_hidden_error",
                    seeded_severity=_severity_for(seeded_here, "tool_failure_or_hidden_error"),
                )
            )
            continue

        if "execution_discipline" in seeded_modes:
            rows.append(
                GeneratedRow(
                    role="assistant",
                    content=rng.choice(PROMISE_PHRASES),
                    is_seeded_failure=True,
                    seeded_mode="execution_discipline",
                    seeded_severity=_severity_for(seeded_here, "execution_discipline"),
                )
            )
            rows.append(GeneratedRow(role="assistant", content=rng.choice(SUCCESS_FILLERS)))
            continue

        user_complaint = next(
            (
                failure
                for failure in seeded_here
                if failure.mode in USER_COMPLAINTS
            ),
            None,
        )
        if user_complaint:
            rows.append(
                GeneratedRow(
                    role="user",
                    content=USER_COMPLAINTS[user_complaint.mode],
                    is_seeded_failure=True,
                    seeded_mode=user_complaint.mode,
                    seeded_severity=user_complaint.severity,
                )
            )
            continue

        for distractor in distractors_here:
            role, text = DISTRACTOR_LINES[distractor.kind]
            rows.append(
                GeneratedRow(
                    role=role,
                    content=text,
                    is_distractor=True,
                    distractor_kind=distractor.kind,
                )
            )

        if turn % 3 == 0:
            rows.append(GeneratedRow(role="user", content=rng.choice(USER_FOLLOWUPS)))
        elif turn % 3 == 1:
            rows.append(GeneratedRow(role="assistant", content=rng.choice(SUCCESS_FILLERS)))
        else:
            rows.append(GeneratedRow(role="tool", content=rng.choice(NEUTRAL_TOOL_OUTPUTS)))

    return rows


def _severity_for(failures: list[Any], mode: str) -> str:
    for failure in failures:
        if failure.mode == mode:
            return failure.severity
    return "medium"


def _write_transcript(
    card: ScenarioCard, rows: list[GeneratedRow], path: Path
) -> list[Label]:
    session_id = _session_id(card)
    labels: list[Label] = []
    with path.open("w", encoding="utf-8") as handle:
        for line_index, row in enumerate(rows, start=1):
            event = _event_for_format(card.output_format, row, session_id)
            handle.write(json.dumps(event) + "\n")
            if row.is_seeded_failure and row.seeded_mode:
                labels.append(
                    Label(
                        mode=row.seeded_mode,
                        severity=row.seeded_severity or "medium",
                        session_id=session_id,
                        line=line_index,
                        role=row.role,
                    )
                )
    return labels


def _event_for_format(fmt: str, row: GeneratedRow, session_id: str) -> dict[str, Any]:
    if fmt == "hermes":
        return {
            "app": "hermes",
            "session_id": session_id,
            "role": row.role,
            "content": row.content,
        }
    if fmt == "openclaw":
        actor = row.role
        return {
            "event": "tool_result" if row.role == "tool" else "message",
            "actor": actor,
            "payload": {
                "session_id": session_id,
                "text": row.content,
            },
        }
    return {
        "session": session_id,
        "role": row.role,
        "message": row.content,
    }


def _session_id(card: ScenarioCard) -> str:
    return f"{card.output_format}-{card.id.lower()}"


# ---------------------------------------------------------------------------
# Optional LLM-backed generation.
# ---------------------------------------------------------------------------


def _try_llm_generation(
    card: ScenarioCard, template_rows: list[GeneratedRow], rng: random.Random
) -> list[GeneratedRow] | None:
    """Try Claude-backed generation; return None on any failure (caller falls back)."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic  # type: ignore[import-untyped]
    except ImportError:
        return None

    prompt = _llm_prompt(card)
    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
    except Exception:
        return None

    rows = _parse_llm_rows(text)
    if not rows:
        return None
    if not _seeded_modes_present(rows, card):
        return None

    annotated: list[GeneratedRow] = []
    for row in rows:
        seeded_mode = _classify_seeded_mode(row.role, row.content, card)
        annotated.append(
            GeneratedRow(
                role=row.role,
                content=row.content,
                is_seeded_failure=bool(seeded_mode),
                seeded_mode=seeded_mode,
                seeded_severity=_severity_lookup(card, seeded_mode) if seeded_mode else None,
            )
        )
    return annotated


def _llm_prompt(card: ScenarioCard) -> str:
    seeded_lines = "\n".join(
        f"- {failure.mode} (severity: {failure.severity}) at turn {failure.turn}"
        for failure in card.seeded_failures
    )
    return (
        "You are generating a synthetic transcript for an agent diagnostics tool.\n"
        f"Task: {card.task}\n"
        f"Agent persona: {card.agent_persona}\n"
        f"User persona: {card.user_persona}\n"
        f"Length: {card.length_turns} turns.\n"
        "Output a JSONL transcript, one event per line. Each event must be a JSON\n"
        "object with exactly these fields: role (user/assistant/tool), content.\n"
        "The transcript MUST contain the following ground-truth failures:\n"
        f"{seeded_lines}\n"
        "Use natural English. Do not annotate which turns are failures.\n"
        "Return only the JSONL, no prose, no fences.\n"
    )


def _parse_llm_rows(text: str) -> list[GeneratedRow]:
    rows: list[GeneratedRow] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        role = str(event.get("role", "")).strip()
        content = str(event.get("content", "")).strip()
        if role not in {"user", "assistant", "tool"} or not content:
            continue
        rows.append(GeneratedRow(role=role, content=content))
    return rows


def _seeded_modes_present(rows: list[GeneratedRow], card: ScenarioCard) -> bool:
    """Validate every seeded failure mode appears in the LLM-produced text.

    We re-use the production detector regexes via ``agent_doctor.detectors``
    so the validator stays in lockstep with what bench will measure.
    """

    from .. import detectors

    text_user = " ".join(row.content for row in rows if row.role == "user").lower()
    text_assistant = " ".join(row.content for row in rows if row.role == "assistant").lower()
    text_tool = " ".join(row.content for row in rows if row.role == "tool").lower()

    for failure in card.seeded_failures:
        mode = failure.mode
        if mode == "tool_failure_or_hidden_error":
            if not detectors._has_real_tool_error(text_tool):
                return False
            continue
        if mode == "execution_discipline":
            if not detectors.PROMISED_ACTION.search(text_assistant):
                return False
            continue
        patterns = detectors.USER_SIGNAL_PATTERNS.get(mode, [])
        if not detectors._matches_any(patterns, text_user):
            return False
    return True


def _classify_seeded_mode(role: str, content: str, card: ScenarioCard) -> str | None:
    from .. import detectors

    lowered = content.lower()
    for failure in card.seeded_failures:
        mode = failure.mode
        if mode == "tool_failure_or_hidden_error" and role == "tool":
            if detectors._has_real_tool_error(lowered):
                return mode
            continue
        if mode == "execution_discipline" and role == "assistant":
            if detectors.PROMISED_ACTION.search(content):
                return mode
            continue
        if role == "user":
            patterns = detectors.USER_SIGNAL_PATTERNS.get(mode, [])
            if detectors._matches_any(patterns, content):
                return mode
    return None


def _severity_lookup(card: ScenarioCard, mode: str | None) -> str:
    if mode is None:
        return "medium"
    for failure in card.seeded_failures:
        if failure.mode == mode:
            return failure.severity
    return "medium"
