# Agent Doctor Architecture

Agent Doctor is CLI-first with an optional MCP stdio server in front. The trust boundary is local: transcript files are read locally, deterministic detectors run in-process, and report / patch artifacts are written only to caller-supplied output directories. Live host-agent configuration is never modified.

## Layers

### CLI Core

`agent_doctor.cli` provides the public commands:

- `scan` — ingest JSONL transcripts, detect findings, write reports. On first use (no SKILL.md installed in any detected host), prints a one-line hint to stderr suggesting `agent-doctor bootstrap --invalidate-cache`.
- `apply` — read `findings.json`, stage reviewable patches into a directory plus a unified diff against an optional `--target`. Live config is never modified.
- `eval` — sub-command group: `generate`, `bench`, `replay` for the LLM-first eval framework (see `docs/evaluation.md`).
- `bootstrap` — auto-detect `~/.hermes`, `~/.openclaw`, `~/.claude/skills` and write the unified `SKILL.md` into each host's correct skill location. `--invalidate-cache` best-effort signals each host to rebuild its skill prompt on next start (removes Hermes's `.skills_prompt_snapshot.json`, bumps Claude Code's `skills/` mtime). `--dry-run` previews; `--force` installs into hosts whose home dir doesn't exist.
- `install-skill` — write a single skill file by hand (`--target hermes|openclaw|claude-code|generic`).
- `doctor` — print environment, version, default paths, and privacy info.
- `mcp` — print MCP server metadata + tool list as JSON.
- `mcp serve` — run the stdio MCP server (requires the `[mcp]` extra).

The console script is `agent-doctor`; the same commands run via `python3 -m agent_doctor.cli`.

### One-line installer

`install.sh` (top-level) is the canonical user-facing install path:

```bash
curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh
```

It detects pipx vs `apt-get` vs `brew` vs `pip --user --break-system-packages`, installs pipx first if missing (one visible sudo prompt where required), runs `pipx install` from the GitHub repo (idempotent — `--force` on re-run), optionally injects extras (`--with-mcp` / `--with-llm` / `--with-all`), then runs `agent-doctor bootstrap --invalidate-cache` automatically. Set `AGENT_DOCTOR_REPO` / `AGENT_DOCTOR_REF` env vars to point at a fork or branch; pass `--skip-bootstrap` to land just the package.

### Ingestion

`agent_doctor.ingest` accepts one JSONL file or a directory of JSONL files. It normalizes Hermes-ish, OpenClaw-ish, and generic event formats into a shared `Message` model with roles `user`, `assistant`, `tool`, `system/metadata`. Messages retain file path, line, session id, source format, and raw type so every finding can cite evidence.

The ingestion layer is the resilience boundary: malformed lines are skipped by default with a `parse_errors` count surfaced to the user; `--strict` re-enables hard-fail. Per-message content is capped at `MAX_CONTENT_CHARS` (8000) so a single huge tool stdout cannot dominate downstream detection.

### Detection and Aggregation

`agent_doctor.detectors` runs deterministic regex / structural rules against normalized messages and emits raw matches. A second aggregation pass groups raw matches by `(failure_mode, session_id)`: a session with N user complaints of the same kind becomes **one** finding with N evidence quotes attached and severity escalated by count (`>=3 → high`, `2 → bump tier`).

The detectors do not call an LLM and do not modify agent state. False-positive guards live alongside each pattern, all driven by real-world data:

- **Negative phrases.** `0 errors` / `no failures` / `zero exceptions` stripped from tool stdout before matching.
- **JSON success envelopes.** `"error": null`, `"error": ""`, `"stderr": ""`, `"exit_code": 0`, `"status": 0`, `"success": true`, `"ok": true` (both quoted-JSON and bare-prose forms) and the literal `(not an error)` suffix get stripped — real Hermes / OpenClaw tool results carry these on success and would otherwise cause every successful command to flag as a hidden error.
- **Identifier-like trailing chars.** `error_handler.py`, `error.log`, `error_count` excluded from the bare `error` token via `(?![._-]\w)` lookahead.
- **Imperative-position `remember`.** Informational uses like "Just so I remember the timeline …" don't trip `memory_failure`; the regex requires sentence-initial position or a directed pronoun (`you (must|should|need to)?remember`).
- **Source-line references.** `cli.js:403:` does not match as HTTP 403 — the digit must not be surrounded by colons or other digits.

### Recommendations

`agent_doctor.recommend` maps each finding to reviewable patch proposals and a starter eval case. Patch targets:

- `memory` — durable user / context preferences.
- `identity` — communication-style and persona guidance.
- `sop` — standard operating procedure rules.
- `tool_discipline` — guards on tool-result handling.
- `eval` — reproducible test case for the failure mode.

Recommendations vary by aggregated count (single occurrences include an explicit overfit warning; ≥3 occurrences are treated as patterns).

### Reports

`agent_doctor.report` writes:

- `report.md` — human-readable summary with per-finding severity, occurrence count, and redacted evidence quotes.
- `findings.json` — machine-readable findings (consumed by `apply` and the MCP `list_findings` / `read_finding` tools).
- `eval-cases.yaml` — starter eval cases mirroring the detected modes.

All artifacts are written with `0o600` permissions. Transcript-derived strings pass through `agent_doctor.redaction` for common secrets, API keys, bearer tokens, and JWTs.

### Apply (patch staging)

`agent_doctor.apply` reads `findings.json` and materializes a staging directory:

```
staging/
  memory.md
  sop.md
  identity.md
  tool-discipline.md
  eval/<finding-id>.yaml
  MANIFEST.json
  DIFF.txt          # unified diff vs --target (read-only against live config)
```

`stage_patches()` accepts `minimum_severity` and `minimum_count` thresholds for noise control. The diff is computed using `difflib.unified_diff` against likely live counterpart filenames; if no live file exists the diff renders the patch as a brand-new file. **Nothing is applied automatically.** Live Hermes / OpenClaw / Claude Code configuration is never touched.

### Install and Bootstrap

`agent_doctor.install` writes a unified `SKILL.md` (YAML frontmatter + body) into the correct per-host location:

- `hermes` → `<skills>/autonomous-ai-agents/agent-doctor/SKILL.md` (Hermes's user-skill convention is depth-2 categorized; verified against 124+ existing skills on a real install).
- `openclaw` → `<skills>/agent-doctor/SKILL.md`.
- `claude-code` → `<skills>/agent-doctor/SKILL.md`.
- `generic` → `<out>/agent-doctor-skill.md` (flat file, no frontmatter — for hosts whose loader doesn't parse YAML).

The skill body has the same workflow regardless of host. The only host-specific bit is the suggested scan command in step 1 (`scan --hermes` / `scan --openclaw` / `scan --path <transcript-or-dir>`).

`agent_doctor.bootstrap` is the one-shot installer: it detects which hosts are present and calls `install_skill` for each, prints the MCP config snippet, and (with `--invalidate-cache`) signals each host to rebuild its skill prompt:

- **Hermes**: removes `~/.hermes/.skills_prompt_snapshot.json`. The cached prompt is rebuilt on next start; some live builds re-watch this file and reload immediately.
- **Claude Code**: bumps `~/.claude/skills/` mtime. New sessions auto-rescan.
- **OpenClaw**: skipped — reload protocol is undocumented; we no-op rather than guess and corrupt state.

`--dry-run` previews without writing; `--force` installs into hosts whose home directory doesn't exist yet.

### MCP Server

`agent_doctor.mcp` exposes the same surface as the CLI through the Model Context Protocol so any memoryful agent framework with MCP tool support (Hermes, OpenClaw, Claude Code, …) can call agent-doctor mid-session. Chat clients without their own memory / identity surface are out of scope — there's nothing for `stage_patches` to write into.

Tools:

| Tool | Reads | Writes |
|---|---|---|
| `scan` | JSONL transcripts | report artifacts under `out_dir` |
| `list_findings` | `findings.json` | — |
| `read_finding` | `findings.json` | — |
| `bench` | corpus dir | `bench.json`, `bench.md` under `out_dir` |
| `stage_patches` | `findings.json` (+ optional read-only `target_dir`) | `staging_dir` only |
| `generate_corpus` | scenario cards | corpus under `out_dir` |

Pure-Python tool handlers are testable without the SDK; the SDK glue (`build_server`, `serve`) lazy-imports `mcp`. Missing-extra path exits with a clean install hint, not a stacktrace. No tool calls a remote LLM — the LLM-augmented generator is a CLI-only path on purpose.

### Eval Framework (LLM-first)

`agent_doctor.evals` is a separate subpackage so the production scan path stays dependency-free. Four LLM roles are supported (generator, annotator, judge, replay agent). See [`docs/evaluation.md`](evaluation.md) for the full pipeline.

- `cards.py` — YAML scenario card loader (custom minimal parser, no PyYAML dep).
- `generator.py` — template generator + optional Anthropic-backed generator with structural validation against production regexes.
- `bench.py` — runs detectors against a labeled corpus, emits per-mode P/R/F1, severity-match rate, and a confusion matrix.
- `replay.py` — closed-loop delta with a patched agent, requires `ANTHROPIC_API_KEY` and the `[llm]` extra (gracefully no-ops without).
- `metrics.py` — pure metric computation.

CI runs the bench as a regression gate (`--gate-precision`, `--gate-recall`).

## Data Flow

```text
JSONL transcripts
  → ingest + normalize (Message[])
  → detectors (raw matches)
  → aggregate (Finding[] per session)
  → recommend (patch proposals + eval cases)
  → report.md / findings.json / eval-cases.yaml

findings.json
  → apply
  → staging/{memory,sop,identity,tool-discipline}.md + eval/*.yaml + DIFF.txt

scenario cards
  → eval generate
  → corpus (transcripts + ground-truth labels)
  → eval bench → P/R/F1
  → eval replay → before/after delta with patched agent
```

## Trust Boundary

- All production behavior stays inside the local process and local filesystem.
- Reads come from caller-supplied paths; writes go to caller-supplied output directories only.
- No production code mutates memory, identity, skills, permissions, routing, evals, or host-agent configuration.
- Remote LLMs are only contacted from the opt-in `eval generate --llm` and `eval replay` commands, both gated on `ANTHROPIC_API_KEY` and the `[llm]` extra.
- The MCP server inherits this contract: write tools only write under caller-supplied directories; no tool calls a remote LLM.
