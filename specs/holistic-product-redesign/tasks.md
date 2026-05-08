# Implementation Plan — Holistic Product Redesign

Build order is determined by dependency, not calendar. Each phase is
independently shippable.

## Phase 0 — Foundation fix

- [ ] Commit the working-tree delivery fix as `fix: resolve openclaw under
      launchd minimal PATH` (delivery.py `resolve_openclaw_binary`, service.py
      `Environment=PATH=...` in plist + systemd, autopilot.py
      don't-record-on-delivery-failure).
- [ ] Replace `str(CalledProcessError)` capture in
      `autopilot.run_notify_command` with structured `{rc, stderr, stdout}`
      and write the full record to `delivery-errors.jsonl`.
- [ ] Add a regression test that runs `notify_openclaw_system_event` from a
      tempdir CWD with `env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}` and
      asserts delivery succeeds.
  - _Requirement: 6.4_

## Phase 1 — Adapter substrate

- [ ] Define `agent_doctor/adapters/base.py` with `HostAdapter` Protocol,
      `HostCapabilities`, `Target`, `MessageBody`, `MessageKind`,
      `Reaction`, `SessionMetadata`.
- [ ] Implement `agent_doctor/adapters/openclaw.py` with the full surface
      (send_message, edit_message, add_reaction, list_reactions,
      inject_system_event, infer_text, infer_embedding, session_metadata).
- [ ] Implement `agent_doctor/adapters/generic.py` (file-inbox + OS
      notification fallback, no inference).
- [ ] Implement `agent_doctor/adapters/hermes.py` as a stub that detects
      `~/.hermes` and declares partial capabilities (JSONL ingest works,
      delivery declared unsupported until Phase 5).
- [ ] Add `agent_doctor/capabilities.py` with `detect_hosts()` and a
      24h-TTL host cache in `state.sqlite3:host_cache`.
- [ ] Add `agent_doctor.adapters.testing.AdapterContractTest` fixture.
- [ ] Refactor `delivery.py`, `setup.py`, `install.py`, `bootstrap.py`,
      `service.py` to route through adapters. Per-host branches deleted
      from these files.
- [ ] Add CLI: `agent-doctor adapters list` and `agent-doctor adapters test
      <host>`.
- [ ] Expand `agent-doctor doctor` to print the full capability matrix.
- [ ] Write `docs/adapters/openclaw.md`, `docs/adapters/generic.md`,
      `docs/adapters/hermes.md` (latter notes the v1 gap).
  - _Requirement: 4.1, 4.2, 4.5, 4.6_

## Phase 2 — Detection intelligence

- [ ] Add `agent_doctor/classifier/tier1.py` wrapping existing
      `frustration.py` + `detectors.py` as the regex layer.
- [ ] Add `agent_doctor/classifier/signal_fusion.py` with typing-shape,
      trajectory-across-N-turns, and repeat-theme signals.
- [ ] Add `agent_doctor/classifier/user_dict.py` persisting positive /
      negative phrases per host at
      `~/.agent-doctor/<host>/user-dict.json`.
- [ ] Add `agent_doctor/classifier/tier2.py` calling
      `adapter.infer_text(prompt, model=…)` on borderline scores. Strict
      JSON schema; one retry on parse failure; fall through to Tier 1 on
      unrecoverable failure.
- [ ] Cache Tier 2 results by `(message_hash, model)` in
      `state.sqlite3:classifier_cache`.
- [ ] Per-day call cap (default 100, configurable via
      `--classifier-max-calls-per-day`) bounded at the adapter call site.
- [ ] Doctor command shows: tier configuration, cache size, daily-call
      counter, last calibration suggestions file.
  - _Requirement: 3.1, 3.2, 3.3, 3.4, 7.1_

## Phase 3 — Speak path

- [ ] Add `agent_doctor/speaker.py` with `render_intervene`,
      `render_propose`, `render_digest`, `render_applied`, `render_undone`.
      Detect session language and localize wrapper text. Tier 2 prompt asks
      for response in the user's language.
- [ ] Add `agent_doctor/channel_router.py` with `resolve(session_id) ->
      Target`. Reads JSONL header + host status manifest. TUI sessions
      resolve to `Target(kind="tui", inbox_path=…)`.
- [ ] Add `messages.jsonl` writer (atomic append, 0o600).
- [ ] Replace `run_notify_command` in `autopilot.py` with
      `dispatch_event(event, adapter, channel_router, speaker)`. Subprocess
      stderr/stdout captured into `delivery-errors.jsonl`.
- [ ] Keep `--notify-command` flag with deprecation warning for backward
      compatibility.
- [ ] Test: a real intervene event posts a 🩺 message in the active
      channel; falls back to OS notification + inbox when channel
      unavailable.
  - _Requirement: 1.1, 1.2, 1.3, 1.4, 6.4_

## Phase 4 — Closed-loop apply

- [ ] Add `agent_doctor/proposer.py` with session-end heuristic (no new
      messages for N min, default 5) and N-detection threshold (default 3
      per `(session, failure_mode)`, max 3 proposals per session).
- [ ] Add `agent_doctor/reaction_watcher.py` with 30s/24h fast poll and
      300s/7d slow poll cadences. First non-neutral reaction within 5 min
      wins. Idempotent.
- [ ] Add `agent_doctor/applier.py` with: target resolution from
      capabilities, auto-create missing target with 0o600 perms,
      pre-write backup to `~/.agent-doctor/backups/<patch-id>/`, atomic
      write, post-write `edit_message` to mark applied, log to
      `patch-log.jsonl`.
- [ ] Append-only patches (memory, tool_discipline) skip baseline hash
      check. Edit patches (identity, sop) verify baseline hash; on mismatch
      transition to `state=conflict` and edit the 🩺 message with redraft
      option.
- [ ] CLI: `agent-doctor approve <id>`, `dismiss <id>`, `redraft <id>`,
      `undo <patch-id>`, `undo --last`, `undo --since <duration>`,
      `undo --all-pending`, `patches list`.
- [ ] Generate per-host launchd/systemd service for `watch-reactions`
      alongside the existing autopilot service.
- [ ] Backups retained 30 days (configurable). `restore.sh` per backup.
- [ ] Doctor command shows: pending proposals, recent applies, backup
      count, last undo.
  - _Requirement: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 6.1, 6.2, 6.5_

## Phase 5 — Hermes adapter

- [ ] Research Hermes outbound surface: message-send equivalent, reactions
      API, system-event equivalent, inference CLI.
- [ ] Implement `agent_doctor/adapters/hermes.py` to the extent Hermes
      supports. Declare capabilities truthfully.
- [ ] Run `AdapterContractTest` against Hermes. Document gaps in
      `docs/adapters/hermes.md`.
- [ ] Independent of Phases 3-4; lands when ready.
  - _Requirement: 4.1, 4.2, 4.6_

## Phase 6 — Measurement and digest

- [ ] Add `agent_doctor/measurer.py` that, per applied patch, replays
      trigger turns through `adapter.infer_text` with both patched and
      baseline configs and judges results with the host's stronger model.
      Writes `measurements.jsonl`.
- [ ] Add `agent_doctor/digester.py` (cron-driven, default Sunday 09:00
      host-local). Aggregates the past week and posts via
      `speaker.render_digest`.
- [ ] Add `agent_doctor/classifier/tier3.py` for weekly opt-in calibration.
      Produces `calibration-suggestions.md`. Never auto-applies.
- [ ] CLI: `agent-doctor calibrate enable | review | disable`,
      `agent-doctor digest [--now]`.
- [ ] Migrate `evals/replay.py`, `evals/generator.py` to use
      `adapter.infer_text` so eval pipeline shares the host's
      privacy/cost choices. Preserve the direct Anthropic SDK fallback for
      adapter-less use cases.
  - _Requirement: 3.5, 5.1, 5.2, 5.3, 7.1, 7.2_

## Cross-cutting

- [ ] All new artifacts written 0o600.
- [ ] All transcript-derived strings pass through
      `agent_doctor.redaction` before storage or message rendering.
- [ ] Update README to replace "no LLM in production path" with the
      truthful three-tier privacy claim from Requirement 7.2.
- [ ] Update `docs/architecture.md` to reflect the new layers.
- [ ] All existing tests continue to pass; new tests are added per phase
      and gated in CI.
  - _Requirement: 7.1, 7.2, 7.3, 7.4_
