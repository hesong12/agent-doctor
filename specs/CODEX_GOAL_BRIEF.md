# Agent Doctor Goal Brief

Build an open-source MVP called **Agent Doctor**.

## Product positioning

Agent Doctor is a local-first session postmortem and improvement engine for memoryful AI agents such as Hermes and OpenClaw. It reads human-agent session transcripts, detects frustration/correction/failure patterns, and turns them into reviewable patch proposals for memory, identity, skills, SOPs, permissions, routing, and evals.

Core tagline: **Turn frustrating agent sessions into durable fixes.**

Do NOT make it AI therapy, HR performance management, or surveillance analytics. It is engineering diagnosis: evidence -> root cause -> patch proposal -> eval.

## Implementation constraints

- Use Python 3.11+.
- Package name: `agent-doctor`; import module: `agent_doctor`.
- Prefer minimal dependencies. Typer/Rich/Pydantic are OK if you add them correctly.
- Use strict local-first behavior. No network calls. No remote LLM calls.
- Default mode must be read-only: never modify agent memory/identity/skills/config unless an explicit apply command is used. MVP can implement dry-run apply only.
- Every finding must include evidence quotes/spans from the transcript.
- Include tests and make them pass.
- Commit cleanly when done.

## Required MVP features

### CLI

Provide a CLI command executable as:

```bash
agent-doctor scan --path <file-or-directory> --format markdown --out <output-dir>
agent-doctor scan --hermes --format json --out <output-dir>
agent-doctor scan --openclaw --format markdown --out <output-dir>
agent-doctor doctor
```

`scan` should:
- ingest one JSONL file or a directory containing JSONL files;
- auto-detect Hermes/OpenClaw-ish JSONL event formats;
- normalize messages into roles: user, assistant, tool, system/metadata;
- segment at least by session/file;
- detect findings;
- write `report.md`, `findings.json`, and `eval-cases.yaml` into output directory;
- print a concise summary.

`doctor` should print environment/readiness info: Python version, package version, default paths that exist, and privacy mode.

### Default path detection

Support:
- Hermes default: `~/.hermes/sessions`
- OpenClaw default: `~/.openclaw/agents/main/sessions`

### Detection taxonomy

Implement deterministic detectors for these failure modes:

1. `repeated_user_correction`
   - signal examples: "I already told you", "I already told you", "you did it again", "not this", "not what I asked", "again"

2. `execution_discipline`
   - assistant says it will check/run/test/verify/create/update but no tool message follows before next assistant/user message, or user complains about planning instead of acting.

3. `verification_failure`
   - signals: "did you test", "did you test it", "did you verify it", "without verifying", "not verified", "not actually tested"

4. `memory_failure`
   - signals: "you forgot", "you forgot", "remember", "I told you", "last time"

5. `tool_failure_or_hidden_error`
   - tool message contains error/fail/timeout/unauthorized/401/403/500/traceback; assistant later claims success or continues without acknowledging.

6. `communication_mismatch`
   - signals: "too verbose", "stop explaining", "stop explaining", "just do it", "do not just plan", "don't just plan"

Each finding should include:
- id
- severity: low/medium/high
- failure_mode
- title
- evidence: list of `{file, line, role, quote}`
- diagnosis
- recommendations: list of patch proposals
- eval_case
- confidence

### Recommendation mapping

Map failure modes to patch targets:
- repeated_user_correction -> memory or SOP clarification
- execution_discipline -> SOP patch + eval
- verification_failure -> SOP/eval patch
- memory_failure -> memory candidate with evidence
- tool_failure_or_hidden_error -> SOP/tool discipline patch
- communication_mismatch -> communication preference memory or identity guidance, but warn about overfitting if only one instance

### Install helper

Implement:

```bash
agent-doctor install-skill --target hermes --out <dir>
agent-doctor install-skill --target openclaw --out <dir>
```

It should generate a Markdown skill/SOP file only. Do not modify real Hermes/OpenClaw configs in MVP.

The generated skill must instruct the host agent to:
- run Agent Doctor local-only;
- dry-run by default;
- ask before applying patches;
- never paste full transcripts to remote LLM unless the user explicitly approves.

### MCP scaffold

Include a minimal MCP server scaffold module or documented command:

```bash
agent-doctor mcp
```

MVP can print/serve placeholder JSON or document that MCP write tools are disabled. Do not overbuild.

### Tests

Add tests with pytest. Include fixtures for:
- Hermes-like JSONL
- OpenClaw-like JSONL
- generic JSONL

Test:
- ingestion normalizes messages;
- detectors find each required failure mode;
- report files are written;
- install-skill writes safe skill text;
- CLI smoke works.

### Docs

Create:
- README.md with install, examples, privacy model, CLI usage, and positioning.
- docs/architecture.md explaining CLI-first + skill wrapper + optional MCP layers.
- docs/taxonomy.md explaining failure modes and patch targets.

## Suggested project structure

```text
agent-doctor/
  pyproject.toml
  README.md
  src/agent_doctor/
    __init__.py
    cli.py
    schema.py
    ingest.py
    detectors.py
    recommend.py
    report.py
    install.py
    mcp.py
  tests/
    fixtures/
    test_ingest.py
    test_detectors.py
    test_cli.py
  docs/
    architecture.md
    taxonomy.md
```

## Verification commands

Run these before finalizing:

```bash
python -m pytest -q
python -m agent_doctor.cli doctor
python -m agent_doctor.cli scan --path tests/fixtures --out /tmp/agent-doctor-smoke --format markdown
python -m agent_doctor.cli install-skill --target hermes --out /tmp/agent-doctor-skill
```

## Final response expected from Codex

Include:
- files created;
- tests run and results;
- any known limitations;
- commit hash.
