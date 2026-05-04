# Agent Doctor Architecture

Agent Doctor is CLI-first. The MVP keeps the trust boundary simple: transcript files are read locally, deterministic detectors run in-process, and report artifacts are written to a user-selected output directory.

## Layers

### CLI Core

`agent_doctor.cli` provides the public commands:

- `scan`: ingest JSONL transcripts, detect findings, and write reports.
- `doctor`: print environment, version, default path, and privacy readiness information.
- `install-skill`: generate a safe Markdown SOP file for a host agent.
- `mcp`: print placeholder MCP metadata with write tools disabled.

The console script is `agent-doctor`; the same commands can run through `python3 -m agent_doctor.cli`.

### Ingestion

`agent_doctor.ingest` accepts one JSONL file or a directory of JSONL files. It normalizes common Hermes-ish, OpenClaw-ish, and generic event formats into a shared `Message` model:

- `user`
- `assistant`
- `tool`
- `system/metadata`

Messages retain file path, line number, session id, source format, and raw type so every finding can cite evidence.

Ingestion walks common nested containers, including `message`, `data`, `entry`, and `payload`, so rows with nested `message.role` and `message.content` fields normalize into the same model. OpenClaw source detection also checks the full input path and UUID-named nested message rows.

### Detection

`agent_doctor.detectors` contains deterministic rules for the MVP taxonomy. It scans normalized messages for user complaint signals, promised actions without observed tool execution, and tool errors that are not acknowledged before later assistant success claims.

The detectors do not call an LLM and do not modify agent state.

### Recommendations and Evals

`agent_doctor.recommend` maps each failure mode to reviewable patch proposals and starter eval cases. These are recommendations, not automatic changes.

Patch target examples:

- memory
- identity
- SOP
- tool discipline
- eval

### Reports

`agent_doctor.report` writes:

- `report.md`
- `findings.json`
- `eval-cases.yaml`

The Markdown report is meant for human review. JSON and YAML outputs are meant for downstream tooling or eval authoring.

Report artifacts are written with `0600` permissions. Transcript-derived strings are redacted by default for common secrets and tokens, but evidence remains transcript excerpts and should be reviewed before sharing.

### Skill Wrapper

`agent_doctor.install` writes a host-agent SOP file for Hermes or OpenClaw. It instructs the host agent to run Agent Doctor locally, treat output as dry-run, ask before applying patches, and avoid sending full transcripts to remote LLMs without explicit approval.

The MVP does not edit real Hermes or OpenClaw configuration.

### Optional MCP Layer

`agent_doctor.mcp` is a scaffold only. It exposes placeholder metadata that documents local-only privacy mode and disabled write tools. A future MCP layer can wrap read-only scan functionality first, then add explicitly gated write tools later.

## Data Flow

```text
JSONL transcript(s)
  -> ingest and normalize messages
  -> deterministic detectors
  -> recommendation and eval mapping
  -> report.md, findings.json, eval-cases.yaml
  -> human review
  -> optional manual patch application outside the MVP
```

## Trust Boundary

All MVP behavior stays inside the local process and local filesystem. Agent Doctor reads transcript inputs and writes report outputs only where the user points it. It does not mutate memory, identity, skills, permissions, routing, evals, or host-agent configuration.
