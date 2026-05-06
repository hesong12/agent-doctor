"""Generate Agent Doctor skill files for memoryful agent frameworks.

All three memoryful frameworks we target use the same per-skill directory
convention: ``<host-skills-dir>/agent-doctor/SKILL.md`` with YAML
frontmatter (``name:``, ``description:`` with explicit triggers). Hermes is
the one exception — its user skills are *categorized* at depth 2, so
agent-doctor lands under the ``autonomous-ai-agents`` category alongside
peer skills like ``hermes-agent``, ``codex``, ``claude-code``, and
``openclaw-cross-agent-dispatch``.

Currently supported targets:

- ``hermes``      — writes ``<skills>/autonomous-ai-agents/agent-doctor/SKILL.md``.
- ``openclaw``    — writes ``<skills>/agent-doctor/SKILL.md``.
- ``claude-code`` — writes ``<skills>/agent-doctor/SKILL.md``.
- ``generic``     — writes a flat ``<out>/agent-doctor-skill.md`` (no
                    frontmatter) for hosts whose loader doesn't parse YAML.

The bootstrap module (``agent_doctor.bootstrap``) detects which of these
hosts are present on the system and calls ``install_skill`` for each — that
plus optional ``--invalidate-cache`` is the entry point a user gets when
they say "install agent-doctor". This module stays declarative so adding a
new host is one entry in ``_TARGET_HANDLERS``.
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
    # Hermes user skills live under category subdirectories — verified
    # against /home/hermes/.hermes/skills/ on a real install (124 of 126
    # skills are at <category>/<name>/SKILL.md). agent-doctor sits next to
    # peer "autonomous-ai-agents" skills like hermes-agent, codex,
    # claude-code, and openclaw-cross-agent-dispatch.
    return _write_skill_dir(
        out_dir / "autonomous-ai-agents",
        _skill_text("agent-doctor scan --hermes"),
    )


def _install_openclaw(out_dir: Path) -> Path:
    return _write_skill_dir(out_dir, _skill_text("agent-doctor scan --openclaw"))


def _install_claude_code(out_dir: Path) -> Path:
    return _write_skill_dir(out_dir, _skill_text("agent-doctor scan --path <transcript-or-dir>"))


def _install_generic(out_dir: Path) -> Path:
    # Generic targets don't follow a known directory convention, so we drop a
    # flat Markdown file. The text is identical to the unified skill body
    # without YAML frontmatter (since unknown hosts may not parse it).
    path = out_dir / "agent-doctor-skill.md"
    path.write_text(_skill_text("agent-doctor scan --path <transcripts>"), encoding="utf-8")
    return path


def _write_skill_dir(out_dir: Path, content: str) -> Path:
    """Write the unified SKILL.md into ``<out_dir>/agent-doctor/SKILL.md``.

    This convention is what every memoryful framework we've examined uses
    (Claude Code, OpenClaw extensions): a per-skill directory containing a
    ``SKILL.md`` with YAML frontmatter. Putting all hosts on the same shape
    means agents discover us the same way regardless of host runtime.
    """

    skill_dir = out_dir / "agent-doctor"
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


_TARGET_HANDLERS: dict[str, Callable[[Path], Path]] = {
    "hermes": _install_hermes,
    "openclaw": _install_openclaw,
    "claude-code": _install_claude_code,
    "generic": _install_generic,
}


def _skill_text(default_scan_command: str) -> str:
    """Return the unified SKILL.md text with YAML frontmatter.

    All memoryful agent frameworks we target use the same `<skill>/SKILL.md`
    convention with frontmatter — Claude Code, OpenClaw extensions, and
    similar. The only host-specific bit is the scan command we suggest in
    the workflow (so an agent on Hermes sees `--hermes`, on OpenClaw sees
    `--openclaw`, etc.).

    `__SCAN_COMMAND__` is the only placeholder; the embedded JSON config
    snippet contains literal `{}` braces, so we use a plain template +
    string replace instead of an f-string.
    """

    template = """---
name: agent-doctor
description: Local-first session postmortem and improvement engine for memoryful AI agent frameworks. Use when the user wants to diagnose a frustrating session, summon Doctor Pet for an active frustration moment, find patterns of failure (verbose output, hidden tool errors, repeated corrections, memory gaps, missing verification), or turn session complaints into reviewable patches for memory / SOP / identity / tool-discipline files. Triggers: "review my session", "diagnose this transcript", "what went wrong in my last session", "why does the agent keep doing X", "generate a postmortem", "this session was frustrating", "audit recent sessions", "find patterns in my agent's failures", "help right now", "you keep doing this".
---

# Agent Doctor

Use Agent Doctor as a local-first engineering diagnosis tool for agent session postmortems. It turns frustrating session transcripts into reviewable patch proposals for memory, identity, SOPs, tool discipline, and evals.

## When to invoke

- The user reports a frustrating session and wants to know what went wrong.
- The user is angry or losing trust in the current turn and needs a visible recovery response.
- A pattern of failure is suspected across recent sessions.
- Reviewing a transcript before changing memory, SOP, or identity.

## Doctor Pet

Doctor Pet is Agent Doctor's small doctor-shaped intervention surface. It is
local-only and deterministic: `idle`, `watching`, `concerned`, or
`intervening`, with redacted evidence and 2-3 action options.

Manual summon for the current turn:

`agent-doctor pet --message "<current user message>"`

Transcript/status mode:

`agent-doctor pet --path <transcript-or-dir> --out ./doctor-pet`

Desktop display:

`agent-doctor pet-display --status-file ./doctor-pet/pet-status.json`

The desktop pet renders a packaged chibi doctor sprite when available and
animates locally by state: idle breathing, watching scan, concerned diagnostic
pulse, and intervening alert. In autopilot setup it is the default user-facing
entry point: click it for the single status/action panel. For transcript-backed
OpenClaw/Hermes incidents, the dialog can send the generated recovery suggestion
back through the local host adapter; manual incidents remain copy-only.

If Doctor Pet returns `action=intervene`, pause the normal success path, name
the concrete failure, cite the evidence, and provide one corrective next step.
Do not defend the previous response or write a long apology.

## Autopilot sidecar

Agent Doctor can also run outside the host runtime as a platform-agnostic
sidecar. This is for productized deployments where the user should not have
to remember to ask the agent for diagnosis.

- OpenClaw: `agent-doctor autopilot --platform openclaw --out ~/.agent-doctor/openclaw`
- Hermes: `agent-doctor autopilot --platform hermes --out ~/.agent-doctor/hermes`
- Long-running mode: add `--watch`
- Zero-touch setup: `agent-doctor setup autopilot`
- Service install: `agent-doctor service install --platform openclaw --out ~/.agent-doctor/openclaw --start`

The sidecar only reads existing transcript/log JSONL through Agent Doctor's
ingestion layer and writes diagnosis cards/events under `--out`. It also
refreshes host-local Pet status there and, after `setup autopilot`, shared
desktop Pet status under `~/.agent-doctor/pet`. It does not send system
notifications or host messages by default, and it does not modify OpenClaw,
Hermes, or live agent configuration.

When the user asks you to enable proactive diagnosis, prefer
`agent-doctor setup autopilot`. It detects OpenClaw/Hermes, installs or
refreshes Agent Doctor skills, baselines existing transcripts, writes the
right launchd/systemd user services, installs the desktop Doctor Pet service,
and starts them with changed-file scanning. Doctor Pet is the only default
interactive surface; healthy idle state has no setup/start action because
sidecar installation is handled by setup, not by the Pet window. Legacy notify
hooks are explicit opt-ins.
Do not ask the user to manually edit host configuration.

## Detection taxonomy (what this skill catches)

Agent Doctor detects seven failure modes deterministically from transcript signals:

- `repeated_user_correction` — "I already told you", "not what I asked", "X again".
- `execution_discipline` — promised action without observed tool execution; "don't just plan".
- `verification_failure` — "did you test", "without verifying", "not actually tested".
- `memory_failure` — "you forgot", imperative "remember", "last time", "I told you".
- `tool_failure_or_hidden_error` — tool emits error / timeout / 401 / 500 / traceback, assistant claims success.
- `communication_mismatch` — "too verbose", "stop explaining".
- `user_frustration_signal` — user anger, direct insult/profanity, trust-break language, direct dumb/stupid feedback, or direct quality complaints.

Autopilot emits high-severity user frustration with action `intervene`, not just
`notify`. Treat an intervention as a live recovery moment: pause the normal
success path, identify the concrete failure, cite evidence, and give the next
corrective action without defensiveness or a long apology.

## Workflow

1. Scan: `__SCAN_COMMAND__ --format markdown --out ./postmortem`
2. Read `./postmortem/report.md` and triage findings by severity. Findings are aggregated per `(failure_mode, session_id)` with severity escalating by count.
3. Stage reviewable patches: `agent-doctor apply --findings ./postmortem --out ./staging --target <live-config-dir>`. Use `--min-severity medium` to filter low-severity noise.
4. Review `./staging/sop.md`, `memory.md`, `identity.md`, `tool-discipline.md`, and `DIFF.txt` against the live config.
5. Manually copy approved sections into the live config. Do not auto-apply.

## Operating rules

- Local-only by default. The `scan` and `apply` commands make no network calls and never modify live host-agent configuration.
- Treat all output as dry-run. Review proposed patches before adopting them.
- Ask the user before copying patches into memory, identity, skills, SOPs, permissions, routing, or evals.
- Never paste full transcripts to a remote LLM unless the user explicitly approves that disclosure.
- Prefer evidence quotes and line spans over broad claims about the user or agent.
- All artifacts are written `0600` and pre-redacted for common secrets, API keys, bearer tokens, and JWTs.

## Eval (optional)

When the user wants to validate detector quality or measure improvement:

- `agent-doctor eval generate --cards <cards-dir> --out ./corpus`
- `agent-doctor eval bench --corpus ./corpus --out ./bench`
- `agent-doctor eval replay --transcript <jsonl> --patches ./staging --out ./replay`

`eval replay` requires `ANTHROPIC_API_KEY` and `pip install agent-doctor[llm]`. Skip gracefully without them.

## MCP integration (optional)

If the host runtime speaks MCP, `agent-doctor mcp serve` (requires `pip install agent-doctor[mcp]`) exposes the same surface as the CLI: `scan`, `list_findings`, `read_finding`, `bench`, `stage_patches`, `generate_corpus`, `doctor_pet_status`, `doctor_pet_intervene`. Write tools only write to caller-supplied `staging_dir` / `out_dir`; no tool calls a remote LLM.

Configuration snippet:

```json
{
  "mcpServers": {
    "agent-doctor": {
      "command": "agent-doctor",
      "args": ["mcp", "serve"],
      "env": {}
    }
  }
}
```

## What this is not

Not therapy. Not HR performance management. Not surveillance analytics. It is engineering diagnosis: evidence → root cause → patch proposal → eval.
"""
    return template.replace("__SCAN_COMMAND__", default_scan_command)
