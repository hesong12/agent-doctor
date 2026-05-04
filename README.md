# Agent Doctor

Agent Doctor is a local-first session postmortem and improvement engine for memoryful AI agents such as Hermes and OpenClaw.

**Turn frustrating agent sessions into durable fixes.**

It reads JSONL session transcripts, detects deterministic frustration and failure patterns, and writes reviewable recommendations for memory, identity guidance, skills, SOPs, tool discipline, routing, permissions, and evals. It is an engineering diagnosis tool: evidence -> root cause -> patch proposal -> eval.

Agent Doctor is not AI therapy, HR performance management, or surveillance analytics.

## Install

Agent Doctor requires Python 3.11 or newer.

```bash
python3 -m pip install -e .
python3 -m pip install -e ".[dev]"
```

The package exposes the `agent-doctor` console script and can also be run with `python3 -m agent_doctor.cli`.

## CLI Usage

Scan one JSONL file or a directory of JSONL files:

```bash
agent-doctor scan --path ./sessions --format markdown --out ./agent-doctor-report
agent-doctor scan --path ./sessions/session.jsonl --format json --out ./agent-doctor-report
```

Use known default transcript locations:

```bash
agent-doctor scan --hermes --format json --out ./agent-doctor-hermes
agent-doctor scan --openclaw --format markdown --out ./agent-doctor-openclaw
```

Print readiness and privacy information:

```bash
agent-doctor doctor
```

Generate a safe host-agent SOP file:

```bash
agent-doctor install-skill --target hermes --out ./skills
agent-doctor install-skill --target openclaw --out ./skills
```

Inspect the minimal MCP placeholder:

```bash
agent-doctor mcp
```

## Outputs

`scan` writes three files into the output directory:

- `report.md`: human-readable summary, redacted evidence quotes, diagnoses, and recommendations.
- `findings.json`: structured redacted findings for review or downstream tooling.
- `eval-cases.yaml`: starter eval cases based on detected failure modes.

Every finding includes transcript evidence with file, line, role, and quote. Report artifacts are written with `0600` file permissions. Evidence quotes are redacted by default, but they are still transcript excerpts; review artifacts before sharing them outside your machine.

## Privacy Model

Agent Doctor is local-only by design.

- It makes no network calls.
- It does not call remote LLMs.
- Default operation is read-only against agent state and transcript inputs.
- Report output redacts common secrets, API keys, bearer tokens, and passwords by default.
- The MVP install helper only writes a Markdown SOP file to the requested output directory.
- Proposed memory, identity, skill, SOP, permission, routing, and eval patches are review artifacts. They are not applied automatically.

The generated host-agent SOP explicitly tells agents not to paste full transcripts to a remote LLM unless the user approves that disclosure.

## Supported Inputs

The MVP ingests JSONL files. It auto-detects Hermes-ish, OpenClaw-ish, and generic event shapes using common fields such as `role`, `actor`, `speaker`, `payload`, `data`, `entry`, `content`, `message`, `text`, `output`, and `error`. Nested `message.role` and `message.content` values are normalized under common `message`, `data`, `entry`, and `payload` containers.

Default paths:

- Hermes: `~/.hermes/sessions`
- OpenClaw: `~/.openclaw/agents/main/sessions`

## Development

Run the test suite:

```bash
python3 -m pytest -q
```

Smoke commands:

```bash
python3 -m agent_doctor.cli doctor
python3 -m agent_doctor.cli scan --path tests/fixtures --out /tmp/agent-doctor-smoke --format markdown
python3 -m agent_doctor.cli install-skill --target hermes --out /tmp/agent-doctor-skill
```

## MVP Limitations

- Detection is deterministic and intentionally conservative; it does not infer hidden intent.
- JSONL normalization covers common event shapes, not every proprietary transcript schema.
- MCP support is a placeholder with write tools disabled.
- Apply mode is deliberately not implemented in the MVP.
