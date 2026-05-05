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

False-positive guards (each driven by a real-world false positive observed on production transcripts):

- **Negative prose** — `0 errors`, `no errors`, `no failures`, `zero exceptions` stripped before matching.
- **JSON success envelopes** — `"error": null`, `"error": ""`, `"error": "none"`, `"stderr": ""`, `"exception": null`, `"traceback": null` stripped. Hermes / OpenClaw / generic CLI shells use these as the explicit "no error" indicator on success; without this strip every successful command flagged as a hidden error.
- **Zero-exit / success markers** — `"exit_code": 0`, `"status_code": 0`, `"returncode": 0`, `"status": 0`, `"success": true`, `"ok": true` (both JSON-quoted and bare-prose forms) stripped.
- **Explicit non-error annotation** — the literal `(not an error)` phrase (some tools emit this when grep returns exit code 1 for "no matches", etc.) is stripped.
- **Identifier-like trailing chars** — `error_handler.py`, `error.log`, `error_count` don't match the bare `error` token, via `(?![._-]\w)` negative lookahead.
- **HTTP codes vs source line refs** — `401` / `403` / `500` only match when not surrounded by colons or other digits, so `cli.js:403:` (`file:line:col` source ref) doesn't get treated as HTTP 403, while `"401 Unauthorized: token rejected"` still does.

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

## user_frustration_signal

Signals include direct user anger, insults/profanity, trust-break language, repeated correction language, direct quality complaints, direct dumb/stupid feedback, and **trust-degradation phrases** in English and Chinese. Examples include "what the fuck are you doing", "this is bullshit", "Why are you so dumb?", "Are you stupid?", "not useful", "cannot trust you", "废物", "垃圾", "不够聪明", "你怎么这么笨的？", "好笨", "每次都这样", `you are getting worse and worse`, `lately you seem dumber`, `越来越笨`, `越来越蠢`, and `你最近怎么越来越笨了`.

This detector is implemented as a local weighted classifier, not a remote LLM:

- insults/profanity, trust-break language, and trust-degradation phrases each carry high weight.
- direct quality complaints carry high weight.
- repeated corrections and urgency shape are weak supporting signals and do not create a scan finding by themselves.
- Trust-degradation phrases are also routed to autopilot as `intervene` events even at "medium" baseline severity, because they describe cumulative quality loss rather than a one-off complaint.

Patch targets:

- `identity` — switch to a short evidence-backed recovery response.
- `sop` — pause the normal success path, name the concrete failure, cite diagnosis evidence, and give the next corrective action.
- `eval` — add a regression case for user-frustration recovery without defensiveness or long apology.

## trust_degradation_episode

Episode-level meta-finding: emitted when two or more trust-eroding signals (frustration, repeated correction, missed core question, instruction drift, memory failure, verification failure, communication mismatch, over-process response) cluster within a small user-turn window in the same session.

This is a *cumulative* failure mode — three short corrections inside one session can be far more important than any single one of them. The episode finding is high severity, high confidence, and routed to autopilot as an `intervene` event with an explicit **Required Acknowledgement** section in both the diagnosis card and the inbox advisory. The host agent is expected to surface the cumulative pattern, propose a recovery plan, and wait for user acknowledgement before resuming the normal task path.

Patch targets:

- `identity` — treat clusters of complaints as a trust-loss moment, not isolated noise.
- `sop` — pause, summarize the recent failures, propose a concrete recovery plan, require user acknowledgement before continuing.
- `eval` — add a regression case where multiple frustration signals cluster across user turns and the agent must surface the episode rather than handle each turn alone.

## missed_core_question

Signals include phrases such as "you didn't answer my question", "answer my actual question", "that's not what I asked", "你没回答我的问题", "我问的是…", "答非所问". The agent should re-anchor on the original question before continuing.

Patch targets:

- `sop` — restate the user's core question and confirm the answer addresses it.
- `eval` — add a regression case where the first response misses the core question.

## instruction_drift

Signals include phrases such as "I didn't ask you to…", "nobody asked you to…", "stop adding extra…", "don't go beyond…", "我没让你…", "我只让你…". Indicates the agent silently expanded scope beyond the user's explicit request.

Patch targets:

- `sop` — stay strictly inside the user's stated scope; ask before expanding.
- `eval` — add a regression case where the user gives a narrow request and the agent must not silently add extra work.

## over_process_response

Signals include direct user complaints about meta-narration ("stop narrating what you're doing", "just give me the answer", "少废话") **and** assistant messages that are long (≥ 400 chars) and dominated by step-by-step planning tokens ("first, then, next, after that, I'll, I'm going to, finally"). The latter is intentionally conservative; short plans are normal pacing, not a failure mode.

Patch targets:

- `identity` — lead with the result, not the plan.
- `sop` — limit assistant responses to answer plus minimal evidence unless the user explicitly asks for play-by-play.

## unsupported_completion_claim

Signals: an assistant message contains a completion claim ("done", "fixed", "verified", "passed", "shipped", "完成了", "搞定了", "修好了", "已验证", "部署完成") and there is **no** tool result or verification keyword in the prior six turns of the same session. If a user immediately challenges the claim ("are you sure?", "it's not done", "你确定做完了"), severity escalates to high and the doubting turn is attached as additional evidence.

The detector excludes the assistant's own message from the verification window so a phrase like "Done, fixed and verified." cannot satisfy its own check.

Patch targets:

- `sop` — never claim completion without either a recent tool action or an explicit non-verification disclosure.
- `eval` — add a regression case that fails when the agent claims completion in a span without a tool result.

## Distractors that must not be flagged

These are deliberate non-matches; the regression bench and unit tests include scenarios for each to ensure they stay non-matches:

- **Informational `remember`** — "Just so I remember the timeline …" does not trip `memory_failure`.
- **Negative-error prose** — `0 errors`, `no failures`, `zero exceptions` do not trip `tool_failure_or_hidden_error`.
- **JSON success envelopes** — `{"output": "...", "exit_code": 0, "error": null}`, `{"success": true, ...}`, `{"stderr": ""}` do not trip `tool_failure_or_hidden_error`. Verified against real Hermes sessions where `git remote -v` and similar successful commands emit this shape.
- **Identifier-like names** — `error_handler.py`, `error.log`, `error_count` do not match the bare `error` token.
- **Source-line references** — `cli.js:403:` does not match HTTP 403; `path/to/file.py:500:` does not match HTTP 500. Verified against real grep output.
- **Capability offers** — "I can run … if you want", "let me know if …" are not promises and don't trip `execution_discipline`.
- **Filler acknowledgements** — "No problem" without a preceding error context is not treated as an error acknowledgement.
- **Urgency shape alone** — "WHAT???" does not trip `user_frustration_signal` without a stronger complaint, insult, repeated correction, or trust-break phrase.
- **Technical Chinese terms** — "笨重" describes something cumbersome/heavy and does not trip `user_frustration_signal`.
