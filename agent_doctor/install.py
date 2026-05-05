"""Generate safe Agent Doctor skill / SOP files for host agents.

Each host agent ecosystem has its own conventions for where skills live and
what file format they expect. This module owns the per-target text and
defaults so the rest of the codebase can treat installation as a single
function call.

Currently supported targets:

- ``hermes``      — writes ``agent-doctor-hermes-sop.md`` (Markdown SOP).
- ``openclaw``    — writes ``agent-doctor-openclaw-sop.md`` (Markdown SOP).
- ``claude-code`` — writes ``agent-doctor/SKILL.md`` with Claude Code YAML
                    frontmatter, suitable for ``~/.claude/skills/``.
- ``generic``     — writes ``agent-doctor-skill.md`` for any framework that
                    just needs a Markdown skill file.

The bootstrap module (``agent_doctor.bootstrap``) detects which of these
hosts are present on the system and calls ``install_skill`` for each — that's
the entry point a user gets when they say "install agent-doctor". This module
stays declarative so adding a new host is one entry in ``_TARGET_HANDLERS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

VALID_TARGETS: set[str] = {"hermes", "openclaw", "claude-code", "generic"}


@dataclass(frozen=True)
class InstallResult:
    target: str
    path: Path


def install_skill(target: str, out_dir: Path) -> Path:
    """Write a skill / SOP file for ``target`` into ``out_dir``.

    Returns the path written. Raises :class:`ValueError` for unknown targets.
    Existing files are overwritten so repeat installs are idempotent.
    """

    normalized = target.strip().casefold().replace("_", "-")
    if normalized == "claude":
        normalized = "claude-code"
    handler = _TARGET_HANDLERS.get(normalized)
    if handler is None:
        raise ValueError(
            f"Unsupported target: {target}. Expected one of {sorted(VALID_TARGETS)}."
        )
    out_dir.expanduser().mkdir(parents=True, exist_ok=True)
    return handler(out_dir.expanduser())


def _install_hermes(out_dir: Path) -> Path:
    path = out_dir / "agent-doctor-hermes-sop.md"
    path.write_text(_sop_text("Hermes", "--hermes"), encoding="utf-8")
    return path


def _install_openclaw(out_dir: Path) -> Path:
    path = out_dir / "agent-doctor-openclaw-sop.md"
    path.write_text(_sop_text("OpenClaw", "--openclaw"), encoding="utf-8")
    return path


def _install_generic(out_dir: Path) -> Path:
    path = out_dir / "agent-doctor-skill.md"
    path.write_text(_sop_text("your agent", "--path <transcripts>"), encoding="utf-8")
    return path


def _install_claude_code(out_dir: Path) -> Path:
    skill_dir = out_dir / "agent-doctor"
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(_claude_code_skill_text(), encoding="utf-8")
    return path


_TARGET_HANDLERS: dict[str, Callable[[Path], Path]] = {
    "hermes": _install_hermes,
    "openclaw": _install_openclaw,
    "claude-code": _install_claude_code,
    "generic": _install_generic,
}


def _sop_text(label: str, default_flag: str) -> str:
    return f"""# Agent Doctor SOP for {label}

Use Agent Doctor as a local-first engineering diagnosis tool for agent session postmortems. It turns frustrating session transcripts into reviewable patch proposals for memory, identity, SOPs, tool discipline, and evals.

## When to invoke

- The user reports a frustrating session and wants to know what went wrong.
- A pattern of failure is suspected across recent sessions.
- Reviewing a transcript before changing memory, SOP, or identity.

## Workflow

1. Scan: `agent-doctor scan {default_flag} --format markdown --out ./postmortem`
2. Read `./postmortem/report.md` and triage findings by severity.
3. Stage reviewable patches: `agent-doctor apply --findings ./postmortem --out ./staging --target <live-config-dir>`
4. Review `./staging/sop.md`, `memory.md`, `identity.md`, `tool-discipline.md`, and `DIFF.txt`.
5. Manually copy approved sections into the live config. Do not auto-apply.

## Operating rules

- Run Agent Doctor local-only. The `scan` and `apply` commands make no network calls.
- Treat all output as dry-run. Review proposed patches before adopting them.
- Ask the user before copying patches into memory, identity, skills, SOPs, permissions, routing, or evals.
- Never paste full transcripts to a remote LLM unless the user explicitly approves that disclosure.
- Prefer evidence quotes and line spans over broad claims about the user or agent.

## Eval (optional)

When the user wants to validate detector quality or measure improvement:

- `agent-doctor eval generate --cards <cards-dir> --out ./corpus`
- `agent-doctor eval bench --corpus ./corpus --out ./bench`
- `agent-doctor eval replay --transcript <jsonl> --patches ./staging --out ./replay`

`eval replay` requires `ANTHROPIC_API_KEY` and `pip install agent-doctor[llm]`. Skip gracefully without them.

## What this is not

Not therapy. Not HR performance management. Not surveillance analytics. It is engineering diagnosis: evidence → root cause → patch proposal → eval.
"""


def _claude_code_skill_text() -> str:
    return """---
name: agent-doctor
description: Local-first session postmortem and improvement engine for memoryful AI agents. Use when the user wants to diagnose frustrating agent sessions, find patterns of failure (verbose output, hidden tool errors, repeated corrections, memory gaps, missing verification), or turn session complaints into reviewable patches for memory/SOP/identity. Triggers: "review my session", "why does the agent keep doing X", "generate a postmortem", "diagnose this transcript", "what went wrong in this session".
---

# Agent Doctor

Run Agent Doctor as a local-first engineering diagnosis tool for agent session postmortems. It reads JSONL session transcripts, detects deterministic failure patterns, aggregates repeated occurrences, and produces reviewable patches.

## When to use

- The user reports a frustrating session and wants to know what went wrong.
- A pattern of failure is suspected across recent sessions of any memoryful agent (Hermes, OpenClaw, Claude Code, etc.).
- Reviewing a transcript before changing memory, SOP, identity, or skill files.

## Detection taxonomy

Agent Doctor detects six failure modes deterministically from transcript signals:

- `repeated_user_correction` — "I already told you", "not what I asked", "X again".
- `execution_discipline` — promised action without observed tool execution; "don't just plan".
- `verification_failure` — "did you test", "without verifying", "not actually tested".
- `memory_failure` — "you forgot", imperative "remember", "last time", "I told you".
- `tool_failure_or_hidden_error` — tool emits error/timeout/401/500/traceback, assistant claims success.
- `communication_mismatch` — "too verbose", "stop explaining".

## Workflow

1. Scan: `agent-doctor scan --path <transcript-or-dir> --format markdown --out ./postmortem`
2. Read `./postmortem/report.md`. Findings are aggregated per `(failure_mode, session_id)` with severity escalating by count.
3. Stage reviewable patches: `agent-doctor apply --findings ./postmortem --out ./staging --target <live-config-dir>`. Use `--min-severity medium` to filter low-severity noise.
4. Review `./staging/sop.md`, `memory.md`, `identity.md`, `tool-discipline.md`, and the unified `DIFF.txt` against the live config.
5. Manually apply approved sections. Do not auto-apply.

## Operating rules

- Local-only by default. Scan and apply make no network calls.
- All artifacts written `0600` and pre-redacted for common secrets, API keys, bearer tokens, JWTs.
- Never paste full transcripts to a remote LLM unless the user explicitly approves.
- Prefer evidence quotes and line spans over broad claims about the user or the agent.

## Eval harness (optional)

For quality validation and closed-loop measurement:

- `agent-doctor eval generate --cards <cards-dir> --out ./corpus` — synthetic transcripts with ground-truth labels.
- `agent-doctor eval bench --corpus ./corpus --out ./bench --gate-precision 0.95 --gate-recall 0.85` — detector P/R/F1, exits non-zero on regression.
- `agent-doctor eval replay --transcript <jsonl> --patches ./staging --out ./replay` — closed-loop delta with patched agent (requires `ANTHROPIC_API_KEY` and `agent-doctor[llm]`).

See `docs/evaluation.md` in the agent-doctor repo for the full LLM-first framework.

## What this is not

Not therapy. Not HR performance management. Not surveillance analytics. It is engineering diagnosis: evidence → root cause → patch proposal → eval.
"""
