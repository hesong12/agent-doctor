# Agent Doctor — Evaluation Framework

Agent Doctor's production scan path is deterministic and offline. Evaluation
of whether that path produces *useful* findings, however, benefits enormously
from LLMs. This document describes how the `agent-doctor eval` subsystem is
structured so we can answer three questions empirically:

1. **Does it find the right problems?** (precision / recall / F1 vs ground
   truth)
2. **Are the recommendations actually good?** (judge-LLM rubric scoring)
3. **Does applying the recommendations improve the next session?**
   (closed-loop replay delta)

LLMs play four orthogonal roles in the eval pipeline; the production tool
itself never calls one. That separation is what lets the local-first guarantee
coexist with rigorous measurement.

## Pipeline overview

```
┌──────────────┐   ┌──────────────┐    ┌────────────────────┐
│ Scenario     │ → │ Generator    │ →  │ Synthetic corpus    │
│ cards (yaml) │   │ (template /  │    │ (transcripts +      │
└──────────────┘   │  Claude)     │    │  ground-truth labels│
                   └──────────────┘    └─────────┬───────────┘
┌──────────────┐                                 │
│ Real, redacted│ ───── annotated ──────►        │
│ transcripts   │                                ▼
└──────────────┘                  ┌─────────────────────────┐
                                  │  agent-doctor scan      │
                                  │  (deterministic SUT)    │
                                  └────────────┬────────────┘
                                               │
                       ┌───────────────────────┼─────────────────────────┐
                       ▼                       ▼                         ▼
              ┌─────────────────┐   ┌──────────────────────┐   ┌─────────────────┐
              │ bench:          │   │ judge:               │   │ replay:         │
              │ P / R / F1 vs   │   │ rubric-rate findings │   │ before/after    │
              │ labels          │   │ + recommendations    │   │ delta with      │
              │ (deterministic) │   │ (LLM)                │   │ patched agent   │
              └─────────────────┘   └──────────────────────┘   └─────────────────┘
```

## Scenario cards

A scenario card is the deterministic seed for one synthetic transcript. It
declares the task, personas, length, output format, and — critically — the
ground-truth failure modes and distractors. Cards are YAML or JSON.

```yaml
id: SCEN-001
task: fix the broken auth middleware and add a regression test
agent_persona: memoryful coding agent, prone to over-narration
user_persona: senior eng, terse, hates planning-without-action
length_turns: 8
output_format: hermes
seeded_failures:
  - mode: execution_discipline
    turn: 3
    severity: medium
distractors:
  - kind: i_can_offer
    turn: 5
```

Valid `mode` values match the production failure taxonomy
(`docs/taxonomy.md`); invalid modes are rejected at load time so the corpus
cannot drift from what the detectors actually emit.

Valid `distractor` kinds:

- `remember_in_neutral_context` — informational use of "remember" that must
  *not* trigger `memory_failure`.
- `zero_errors_in_tool` — tool output containing "0 errors" / "no failures"
  that must *not* trigger `tool_failure_or_hidden_error`.
- `i_can_offer` — assistant offering with "I can ..." that must *not* trigger
  `execution_discipline`.
- `no_problem_filler` — assistant saying "No problem" without actually
  acknowledging an error (used to test the dismissive-non-ack rule).

A bank of cards covering combinations of seeded modes + distractors lives in
`tests/fixtures/cards/`. New cards go there.

## Generator

```bash
agent-doctor eval generate --cards tests/fixtures/cards --out ./corpus
```

The default generator is **template-based**: it stitches pre-written user /
assistant / tool turns and injects each seeded failure at its declared turn.
It is fully deterministic given a seed and requires no API key. The
template generator is what runs in CI.

For higher-fidelity transcripts, opt in with `--llm`:

```bash
ANTHROPIC_API_KEY=... agent-doctor eval generate --cards ... --out ... --llm
```

The LLM is given the card and asked to write a natural-sounding transcript
that satisfies the seeded failures. The output is **structurally validated**
against the same regexes the production detectors use — every seeded failure
must be locatable by the detector, otherwise we fall back to the template
generator and surface the fallback in the corpus summary. This keeps the
generator honest: it cannot quietly produce transcripts where the seeded
ground truth is absent from the text.

The corpus output layout:

```
corpus/
  INDEX.json                # manifest: per-scenario paths, label counts
  transcripts/<scenario>.jsonl
  labels/<scenario>.json    # ground-truth labels per scenario
```

Labels record the canonical mapping from a synthetic line to its seeded
failure mode and severity. The bench joins findings against this.

## Detection benchmark

```bash
agent-doctor eval bench --corpus ./corpus --out ./bench
```

Runs the production detectors against every transcript in the corpus and
compares aggregated findings to ground-truth labels. A finding for mode `M`
in session `S` is a true positive if there exists at least one labeled
occurrence of `M` in `S`. A single aggregated finding can satisfy multiple
labels — aggregation is intentional, and the metric accounts for that.

Outputs:

- `bench.json` — full report with per-mode TP / FP / FN, precision / recall /
  F1, severity-match rate, confusion matrix, and the full match list.
- `bench.md` — human-readable summary suitable for PR descriptions.

CI gates:

```bash
agent-doctor eval bench --corpus ./corpus --out ./bench \
  --gate-precision 0.95 --gate-recall 0.85
```

If any per-mode metric falls below its gate, the command exits non-zero.
This is the regression gate for detector changes — every PR that touches
`detectors.py` is expected to leave the bench numbers either flat or
improved.

## Replay loop

```bash
ANTHROPIC_API_KEY=... agent-doctor eval replay \
  --transcript ./sessions/frustrating.jsonl \
  --patches ./staging \
  --out ./replay
```

The replay loop measures the *closed-loop* value of Agent Doctor: did
applying the staged patches actually change the next session for the better?

It works by:

1. Re-running detectors against the original transcript (`baseline`).
2. Building a "patched" system prompt from the contents of the staging
   directory (`memory.md`, `sop.md`, `identity.md`, `tool-discipline.md`).
3. Driving an LLM through the same user turns under that patched system
   prompt, recording the assistant's responses as a new transcript.
4. Re-running detectors against the replay (`replay`).
5. Computing per-mode delta in finding count and severity distribution.

Without an `ANTHROPIC_API_KEY`, replay records the baseline stats only and
explains why it skipped the LLM step — there is no fabricated data path.

## Metrics summary

| Metric | What it tells you |
|---|---|
| Per-mode precision | When detector says "this is mode X", how often is it right? |
| Per-mode recall    | When the corpus has mode X, how often does the detector catch it? |
| Severity-match rate | Of true positives, what fraction land within ±1 severity of label? |
| Confusion matrix   | When the detector misfires, which mode does it confuse for which? |
| Replay delta       | After applying patches, did the next session contain fewer findings of each mode? |

## Suggested workflow

| Phase | Action |
|---|---|
| Building a new detector | Write at least one card per signal the detector should catch and one distractor card it must *not* fire on. Run `eval bench` until both fire correctly. |
| Tuning aggregation | Add cards with the same mode at multiple turns; verify the aggregated finding has the expected count and escalated severity. |
| Reviewing a PR | Run `eval bench` against the corpus; require `--gate-precision 0.95 --gate-recall 0.85` (or whatever the project standard is). Compare `bench.md` to `main`. |
| Validating apply | Run `agent-doctor scan` on real transcripts → `agent-doctor apply` to stage patches → `eval replay` for the closed-loop delta. |
| Releasing | Generate the corpus once, snapshot `bench.json`, archive in `docs/bench-history/<date>.json` so you can show the trend over time. |

## Limitations

- Template-mode synthetic transcripts undersample real linguistic variety.
  The LLM mode is closer to real distribution but costs API budget per card.
- Real-data half of the golden corpus (annotated production transcripts)
  is not yet wired up; expect to add a `eval annotate` UI in a follow-up.
- The judge-LLM rubric (recommendation quality scoring) is described in this
  doc but is not yet implemented as a CLI subcommand. The replay loop covers
  the most decision-relevant question; the judge will land in the next phase.
