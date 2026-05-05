# Failure Taxonomy

Agent Doctor uses deterministic failure modes. Each finding includes evidence quotes with file, line, role, diagnosis, recommendation proposals, an eval case, an aggregated occurrence count, and a confidence value.

Findings are aggregated per `(failure_mode, session_id)`: a session with N raw matches of the same mode becomes **one** finding with N evidence quotes attached and severity escalated by count (`>=3 → high`, `2 → bump tier`).

## repeated_user_correction

Signals include phrases such as "I already told you", "you did it again", "not this", "not what I asked", and contextual repeat complaints like "you missed it again". A standalone "again" is not enough on its own.

Patch targets:

- `memory` — capture stable user preferences only when transcript evidence supports them.
- `sop` — clarify how to handle corrections and conflicting instructions.

## execution_discipline

Signals include an assistant promising to check, run, test, verify, create, update, inspect, read, search, fix, or write, with no observed tool message before the next assistant or user turn. Also catches user complaints about planning instead of acting (e.g. "stop planning, just do it", "don't just plan").

Capability statements ("I can …") and offers ("let me know if …") are *not* treated as promises.

Patch targets:

- `sop` — require the promised action before reporting progress or results.
- `eval` — fail when the agent promises execution but only narrates.

## verification_failure

Signals include phrases such as "did you test", "did you verify it", "without verifying", "not verified", "not actually tested".

Patch targets:

- `sop` — require explicit verification status.
- `eval` — fail confident claims that lack a verification step or a clear disclosure that verification was not run.

## memory_failure

Signals include "you forgot", imperative "remember" (in directed or sentence-initial position), "I told you", and "last time".

Informational uses such as "Just so I remember the timeline …" are explicitly excluded — the regex requires an imperative position (`you (must|should|need to)?remember`, or `remember` after a sentence boundary).

Patch targets:

- `memory` — create a reviewable memory candidate from evidence, without inferring beyond the quoted transcript.

## tool_failure_or_hidden_error

Signals include tool output containing `error`, `failed`, `failure`, `timeout`, `unauthorized`, `401`, `403`, `500`, `traceback`, or `exception`, followed by an assistant response that does not acknowledge the failure.

False-positive guards:

- Negative phrases like `0 errors`, `no errors`, `no failures` are stripped before matching.
- Identifier-like trailing chars are excluded — `error_handler.py`, `error.log`, `error_count` do not match the bare `error` token.

Patch targets:

- `sop` — acknowledge tool failures, summarize the observed error, and change course before claiming success.
- `tool_discipline` — block success summaries when recent tool results contain failure signals.

## communication_mismatch

Signals include phrases such as "too verbose", "stop explaining", and similar complaints about volume or pacing of agent output.

Imperative-action phrases like "just do it" / "don't just plan" / "do not just plan" belong to `execution_discipline` and are *not* treated as communication complaints — they are about action discipline, not style.

Single instances can be situational, so recommendations include an overfit warning and only escalate to a memory candidate at higher counts.

Patch targets:

- `memory` — store a concise communication preference only after repeated evidence (overfit warning included for single occurrences).
- `identity` — guide the agent toward direct, action-oriented updates.

## Distractors that must not be flagged

These are deliberate non-matches; the regression bench includes a distractor-only scenario to ensure they stay non-matches.

- "Just so I remember the timeline …" — informational `remember`, not a memory complaint.
- Tool output containing `0 errors` / `no failures` — explicit success signal, not a hidden error.
- Identifier-like terms such as `error_handler.py`, `error.log`, `error_count` — file/symbol names, not error messages.
- "I can run … if you want" — capability statement / offer, not a promise.
- "No problem" without an actual error context — filler, not an error acknowledgement.
