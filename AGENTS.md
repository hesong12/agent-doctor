# AGENTS.md — Notes for AI agents working on this repo

This file is for AI agents (Claude Code, Codex, Hermes, OpenClaw, or any code-modifying agent) that have been asked to read, modify, test, or extend Agent Doctor itself. If you are an agent inside a memoryful framework that wants to *use* Agent Doctor as a tool, read [`README.md`](README.md) and the `SKILL.md` that `agent-doctor bootstrap` writes into your host instead.

## What this project is

Agent Doctor is a local-first session postmortem and improvement engine. It reads JSONL session transcripts, deterministically detects seven failure modes, aggregates repeats per `(failure_mode, session_id)`, and stages reviewable patches for memory / SOP / identity / tool discipline / evals. It also exposes an MCP stdio server so other agents can call it mid-session.

Core invariants — do not violate these without an explicit user request:

- **Local-first.** `scan`, `apply`, `bootstrap`, `mcp serve`, and `eval generate` (without `--llm`) make no network calls. Never add a remote call to one of these paths.
- **Dependency-free production path.** The detection / report / apply path imports nothing outside the standard library. Optional integrations live behind `[llm]` and `[mcp]` extras with lazy imports.
- **Never modify live host-agent config.** `apply` writes to `staging_dir`. `bootstrap` writes to host skill directories — that is the trust boundary, do not extend it. MCP write tools must only write under caller-supplied `staging_dir` / `out_dir`.
- **Redact secrets.** All transcript-derived strings pass through `agent_doctor.redaction` before being written to disk. Add new secret patterns there, not at call sites.
- **Immutability.** Dataclasses are `frozen=True`. Never mutate `Message`, `Evidence`, `Finding`, `ScanResult`. Build new instances.

## Layout

```
install.sh           # one-line installer (curl … | sh) for end users
LICENSE              # MIT
agent_doctor/
  __init__.py        # version
  schema.py          # frozen dataclasses (Message, Evidence, Finding, ScanResult)
  ingest.py          # JSONL → Message[] (handles typed-part content arrays)
  detectors.py       # Message[] → raw matches → aggregated Findings
  recommend.py       # Finding → patch proposals + eval cases
  report.py          # write report.md / findings.json / eval-cases.yaml
  apply.py           # findings.json → staging dir + DIFF.txt
  install.py         # unified SKILL.md template (YAML frontmatter + body)
  bootstrap.py       # auto-detect hosts + invalidate caches
  autopilot.py       # outside-in sidecar trigger engine
  service.py         # launchd/systemd user-service installer for autopilot
  mcp.py             # MCP stdio server + pure-Python tool handlers
  redaction.py       # secret redaction patterns
  cli.py             # argparse subcommands; thin wrappers around modules
  evals/
    cards.py         # YAML scenario card loader (custom minimal parser)
    generator.py     # template + optional LLM generator
    metrics.py       # P/R/F1, severity-match, confusion matrix
    bench.py         # corpus → bench.json + bench.md
    replay.py        # closed-loop delta with patched agent
docs/
  architecture.md    # high-level layering
  taxonomy.md        # detection failure modes (canonical reference)
  evaluation.md      # LLM-first eval framework
tests/
  fixtures/          # JSONL transcripts + scenario cards
  test_*.py          # one file per major area
```

## How to make changes

1. **Read first.** Look at the relevant module *and* its tests before editing. Dataclass changes ripple through `report.py`, `apply.py`, `mcp.py`, and the eval pipeline — verify each.
2. **TDD when adding behavior.** Write or extend a test in `tests/test_<area>.py`, run it, then make it pass.
3. **Run the bench when touching detection.** `python3 -m agent_doctor.cli eval generate --cards tests/fixtures/cards --out /tmp/c && python3 -m agent_doctor.cli eval bench --corpus /tmp/c --out /tmp/b --gate-precision 0.95 --gate-recall 0.85`. The bench is the regression gate for `detectors.py`.
4. **Keep files small.** Aim for 200–400 lines per module, 800 absolute max. Extract helpers to a new file rather than letting one module sprawl.
5. **Update docs in the same PR.** When you add a CLI command, update `README.md`, `docs/architecture.md`, and (if it changes the user-facing flow) the SKILL templates in `agent_doctor/install.py`. When you change the failure taxonomy, update `docs/taxonomy.md`.

## Test commands

```bash
python3 -m pytest -q                     # full suite (must be green before commit)
python3 -m agent_doctor.cli doctor       # quick sanity check
python3 -m agent_doctor.cli scan --path tests/fixtures --out /tmp/scan-smoke --format markdown
python3 -m agent_doctor.cli apply --findings /tmp/scan-smoke --out /tmp/stage-smoke
python3 -m agent_doctor.cli eval generate --cards tests/fixtures/cards --out /tmp/corpus
python3 -m agent_doctor.cli eval bench --corpus /tmp/corpus --out /tmp/bench
```

## Common review failures

- Adding `print` statements to non-CLI modules. Library code is silent; CLI handlers in `cli.py` may print, and `_print_first_run_hint_if_needed` writes to stderr only.
- Catching `Exception` and swallowing it. Use specific exceptions; let unexpected errors propagate so they are visible. The one exception is `_print_first_run_hint_if_needed`, which must never crash the CLI.
- Importing optional extras (`anthropic`, `mcp`) at module top level. Always lazy-import inside the function that needs them.
- Mutating dataclass fields. Build a new instance.
- Writing to a path that is not under the caller-supplied output directory. Bootstrap is the only writer outside that contract — it writes inside known per-host skill directories. Cache invalidation only deletes / touches files we ourselves placed semantics on (`.skills_prompt_snapshot.json`, `.claude/skills/` mtime).
- Hard-coding host-runtime restart instructions. We can't restart Hermes/OpenClaw/Claude Code from a CLI tool — `bootstrap --invalidate-cache` does the best-effort cache poke, the user provides the restart if their host doesn't auto-reload.

## Out of scope for now

- Cross-session aggregation (next phase).
- LLM-augmented detection (next phase, opt-in second pass only).
- Annotation UI for real-data corpus (next phase).
- Judge-LLM rubric runner (described in `docs/evaluation.md`, not yet implemented).

If a request asks for one of these, it is fair game — but flag it as a phase-3 item before starting so the user can confirm scope.
