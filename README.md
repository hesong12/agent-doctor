# Agent Doctor

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)

Local-first session postmortem and improvement engine for **memoryful AI agent frameworks** — agents that have their own persistent identity, memory, skills, and SOP files. Today: **Hermes, OpenClaw, Claude Code**. Same shape works for any framework that records sessions as JSONL and stores its own configuration files.

**Turn frustrating agent sessions into durable fixes.** Read JSONL transcripts → detect failure patterns deterministically → aggregate into one finding per session → stage reviewable patches for memory, SOP, identity, tool discipline, and evals. For productized agent deployments, let the host agent run `agent-doctor setup autopilot` so diagnosis triggers automatically from negative feedback, hidden tool failures, and unverified completion claims. No network calls in the production path. No automatic edits to your agent config.

Agent Doctor is an engineering diagnosis tool. It is *not* therapy, HR performance management, or surveillance analytics, and it is *not* aimed at chat clients without their own memory or identity surface (Claude Desktop, Cursor, Cline, ChatGPT, …) — those have nothing for `apply` to patch.

## Install

**One line.** Detects pipx vs pip, installs Agent Doctor, writes skills into every detected memoryful agent framework on the machine, and invalidates host skill caches so the new skill is live on the next session — no manual restart needed where supported.

```bash
curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh
```

With extras:

```bash
# Include the MCP stdio server
curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh -s -- --with-mcp

# MCP + LLM extras
curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh -s -- --with-all

# Extras plus always-on autopilot sidecars
curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh -s -- --with-all --with-autopilot
```

After install, just say to your AI agent: *"review my last session"* / *"diagnose this transcript"* / *"why does the agent keep doing X"*. The host's skill router will match against the `SKILL.md` we wrote into each detected memoryful framework's skill directory and load Agent Doctor's workflow.

For AI-agent-managed deployments, the agent should use the opinionated setup
command instead of asking the user to configure paths or service managers:

```bash
agent-doctor setup autopilot
```

That command detects OpenClaw/Hermes from the host home, installs or refreshes
Agent Doctor skills, baselines existing transcripts, writes launchd/systemd user
services, starts them by default, and enables changed-file scanning so old
sessions do not flood the inbox. It does not edit OpenClaw/Hermes runtime
configuration.

For always-on deployments where the user should not have to remember to ask for diagnosis, run the sidecar:

```bash
# One-shot check using each platform's default transcript path
agent-doctor autopilot --platform openclaw --out ~/.agent-doctor/openclaw
agent-doctor autopilot --platform hermes --out ~/.agent-doctor/hermes

# Long-running sidecar mode. Use launchd on macOS or systemd on Linux.
agent-doctor autopilot --platform openclaw --out ~/.agent-doctor/openclaw --watch

# Install as a user service without changing OpenClaw/Hermes.
agent-doctor service install --platform openclaw --out ~/.agent-doctor/openclaw \
  --inbox-dir ~/.agent-doctor/inbox/openclaw --start
```

`autopilot` is outside-in: it reads existing transcript/log JSONL, keeps its own SQLite state for cooldown/de-duplication, and writes short diagnosis cards plus `events.jsonl` under `--out`. It does **not** require OpenClaw/Hermes runtime hooks, source changes, or platform cooperation.

If you'd rather not run a remote shell script, the manual package install path is:

```bash
pipx install git+https://github.com/hesong12/agent-doctor.git
agent-doctor bootstrap --invalidate-cache
# Optional always-on sidecars after manual install:
agent-doctor setup autopilot
```

(On Ubuntu 24.04+ / Debian 12+ install pipx first: `sudo apt-get install -y pipx && pipx ensurepath`. The `install.sh` script handles this automatically.)

`bootstrap` auto-detects `~/.hermes`, `~/.openclaw`, and `~/.claude/skills` and writes a `SKILL.md` with YAML frontmatter into the right place for each host. Hermes gets `~/.hermes/skills/autonomous-ai-agents/agent-doctor/SKILL.md` (the categorized layout 124+ existing Hermes skills use); OpenClaw and Claude Code get `~/.<host>/skills/agent-doctor/SKILL.md`. It also prints the MCP configuration snippet you can paste into any MCP-aware host.

For local development:

```bash
git clone https://github.com/hesong12/agent-doctor.git
cd agent-doctor
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
```

The package exposes the `agent-doctor` console script and can also be run with `python3 -m agent_doctor.cli`.

## For AI agents

Read this section if you are an AI agent inside a memoryful framework (Hermes, OpenClaw, Claude Code, …) deciding whether to invoke Agent Doctor.

**When to invoke:**

| User intent | Command |
|---|---|
| "review / diagnose / postmortem my last session" | `agent-doctor scan --hermes --format markdown --out ./postmortem` (or `--openclaw`, or `--path <jsonl-or-dir>`) |
| "why does the agent keep doing X" | same `scan`; look for findings with `count >= 3` and severity `high` |
| "fix the patterns you found" | `agent-doctor apply --findings ./postmortem --out ./staging --target <live-config-dir>` |
| "enable proactive diagnosis / install Agent Doctor autopilot" | `agent-doctor setup autopilot` |
| "is the detector accurate / measure improvement" | `agent-doctor eval generate` → `eval bench` → `eval replay` |

**Operating rules** (these mirror the SKILL.md `bootstrap` installs):

1. **Local-only.** `scan`, `apply`, `bootstrap`, and `mcp serve` make no network calls. The only commands that contact a remote LLM are `eval generate --llm` and `eval replay`, both gated on `ANTHROPIC_API_KEY` and the `[llm]` extra.
2. **Treat patch output as dry-run.** `apply` writes to a staging directory; live host-agent config is never modified. Always ask the user before copying staged patches into memory / identity / SOPs / skills / permissions / routing / evals.
3. **Autopilot setup is reversible host-side setup, not a host runtime edit.** `setup autopilot` may install Agent Doctor skills, local state, inbox files, and user-level launchd/systemd services. It must not edit OpenClaw/Hermes runtime config.
4. **Never paste full transcripts to a remote LLM** unless the user explicitly approves that disclosure.
5. **Cite evidence.** Findings include file paths, line numbers, role, and quoted excerpts. Prefer those over broad claims about the user or the agent.

**MCP-native invocation** (if the host speaks MCP and the `[mcp]` extra is installed):

The server exposes six tools (`scan`, `list_findings`, `read_finding`, `bench`, `stage_patches`, `generate_corpus`). All write tools restrict writes to caller-supplied `staging_dir` / `out_dir`. No tool calls a remote LLM. See [MCP server](#mcp-server) below.

**One-shot golden flow** an agent can run end-to-end:

```bash
agent-doctor scan --path ./sessions --format markdown --out ./postmortem
agent-doctor apply --findings ./postmortem --out ./staging --target ~/.hermes/skills --min-severity medium
# Then summarize ./staging/sop.md, ./staging/memory.md, and ./staging/DIFF.txt
# back to the user and ask which sections to copy into the live config.
```

## CLI quickstart

### Diagnose

```bash
agent-doctor scan --path ./sessions --format markdown --out ./agent-doctor-report
agent-doctor scan --hermes --format json --out ./agent-doctor-hermes
agent-doctor scan --openclaw --format markdown --out ./agent-doctor-openclaw
```

`scan` writes three files:

- `report.md` — human-readable summary with redacted evidence quotes.
- `findings.json` — machine-readable findings.
- `eval-cases.yaml` — starter eval cases.

Findings are aggregated per `(failure_mode, session_id)`. A session with 20 user complaints of the same kind becomes **one** high-severity finding with all 20 evidence quotes attached, not 20 separate medium findings. Severity escalates by count.

Add `--strict` to fail on malformed JSONL lines instead of silently skipping them; the default behavior surfaces the skipped count in the summary.

### Stage reviewable patches

```bash
agent-doctor apply --findings ./agent-doctor-report --out ./staging
agent-doctor apply --findings ./agent-doctor-report --out ./staging --target ~/.hermes/skills
```

`apply` reads `findings.json` and writes a staging directory:

```
staging/
  memory.md          # one block per memory candidate
  sop.md             # SOP guidance, grouped by failure mode
  identity.md        # identity / communication-style guidance
  tool-discipline.md # tool-discipline guards
  eval/<id>.yaml     # one starter eval case per finding
  MANIFEST.json      # finding → emitted file mapping
  DIFF.txt           # unified diff vs --target (if given)
```

**Nothing is applied automatically.** Real Hermes / OpenClaw configuration is never touched. The staging directory is a curated copy-paste source plus a unified diff against your live config so you can see exactly what would change. Use `--min-severity` and `--min-count` to filter noise.

### Readiness, install, MCP

```bash
agent-doctor doctor                                          # environment + privacy info
agent-doctor setup autopilot                                 # auto-detect hosts, install skills, start sidecars
agent-doctor autopilot --platform openclaw --out ~/.agent-doctor/openclaw
agent-doctor autopilot --platform hermes --out ~/.agent-doctor/hermes --watch
agent-doctor service install --platform openclaw --out ~/.agent-doctor/openclaw --start
agent-doctor bootstrap                                       # auto-detect hosts and install skills into each
agent-doctor bootstrap --dry-run                             # preview without writing
agent-doctor bootstrap --target claude-code --force          # force into a specific host
agent-doctor install-skill --target hermes --out ./skills    # write a single skill file by hand
agent-doctor mcp                                             # print MCP server metadata + tool list
pip install 'agent-doctor[mcp]' && agent-doctor mcp serve    # run the stdio MCP server
```

Supported `--target` values: `hermes`, `openclaw`, `claude-code`, `generic`. Hermes, OpenClaw, and Claude Code get a `SKILL.md` with YAML frontmatter in the host's expected skill location; `generic` writes a flat Markdown SOP file.

### Autopilot sidecar

`autopilot` is the no-runtime-modification product path. It is intended to run as a local daemon, not as a dashboard and not as a cron-only batch job:

```bash
agent-doctor autopilot --platform openclaw --out ~/.agent-doctor/openclaw --watch --interval 15
agent-doctor autopilot --platform hermes --out ~/.agent-doctor/hermes --watch --interval 15
agent-doctor autopilot --platform generic --path ./sessions --out ./doctor-autopilot
```

For an AI agent configuring Agent Doctor on the user's behalf, prefer:

```bash
agent-doctor setup autopilot
```

This is the zero-touch setup flow: it detects OpenClaw/Hermes, runs bootstrap,
best-effort invalidates host skill caches, installs launchd/systemd user
services, baselines existing transcripts, starts the services, and writes
advisory inbox files under `~/.agent-doctor/inbox/<platform>`. Use `--dry-run`
to preview, `--platform openclaw` / `--platform hermes` to limit scope,
`--no-start` to only write service files, and `--force` when provisioning a host
home before the platform has created its root directory.

Current automatic triggers:

- user negative feedback, including direct complaints like "not useful", "no value", "not thinking", and common Chinese equivalents.
- assistant completion claims without nearby verification evidence.
- hidden or unacknowledged tool failures surfaced by the deterministic detectors.

Watch mode automatically runs a full first pass, then switches to changed-file
scanning using JSONL path, `mtime`, and size state in SQLite. To skip unchanged
files on the first pass as well (for example, for one-shot batch jobs or daemon
restarts), pass `--changed-only`.

Artifacts:

```
~/.agent-doctor/openclaw/
  state.sqlite3       # local de-dupe / cooldown state
  events.jsonl        # machine-readable emitted interventions
  latest.md           # most recent short diagnosis card
  cards/<event>.md    # one card per emitted event
```

This is the Agent Doctor "self-healing layer" boundary: observe from the outside, diagnose locally, notify through existing channels, and stage durable fixes. It does not block host runtime execution or patch live configuration.

Delivery options stay outside the host runtime:

```bash
agent-doctor autopilot --platform openclaw --out ~/.agent-doctor/openclaw \
  --inbox-dir ~/.agent-doctor/inbox/openclaw \
  --notify-command "/usr/local/bin/send-agent-doctor-card"
```

- `--inbox-dir` writes a per-session advisory file that a memoryful agent can read on its next turn or heartbeat.
- `--notify-command` runs a local command after a card is emitted. Metadata is passed through `AGENT_DOCTOR_*` environment variables such as `AGENT_DOCTOR_CARD`, `AGENT_DOCTOR_TRIGGER`, `AGENT_DOCTOR_SEVERITY`, and `AGENT_DOCTOR_SESSION_ID`.
- Delivery failures are recorded in `delivery-errors.jsonl`; diagnosis itself still succeeds.

Install as a background user service:

```bash
# macOS: writes ~/Library/LaunchAgents/com.agentdoctor.openclaw.plist
# Linux: writes ~/.config/systemd/user/agent-doctor-openclaw.service
agent-doctor service install --platform openclaw --out ~/.agent-doctor/openclaw \
  --inbox-dir ~/.agent-doctor/inbox/openclaw --start
```

Service installation baselines existing transcript files before starting by
default and starts the service with changed-file scanning enabled, so a fresh
sidecar does not flood the inbox with historical findings. Pass
`--no-baseline-existing` when you intentionally want the service to scan old
sessions as soon as it starts.

The installer also supports this as an opt-in:

```bash
curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh -s -- --with-autopilot
```

### Eval harness (LLM-first)

```bash
# 1. Generate synthetic transcripts with ground-truth labels.
agent-doctor eval generate --cards tests/fixtures/cards --out ./corpus

# 2. Benchmark detector P/R/F1 against the labels.
agent-doctor eval bench --corpus ./corpus --out ./bench \
  --gate-precision 0.95 --gate-recall 0.85

# 3. Closed-loop replay: apply patches, re-run user turns through a patched agent.
ANTHROPIC_API_KEY=... agent-doctor eval replay \
  --transcript ./sessions/frustrating.jsonl \
  --patches ./staging \
  --out ./replay
```

The eval pipeline is deliberately separated from the production scan path so the local-first guarantee is preserved. See [`docs/evaluation.md`](docs/evaluation.md) for the full framework, including scenario card schema, distractor kinds, the LLM-backed generator, and CI gating.

## Privacy model

Agent Doctor is local-only by design.

- The `scan`, `apply`, and `eval generate` (without `--llm`) commands make no network calls and do not call remote LLMs.
- Default operation is read-only against agent state and transcript inputs.
- `apply` writes patches into a staging directory only; it never edits your live agent config.
- Reports redact common secrets, API keys, bearer tokens, and passwords by default.
- All artifacts are written with `0o600` permissions.
- The generated host-agent SOP explicitly tells agents not to paste full transcripts to a remote LLM unless the user approves that disclosure.
- `eval generate --llm` and `eval replay` are the only commands that contact a remote LLM, and only when an `ANTHROPIC_API_KEY` is present and the `[llm]` extra is installed. They live in the `agent_doctor.evals` subpackage to keep the production path dependency-free.

## Detection taxonomy

| Failure mode | Signal | Patch targets |
|---|---|---|
| `repeated_user_correction` | "I already told you", "not what I asked", "X again" | memory, SOP |
| `execution_discipline` | promised action without observed tool execution; "don't just plan" | SOP, eval |
| `verification_failure` | "did you test", "without verifying", "not actually tested" | SOP, eval |
| `memory_failure` | "you forgot", imperative "remember", "last time", "I told you" | memory |
| `tool_failure_or_hidden_error` | tool emits error/timeout/401/500/traceback; assistant claims success without acknowledging | SOP, tool discipline |
| `communication_mismatch` | "too verbose", "stop explaining" | memory (with overfit warning), identity |

Distractors that are deliberately *not* flagged:

- "Just so I remember the timeline …" (informational `remember`).
- Tool output containing "0 errors" / "no failures".
- Identifier-like terms such as `error_handler.py` or `error.log`.
- Assistant offers like "I can run … if you want" (capability, not promise).

The bench corpus under `tests/fixtures/cards/` includes a distractor-only scenario; CI fails if any of these false-positives reappear.

## Supported inputs

The MVP ingests JSONL files. It auto-detects Hermes-ish, OpenClaw-ish, and generic event shapes using common fields such as `role`, `actor`, `speaker`, `payload`, `data`, `entry`, `content`, `message`, `text`, `output`, and `error`. Nested `message.role` and `message.content` values are normalized under common `message`, `data`, `entry`, and `payload` containers.

Default paths:

- Hermes: `~/.hermes/sessions`
- OpenClaw: `~/.openclaw/agents/main/sessions`

Per-message content is capped at 8,000 characters so a single huge tool stdout cannot dominate downstream detection.

## Development

```bash
python3 -m pytest -q
```

Smoke commands:

```bash
python3 -m agent_doctor.cli doctor
python3 -m agent_doctor.cli scan --path tests/fixtures --out /tmp/agent-doctor-smoke --format markdown
python3 -m agent_doctor.cli apply --findings /tmp/agent-doctor-smoke --out /tmp/agent-doctor-staging
python3 -m agent_doctor.cli eval generate --cards tests/fixtures/cards --out /tmp/ad-corpus
python3 -m agent_doctor.cli eval bench --corpus /tmp/ad-corpus --out /tmp/ad-bench
```

## MCP server

Agent Doctor ships a stdio MCP server that exposes the same diagnosis surface as the CLI. Install the optional extra and let any memoryful MCP-aware agent framework (Hermes, OpenClaw, Claude Code, or any framework that supports MCP tools and has its own memory / identity files) call it mid-session:

```bash
pip install 'agent-doctor[mcp]'
agent-doctor mcp serve   # stdio server; configure your host with the snippet from `bootstrap`
```

Tools exposed:

| Tool | Reads | Writes |
|---|---|---|
| `scan` | JSONL transcripts | report artifacts under `out_dir` |
| `list_findings` | `findings.json` | — |
| `read_finding` | `findings.json` | — |
| `bench` | corpus dir | `bench.json`, `bench.md` under `out_dir` |
| `stage_patches` | `findings.json` (+ optional read-only `target_dir`) | `staging_dir` only |
| `generate_corpus` | scenario cards | corpus under `out_dir` |

The trust boundary matches the CLI: write tools never touch live host-agent configuration, only `staging_dir` / `out_dir`. No tool calls a remote LLM — the LLM-augmented generator is a CLI-only path on purpose.

`agent-doctor mcp` (no subcommand) prints the metadata and tool list as JSON.

## What's still ahead

- Cross-session aggregation for stronger memory candidates.
- LLM-augmented detection layer (opt-in second pass for sarcasm / indirect signals).
- Annotation UI for the real-data half of the golden corpus.
- Judge-LLM recommendation rubric runner (described in `docs/evaluation.md`).
- Calibrated confidence model and severity from labeled data.
