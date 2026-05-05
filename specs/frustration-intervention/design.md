# Technical Design

## Architecture

Add a deterministic local classifier under `agent_doctor.frustration`.
The classifier returns a `FrustrationSignal` with:

- `matched`
- `severity`
- `score`
- `labels`
- `rationale`

The production path remains:

```text
JSONL transcript
  -> ingest Message[]
  -> deterministic detectors + frustration classifier
  -> aggregated Finding[]
  -> autopilot event decision
  -> local cards / inbox / optional notify command
```

No remote LLM is used in `scan`, `autopilot`, `apply`, or MCP tools.

## Detection

The classifier uses weighted local signals:

- profanity or direct insults: high weight
- trust-break language: high weight
- direct quality complaints: high weight
- repeated corrections: low supporting weight
- urgency shape: low weight

This is intentionally more expressive than a single regex, but still
explainable and testable.

## Autopilot

Autopilot upgrades high-severity user frustration from `notify` to `intervene`.
Intervention cards tell the host agent to pause the normal success path and
recover with a short, evidence-based response.

## Reports And Patches

`user_frustration_signal` becomes a normal finding mode. Recommendations target:

- `identity`: change response posture during user anger
- `sop`: pause, diagnose, and answer with evidence
- `eval`: regression case for user-frustration recovery

## Risks

- A phrase list can miss indirect sarcasm. The mitigation is explicit labels
  plus future opt-in local/remote classifier support, not default transcript
  upload.
- Over-triggering can annoy users. The mitigation is severity gating and
  existing cooldown/de-dupe state.
