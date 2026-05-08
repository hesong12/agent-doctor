# Requirements — Holistic Product Redesign

## Introduction

Agent Doctor today detects user frustration and writes diagnosis cards to disk
that the user never sees. Even when delivery to the host succeeds, intervention
flows through `openclaw system event`, which is consumed by the agent and
silently absorbed in the agent's response — there is no user-visible
confirmation that Agent Doctor noticed or acted. The user complaint is exact:
"完全零观感" (zero perception).

This redesign turns Agent Doctor from a passive observability tool into an
**agent immune system** that speaks in the same channel the user is already
using, drafts patches against repeating failure patterns, applies them on a
single emoji reaction, and proves the fixes worked with eval-measured weekly
digests. It must work for the public GitHub audience — any OpenClaw or Hermes
user — without modifying OpenClaw or Hermes source.

## Requirements

### Requirement 1 — Channel-Native Visibility

**User Story:** As a user of OpenClaw or Hermes via any channel (Telegram,
Discord, QQ bot, iMessage, local TUI, etc.), when I express frustration, I want
to see Agent Doctor react in the same conversation, so I know the system is
alive and what it just did.

**Acceptance Criteria:**

1. When a high-severity user_frustration_signal fires for a session, Agent
   Doctor SHALL post a `🩺 Agent Doctor` message in the same channel and target
   as the triggering session, with the detected pattern, evidence excerpt, and
   what action was taken — independent of whether the host agent visibly
   complies with any system event.
2. When the host channel does not support outbound messages (capability
   `can_send_message=false`), Agent Doctor SHALL fall back to OS-native
   notification (macOS, Linux) plus an inbox advisory file, and the
   `agent-doctor doctor` command SHALL surface the degraded mode to the user.
3. When Agent Doctor posts a message, it SHALL record `(target, message_id,
   kind, event_id)` in `messages.jsonl` so reactions can be tracked and
   subsequent edits can mark state.
4. The 🩺 prefix and posting identity (shared agent account vs. dedicated bot)
   SHALL be configurable per host but default to the shared agent account with
   a `🩺 [Agent Doctor]:` prefix so zero new account setup is required.

### Requirement 2 — Closed-Loop Patch Application

**User Story:** As a user, after Agent Doctor detects a repeated failure
pattern in my session, I want to approve a draft fix with a single emoji
reaction and have it applied to my live agent config, so the agent gets better
without me running CLI commands.

**Acceptance Criteria:**

1. When a session ends or N matching detections accumulate (default N=3 per
   `(session, failure_mode)`, capped at 3 proposals per session total), Agent
   Doctor SHALL post a 🩺 propose message containing the draft patch body,
   target file path, and a CLI fallback command for hosts without reaction
   support.
2. When a host supports `react` and `list_reactions`, Agent Doctor SHALL seed
   the propose message with ✅ / ❌ / 💬 reactions as UI hints and poll for
   user reactions every 30s for the first 24h, then every 5min for 7 days,
   then archive the proposal as expired.
3. When the user reacts ✅, Agent Doctor SHALL backup the target file to
   `~/.agent-doctor/backups/<patch-id>/` and apply the patch atomically. On
   success, the original 🩺 message SHALL be edited to show "✅ Applied — undo:
   `agent-doctor undo <patch-id>`".
4. When the user reacts ❌, Agent Doctor SHALL mark the proposal dismissed and
   add the trigger phrase to the per-user negative-example dictionary.
5. When the user reacts 💬, Agent Doctor SHALL mark the proposal as
   refining and watch for a follow-up user message in the same conversation,
   then redraft and repost.
6. `agent-doctor undo <patch-id>`, `agent-doctor undo --last`, and
   `agent-doctor undo --since <duration>` SHALL restore files from backups
   atomically and post a 🩺 confirmation message in the original channel.

### Requirement 3 — Intelligent Detection (Multi-Tier)

**User Story:** As a user whose frustration shows up in language Agent Doctor
wasn't pre-programmed for (sarcasm, indirect complaints, my own dialect), I
want detection to learn from my reactions and from the LLM that's already
configured for my host, so accuracy improves over time without me hand-editing
regexes.

**Acceptance Criteria:**

1. Detection SHALL fuse the existing regex tier with multi-signal scoring
   (typing-shape, trajectory across last N turns, repeat themes) and a
   per-user dictionary that learns from ✅ / ❌ reactions on Agent Doctor
   messages.
2. When Tier 1 produces a borderline confidence (configurable, default
   score 1–2), Agent Doctor SHALL invoke the host's existing inference CLI
   (`openclaw infer model run` or Hermes equivalent) using the host's default
   or configured classifier model and merge the result into the final signal.
3. Tier 2 SHALL never be required — when no host inference is available or the
   user has opted out, detection SHALL gracefully degrade to Tier 1 + signal
   fusion, and `agent-doctor doctor` SHALL announce this degradation.
4. Tier 2 SHALL respect a per-day call cap (default 100) and cache results by
   `(message_hash, model)` in SQLite so re-scans don't re-pay.
5. Tier 3 calibration SHALL be opt-in (`agent-doctor calibrate enable`) and
   produce *suggestions* — never auto-tune thresholds. The user SHALL review
   suggestions via `agent-doctor calibrate review` before any threshold change
   takes effect.

### Requirement 4 — Public-Repo Portability

**User Story:** As an OpenClaw or Hermes user discovering Agent Doctor on
GitHub, I want it to detect my host, surface what's available on my machine,
and work with sensible defaults — without hardcoded model names, channels,
phone numbers, or API keys specific to the maintainer's environment.

**Acceptance Criteria:**

1. Agent Doctor SHALL detect installed hosts (OpenClaw, Hermes, generic) by
   probing well-known paths and binaries at startup, with no user
   configuration required.
2. Each host SHALL be served by a `HostAdapter` implementing a published
   Protocol (`send_message`, `list_reactions`, `infer_text`, `infer_embedding`,
   `session_metadata`, `inject_system_event`) that declares its
   `HostCapabilities` so downstream code can select the best path or degrade
   gracefully.
3. Channel and recipient SHALL be auto-resolved from session JSONL metadata at
   delivery time. The system SHALL never require the user to configure phone
   numbers, chat IDs, or channel handles.
4. The default classifier model SHALL be the host's configured default. The
   user MAY override via `agent-doctor setup autopilot --classifier-model`,
   but no override SHALL be required for first run.
5. `agent-doctor doctor` SHALL print all detected hosts, their full capability
   matrix, configured channels, available inference models, writable host
   surfaces (memory, identity, SOP files), and current health (service
   running, last detection, last delivery, pending proposals, backup count).
6. The repo SHALL include `docs/adapters/` per-host documentation and an
   `agent_doctor.adapters.testing.AdapterContractTest` fixture so community
   contributors can validate new host adapters before submitting PRs.

### Requirement 5 — Eval-Measured Improvement

**User Story:** As a user who's spent reactions ✅-ing patches, I want to see
weekly evidence that the patches actually reduced the failure modes they
targeted, so I can trust that the loop is real.

**Acceptance Criteria:**

1. When `calibrate` is enabled, the measurer SHALL replay the trigger turn(s)
   of every applied patch through the host's inference using both the patched
   and baseline configurations, judge the responses with a stronger host
   model, and store deltas in `measurements.jsonl`.
2. Once per week (configurable cron), Agent Doctor SHALL post a 🩺 weekly
   digest in the host's main channel with: detection count, proposal count,
   apply count, measured improvement count, top patterns fixed, and
   per-pattern before/after numbers.
3. Calibration and measurement SHALL be free of remote LLM calls *outside*
   the host inference adapter — the host's privacy/cost choices apply
   automatically.

### Requirement 6 — Reliability and Reversibility

**User Story:** As a user trusting Agent Doctor with edits to my live agent
config, I need every change to be reversible, every failure to be observable,
and the service itself to never silently die.

**Acceptance Criteria:**

1. Every applied patch SHALL produce a backup at
   `~/.agent-doctor/backups/<patch-id>/<filename>.bak` plus a `restore.sh`
   script. Backups SHALL be retained for 30 days (configurable).
2. `agent-doctor patches list` SHALL print every active applied patch with
   timestamp, target file, originating session, and undo command.
3. The autopilot service SHALL surface its own liveness via `agent-doctor
   doctor`. When the service has not produced a heartbeat log line in 30 min
   while the host home is reachable, `doctor` SHALL flag it and the OS-native
   notification path SHALL fire once per restart-loop window.
4. Every notify subprocess failure SHALL capture and persist the subprocess
   stderr and stdout in `delivery-errors.jsonl` (the current behavior of
   discarding the actual error message via `str(CalledProcessError)` is a
   regression to be removed).
5. Edit-style patches SHALL hash the target file at draft time and verify the
   hash before applying. On hash mismatch, the proposal SHALL transition to
   `state=conflict` and the 🩺 message SHALL be edited with the redraft
   option.

### Requirement 7 — Local-First Privacy

**User Story:** As a privacy-conscious user, I want Agent Doctor's added
intelligence to inherit my host's privacy boundary — not introduce its own
remote API key, not phone home, not leak transcripts.

**Acceptance Criteria:**

1. Agent Doctor SHALL NOT make remote network calls of its own. All inference
   SHALL be routed through the host adapter (which uses the host's existing
   provider configuration).
2. The README and `agent-doctor doctor` SHALL state, accurately: "Tier 1
   detection is regex-only and never calls a model. Tier 2 (optional) reuses
   your host's configured inference. Tier 3 calibration is opt-in and uses the
   same host adapter."
3. All artifacts (events.jsonl, messages.jsonl, proposals.jsonl, patch-log,
   backups) SHALL be written with `0o600` permissions.
4. Transcript-derived strings SHALL pass through `agent_doctor.redaction`
   before any storage or message rendering.
