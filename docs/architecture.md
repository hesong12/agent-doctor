# Agent Doctor Architecture

Agent Doctor is CLI-first with an optional MCP stdio server in front. The trust boundary is local: transcript files are read locally, deterministic detectors run in-process, and report / patch artifacts are written only to caller-supplied output directories. Live host-agent configuration is never modified.

## Layers

### CLI Core

`agent_doctor.cli` provides the public commands:

- `scan` — ingest JSONL transcripts, detect findings, write reports. On first use (no SKILL.md installed in any detected host), prints a one-line hint to stderr suggesting `agent-doctor bootstrap --invalidate-cache`.
- `apply` — read `findings.json`, stage reviewable patches into a directory plus a unified diff against an optional `--target`. Live config is never modified.
- `eval` — sub-command group: `generate`, `bench`, `replay` for the LLM-first eval framework (see `docs/evaluation.md`).
- `pet` — render the Doctor Pet state for a transcript or current user message. Optional `--out` writes `pet-status.json` and `pet-card.md` for desktop/widget shells.
- `pet-display` — open the always-on-top desktop Doctor Pet window by watching a `pet-status.json` file. This lazy-imports Tkinter and is isolated from the headless production path.
- `autopilot` — run the platform-agnostic sidecar trigger engine. It reads host transcripts through adapters, keeps local SQLite state for de-duplication/cooldown, and writes diagnosis cards/events under `--out`.
- `setup autopilot` — opinionated agent-managed installation: detect OpenClaw/Hermes, bootstrap skills, baseline existing transcripts, write launchd/systemd user services, and start the sidecar with safe defaults.
- `notify openclaw-system-event` — host-native delivery adapter for OpenClaw. It reads `AGENT_DOCTOR_*` metadata from autopilot's notify hook and enqueues an `openclaw system event` for live intervention cards.
- `bootstrap` — auto-detect `~/.hermes`, `~/.openclaw`, `~/.claude/skills` and write the unified `SKILL.md` into each host's correct skill location. `--invalidate-cache` best-effort signals each host to rebuild its skill prompt on next start (removes Hermes's `.skills_prompt_snapshot.json`, bumps Claude Code's `skills/` mtime). `--dry-run` previews; `--force` installs into hosts whose home dir doesn't exist.
- `install-skill` — write a single skill file by hand (`--target hermes|openclaw|claude-code|generic`).
- `doctor` — print environment, version, default paths, and privacy info.
- `mcp` — print MCP server metadata + tool list as JSON.
- `mcp serve` — run the stdio MCP server (requires the `[mcp]` extra).

The console script is `agent-doctor`; the same commands run via `python3 -m agent_doctor.cli`.

### Doctor Pet

`agent_doctor.pet` is the product-facing state adapter above the deterministic
detection/autopilot layer. It does not introduce new detection rules. Instead,
it consumes normalized `Message` objects plus `Finding` / `AutopilotEvent`
outputs and returns an immutable `PetStatus`:

- persona: currently `doctor`.
- state: `idle`, `watching`, `concerned`, or `intervening`.
- action: `silent`, `notify`, or `intervene`.
- redacted evidence and stable action options such as `pause_and_diagnose`,
  `stage_fix`, and `keep_watching`.

There are two entry paths:

```bash
agent-doctor pet --message "<current user message>"
agent-doctor pet --path ./sessions --out ./doctor-pet
agent-doctor pet-display --status-file ./doctor-pet/pet-status.json
```

The first path is manual summon for an active turn; it creates one synthetic
local `Message` and runs the same deterministic detectors. The second path
reads transcripts through normal ingestion and picks the highest-priority
autopilot event or finding. Optional artifacts are written only under `--out`
as `pet-status.json` and `pet-card.md` with `0o600` permissions. This lets a
future desktop widget render the pet without linking to the detection core or
watching live host runtime state.

Autopilot writes the same `pet-status.json` / `pet-card.md` pair by default on
every sidecar pass. That is the always-display contract: the pet is always
renderable, and `pet-display` can keep a small borderless topmost desktop
window synced while its state changes from `idle` to `intervening` only when
the local trigger engine sees a quality incident. The display layer is still
optional UI glue: it reads a packaged transparent PNG doctor sprite when
available, falls back to code-drawn shapes, and applies local state-specific
effects without changing the detection model.

### Autopilot Sidecar

`agent_doctor.autopilot` is the productized "agent should not have to remember"
path. It does **not** require OpenClaw, Hermes, Claude Code, or Codex source
changes. The connection to host platforms is adapter-based:

- **Read side:** use existing transcript/log JSONL directories. `--platform openclaw`
  defaults to `~/.openclaw/agents/main/sessions`; `--platform hermes` defaults
  to `~/.hermes/sessions`; `--platform generic --path <file-or-dir>` handles
  other frameworks.
- **State side:** keep `state.sqlite3` under the Agent Doctor output directory
  (or caller-provided `--state`) for event fingerprints, per-session cooldowns,
  and de-duplication.
- **Write side:** emit `events.jsonl`, `latest.md`, and one short diagnosis
  card per event under `cards/`. It also refreshes `pet-status.json` and
  `pet-card.md` every pass so Doctor Pet can remain visible even when idle.
  These are intervention/display artifacts, not live config changes.

Current triggers are intentionally deterministic and local:

- `user_frustration_signal` — direct quality complaints, insults/profanity,
  repeated corrections, and trust-break language such as "not useful",
  "no value", "not thinking", "what the fuck", "Why are you so dumb?",
  "Are you stupid?", "不够聪明", "废物", "每次都这样", "有没有想清楚",
  and "你怎么这么笨的？". Technical terms such as "笨重" are guarded as
  non-frustration distractors.
- `completion_claim_without_nearby_verification` — assistant says a task is
  fixed/done/completed without nearby tool or verification evidence.
- `tool_failure_or_hidden_error` — reused from the existing deterministic
  detector pipeline when a tool failure is hidden or not acknowledged.

High-severity `user_frustration_signal` events are emitted with action
`intervene`, not just `notify`. The card instructs the host agent to pause the
normal success path, name the concrete quality failure, cite evidence, and
provide the next corrective action. Doctor Pet renders the same event as an
`intervening` state so the product can show a visible small-doctor presence
instead of only writing a background card.

Run modes:

```bash
agent-doctor autopilot --platform openclaw --out ~/.agent-doctor/openclaw
agent-doctor autopilot --platform hermes --out ~/.agent-doctor/hermes --watch --interval 15
agent-doctor service install --platform openclaw --out ~/.agent-doctor/openclaw --start
```

`--watch` is a polling sidecar loop suitable for launchd/systemd. Cron can run
one-shot checks as a fallback, but the product model is a local daemon with
state and cooldown, closer to a Sentry/Datadog host agent than a scheduled
report.

The first watch pass scans the selected transcript tree and records JSONL
`mtime`/size snapshots in SQLite. Later watch passes scan only changed JSONL
files, preventing the daemon from reprocessing the full session history on
every poll. One-shot invocations or watch mode restarts can opt into the same
behavior with `--changed-only`.

`agent_doctor.service` writes the user-service wrapper:

- macOS: `~/Library/LaunchAgents/com.agentdoctor.<platform>.plist`.
- Linux: `~/.config/systemd/user/agent-doctor-<platform>.service`.

The generated service executes `python -m agent_doctor.cli autopilot --watch`
from the installed Python environment. `--start` loads it with launchd or
`systemctl --user enable --now`; without `--start` it only writes the service
file for review.

Before starting, service installation records the current JSONL file snapshots
in the sidecar SQLite state and adds `--changed-only` to the generated service
command by default. This baselines historical transcripts and prevents a new
daemon from emitting old findings into the user inbox. `--no-baseline-existing`
disables that behavior for deliberate backfills.

Delivery stays adapter-free and host-runtime-free:

- `--inbox-dir` writes per-session advisory Markdown files.
- `--notify-command` invokes a local command with `AGENT_DOCTOR_*` environment
  variables pointing at the event/card.
- delivery failures are appended to `delivery-errors.jsonl`, but failed
  interventions are not recorded as handled in SQLite. The next watch pass can
  retry them instead of losing the recovery moment behind cooldown state.

OpenClaw has one built-in delivery adapter:

```bash
agent-doctor notify openclaw-system-event
```

It runs as a notify command, reads the emitted card path from
`AGENT_DOCTOR_CARD`, skips non-`intervene` events by default, resolves OpenClaw
from host command locations such as `/opt/homebrew/bin` even under launchd's
minimal PATH, and calls the public OpenClaw CLI:

```bash
openclaw system event --mode now --text <intervention>
```

This preserves the outside-in boundary: Agent Doctor does not edit OpenClaw
runtime config or require hooks, but high-severity interventions are no longer
left only in `latest.md` / inbox files.

### Agent-Managed Setup

`agent_doctor.setup` is the zero-touch entry point meant for OpenClaw/Hermes (or
another supervising agent) to run on the user's behalf:

```bash
agent-doctor setup autopilot
```

The command uses host-home detection, so it works even when invoked from an
agent sandbox whose `HOME` is nested under `.openclaw` or `.hermes`. It is the
preferred path when a user asks their AI agent to enable proactive diagnosis,
because it performs the same safe operations a careful human would have run
manually:

1. detect OpenClaw/Hermes host roots.
2. run `bootstrap --invalidate-cache` so the host agent can discover the
   Agent Doctor skill.
3. install one launchd/systemd user service per detected platform.
4. baseline current JSONL transcript snapshots before start.
5. for OpenClaw, set the default notify command to
   `agent-doctor notify openclaw-system-event`.
6. start services by default with `--changed-only` enabled.

This is deliberately a wrapper around existing public APIs (`bootstrap` and
`service install`) rather than a runtime integration. The user-visible contract
is "the AI agent can install and configure Agent Doctor for me"; the technical
boundary remains outside-in and reversible. Flags such as `--dry-run`,
`--platform`, `--no-start`, and `--force` make the same path usable for tests,
enterprise packaging, or pre-provisioning.

### One-line installer

`install.sh` (top-level) is the canonical user-facing install path:

```bash
curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh
```

It detects pipx vs `apt-get` vs `brew` vs `pip --user --break-system-packages`, installs pipx first if missing (one visible sudo prompt where required), runs `pipx install` from the GitHub repo (idempotent — `--force` on re-run), optionally injects extras (`--with-mcp` / `--with-llm` / `--with-all`), then runs `agent-doctor bootstrap --invalidate-cache` automatically. `--with-all` means all optional Python extras; combine it with `--with-autopilot` when the installer should also start sidecar services. With `--with-autopilot`, it delegates to `agent-doctor setup autopilot --no-bootstrap` so installer-driven and agent-driven setup share the same service logic. Set `AGENT_DOCTOR_REPO` / `AGENT_DOCTOR_REF` env vars to point at a fork or branch; pass `--skip-bootstrap` to land just the package.

### Ingestion

`agent_doctor.ingest` accepts one JSONL file or a directory of JSONL files. It normalizes Hermes-ish, OpenClaw-ish, and generic event formats into a shared `Message` model with roles `user`, `assistant`, `tool`, `system/metadata`. Messages retain file path, line, session id, source format, and raw type so every finding can cite evidence.

The ingestion layer is the resilience boundary: malformed lines are skipped by default with a `parse_errors` count surfaced to the user; `--strict` re-enables hard-fail. Per-message content is capped at `MAX_CONTENT_CHARS` (8000) so a single huge tool stdout cannot dominate downstream detection.

### Detection and Aggregation

`agent_doctor.detectors` runs deterministic regex / structural rules against normalized messages and emits raw matches. User anger and trust-break language are handled by `agent_doctor.frustration`, a local weighted classifier that reports signal labels without calling an LLM. A second aggregation pass groups raw matches by `(failure_mode, session_id)`: a session with N user complaints of the same kind becomes **one** finding with N evidence quotes attached and severity escalated by count (`>=3 → high`, `2 → bump tier`).

The detectors do not call an LLM and do not modify agent state. False-positive guards live alongside each pattern, all driven by real-world data:

- **Negative phrases.** `0 errors` / `no failures` / `zero exceptions` stripped from tool stdout before matching.
- **JSON success envelopes.** `"error": null`, `"error": ""`, `"stderr": ""`, `"exit_code": 0`, `"status": 0`, `"success": true`, `"ok": true` (both quoted-JSON and bare-prose forms) and the literal `(not an error)` suffix get stripped — real Hermes / OpenClaw tool results carry these on success and would otherwise cause every successful command to flag as a hidden error.
- **Identifier-like trailing chars.** `error_handler.py`, `error.log`, `error_count` excluded from the bare `error` token via `(?![._-]\w)` lookahead.
- **Imperative-position `remember`.** Informational uses like "Just so I remember the timeline …" don't trip `memory_failure`; the regex requires sentence-initial position or a directed pronoun (`you (must|should|need to)?remember`).
- **Source-line references.** `cli.js:403:` does not match as HTTP 403 — the digit must not be surrounded by colons or other digits.
- **Frustration shape.** Insults/profanity, trust-break phrases, and direct quality complaints are high severity; repeated corrections and urgency punctuation are supporting signals and are not enough to create a finding by themselves.

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
| `doctor_pet_status` | JSONL transcripts or current message | optional `pet-status.json` / `pet-card.md` under `out_dir` |
| `doctor_pet_intervene` | JSONL transcripts or current message | optional `pet-status.json` / `pet-card.md` under `out_dir` |

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

Autopilot:
OpenClaw/Hermes/generic transcript JSONL
  → adapter default path or --path
  → ingest + deterministic detectors
  → trigger engine + cooldown state
  → events.jsonl + latest.md + pet-status.json + pet-card.md + cards/<event>.md
  → user/session notification by the host's existing delivery mechanism

Doctor Pet:
current user message or transcript JSONL
  → ingest/synthetic Message
  → deterministic detectors + autopilot event selection
  → PetStatus {state, action, evidence, options}
  → CLI/MCP response or pet-status.json + pet-card.md under --out
```

## Trust Boundary

- All production behavior stays inside the local process and local filesystem.
- Reads come from caller-supplied paths; writes go to caller-supplied output directories only.
- No production code mutates memory, identity, skills, permissions, routing, evals, or host-agent configuration.
- Remote LLMs are only contacted from the opt-in `eval generate --llm` and `eval replay` commands, both gated on `ANTHROPIC_API_KEY` and the `[llm]` extra.
- The MCP server inherits this contract: write tools only write under caller-supplied directories; no tool calls a remote LLM.
- The autopilot sidecar inherits this contract: it reads host transcripts and writes only under `--out` / `--state`. It does not patch OpenClaw/Hermes, install runtime hooks, or block host execution.
