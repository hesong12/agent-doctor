# Design — Holistic Product Redesign

## Vision

Agent Doctor becomes a **local-first agent immune system** that lives next to
memoryful agent frameworks. It speaks in the same channel the user is already
using, drafts fixes for repeating failure patterns, applies them with a single
emoji reaction, and proves the fixes worked with eval-measured weekly digests.

Three concentric loops, each shorter than the last:

- **Seconds** — detection → in-channel acknowledgement (`🩺 Agent Doctor noticed`)
- **Minutes** — session-end → draft patch → ✅ → applied
- **Days** — eval-measured improvement → weekly digest → trust compounds

The product never modifies OpenClaw or Hermes source. It runs as an outside-in
sidecar that reads JSONL, posts to channels via the host's existing CLI, and
reuses the host's existing inference for any model-backed work.

## Architecture

```
                           Existing pipeline
                           ─────────────────
   JSONL → ingest → detect → aggregate → recommend → events.jsonl/findings.json

                           New layers below
                           ────────────────
                       ┌───────────────────┐
                       │  channel router   │  session_id → (host, channel, target)
                       └─────────┬─────────┘
            ┌────────────────────┼────────────────────┐
            ↓                    ↓                    ↓
       ┌────────┐          ┌──────────┐         ┌──────────┐
       │speaker │          │ proposer │         │ digester │
       └───┬────┘          └────┬─────┘         └────┬─────┘
           │                    │                     │
           └────────────────────┴─────────────────────┘
                                ↓
                     ┌─────────────────────┐
                     │  HostAdapter        │  pluggable per host
                     │  (Protocol)         │
                     └──────────┬──────────┘
                                ↓
                     ┌──────────────────────┐
                     │  reaction watcher    │
                     └──────────┬───────────┘
                                ↓ ✅
                     ┌──────────────────────┐
                     │      applier         │  backup → write → audit
                     └──────────┬───────────┘
                                ↓
                     ┌──────────────────────┐
                     │     measurer         │  eval-replay before/after
                     └──────────────────────┘
```

## Components

### `agent_doctor/adapters/`

The substrate. Everything else routes through here.

```python
class HostAdapter(Protocol):
    @classmethod
    def detect(cls) -> "HostAdapter | None": ...
    def capabilities(self) -> HostCapabilities: ...
    def send_message(self, target: Target, body: MessageBody, kind: MessageKind) -> str: ...
    def edit_message(self, target: Target, message_id: str, body: MessageBody) -> None: ...
    def add_reaction(self, target: Target, message_id: str, emoji: str) -> None: ...
    def list_reactions(self, target: Target, message_id: str) -> list[Reaction]: ...
    def inject_system_event(self, text: str, *, mode: str = "now") -> None: ...
    def infer_text(self, prompt: str, *, model: str | None = None) -> str: ...
    def infer_embedding(self, text: str, *, model: str | None = None) -> list[float]: ...
    def session_metadata(self, jsonl_path: Path) -> SessionMetadata: ...
```

Not every method is required. `capabilities()` declares the truth; downstream
code branches on the capability flags, not on host identity.

```python
@dataclass(frozen=True)
class HostCapabilities:
    host_name: str
    detected_at: Path

    can_send_message: bool
    can_edit_message: bool
    can_react: bool
    can_list_reactions: bool
    can_inject_system_event: bool
    can_infer_text: bool
    can_infer_embedding: bool

    default_inference_model: str | None
    available_models: list[str]
    available_channels: list[str]

    skill_dir: Path | None
    memory_writable: Path | None
    identity_writable: Path | None
    sop_writable: Path | None
```

Adapters shipped in v1:

| Adapter | File | Strategy |
|---|---|---|
| `OpenClawAdapter` | `adapters/openclaw.py` | wraps `openclaw message send/react/reactions list/edit`, `openclaw infer model run/embedding create`, `openclaw system event` |
| `HermesAdapter` | `adapters/hermes.py` | partial — JSONL ingest + skill install + system-event-equivalent if present; declares `can_send_message=False` until research surfaces a Hermes outbound API or community PR adds one |
| `GenericAdapter` | `adapters/generic.py` | inbox-file delivery + OS notification + no inference; the always-available fallback |

Per-host docs live in `docs/adapters/`. New adapters land via PR with the
`AdapterContractTest` fixture passing.

### `agent_doctor/capabilities.py`

`detect_hosts() -> list[HostAdapter]` walks `~/.openclaw`, `~/.hermes`, then
falls back to `GenericAdapter`. Result is cached in
`state.sqlite3:host_cache` with a 24h TTL plus a manual
`agent-doctor doctor --refresh` invalidator.

### `agent_doctor/channel_router.py`

`resolve(session_id) -> Target`. Reads the session JSONL header and host
session manifest (`openclaw status --json` for OpenClaw) to determine
`(channel, recipient, agent_account)`. For TUI sessions, returns
`Target(kind="tui", inbox_path=…)` so downstream falls through to OS
notification + inbox file. Channel and recipient are NEVER user-configured —
they come from the session metadata at delivery time.

### `agent_doctor/classifier/`

```
classifier/
  tier1.py           regex (existing) + signal fusion + per-user dictionary
  signal_fusion.py   typing-shape, trajectory, repeat themes
  user_dict.py       per-user do-flag / do-not-flag, persisted at
                     ~/.agent-doctor/<host>/user-dict.json
  tier2.py           host-inference second pass, only on borderline scores
  tier3.py           weekly opt-in calibration via host inference
```

`Tier1Fused.classify(messages, context) -> Signal` is always called. The
existing weighted regex scoring in `frustration.py` produces an integer score;
fused with the new signal-fusion and user-dict layers, the final classifier
returns one of three confidence bands:

- `clear-none` — fused score 0 (no Tier 2 call)
- `borderline` — fused score 1–2 (Tier 2 invoked)
- `clear-frustration` — fused score ≥ 3 (no Tier 2 call)

Borderline triggers `Tier2.classify`, which makes one host-inference call
with a few-shot prompt and returns a merged signal. The Tier 2 call result is
cached by `(message_hash, model)` in `state.sqlite3:classifier_cache`. A
daily call cap (default 100, override via
`--classifier-max-calls-per-day`) bounds cost; once hit, Tier 2 transparently
no-ops and Tier 1 wins.

The user dictionary is fed by reactions: ❌ on a 🩺 detection adds the trigger
phrase to the negative list; ✅ on a propose message that referenced a
previously-undetected pattern adds it to the positive list.

Tier 3 calibration runs on cron (opt-in via `agent-doctor calibrate enable`).
It batches the past week's transcripts, asks the host's stronger model for
labels, and produces a `~/.agent-doctor/<host>/calibration-suggestions.md`
file. The user reviews via `agent-doctor calibrate review` and accepts /
rejects threshold tuning. Calibration NEVER auto-applies.

### `agent_doctor/speaker.py`

Templates for the three message kinds. Localizes wrapper text from the
session's dominant language (detected from majority of user messages). Tier 2
prompt explicitly asks the model to respond in the user's language so the
diagnosis line matches.

```python
def render_intervene(event: AutopilotEvent, signal: Signal, language: str) -> MessageBody: ...
def render_propose(proposal: Proposal, language: str) -> MessageBody: ...
def render_digest(digest: WeeklyDigest, language: str) -> MessageBody: ...
def render_applied(proposal: Proposal, patch_id: str, language: str) -> MessageBody: ...
def render_undone(patch_id: str, language: str) -> MessageBody: ...
```

Every message body is structured: a header line (`🩺 Agent Doctor — <kind>`),
a one-paragraph diagnosis, an evidence excerpt (redacted), an action footer.
The propose template includes a CLI fallback (`agent-doctor approve <id>` /
`dismiss <id>`) so users on hosts without reaction support can still close
the loop.

### `agent_doctor/proposer.py`

Triggers on either:
- session-end heuristic (no new messages in this session for N minutes,
  default 5), or
- N matching detections of the same `failure_mode` (default 3).

Caps: max 1 proposal per `(session, failure_mode)`, max 3 proposals per
session total. The proposer uses the existing `recommend.py` to draft patches.
Each draft is one of: `memory`, `identity`, `sop`, `tool_discipline`, `eval`.
Memory and tool_discipline are append-only (no conflict). Identity and SOP
patches store a baseline-hash of the target file at draft time.

Proposal records land in `proposals.jsonl` with TTL=24h. After 24h the propose
message is edited to "⏱️ expired — redraft with `agent-doctor redraft <id>`"
and the proposal transitions to `state=expired`.

### `agent_doctor/reaction_watcher.py`

A long-running task (`agent-doctor watch-reactions`) installed as a launchd
sub-service or run inline by the autopilot. For each pending proposal:

- Poll `adapter.list_reactions(target, message_id)` every 30s for the first
  24h, then every 5min for 7 days.
- The first non-neutral reaction (✅ / ❌ / 💬) within a 5-min window wins —
  later reactions are logged but do not reverse the decision.
- ✅ → applier
- ❌ → mark dismissed, feed user_dict negative
- 💬 → mark refining, watch for follow-up message in same conversation

Idempotent: a second ✅ on an already-applied proposal is a no-op with an
"already applied" 🩺 reply.

### `agent_doctor/applier.py`

```python
def apply_proposal(proposal: Proposal, adapter: HostAdapter) -> AppliedPatch:
    target = resolve_target(proposal.kind, adapter.capabilities())
    if target is None:
        return AppliedPatch(state="degraded_to_staging", staging_path=...)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(default_header_for(proposal.kind), encoding="utf-8")
        target.chmod(0o600)
    if proposal.requires_baseline_hash():
        if hash_of(target) != proposal.baseline_hash:
            mark_conflict(proposal)
            adapter.edit_message(...)
            return AppliedPatch(state="conflict")
    backup = backup_target(target, proposal.id)
    new_text = apply_patch_body(target.read_text(), proposal.body, proposal.kind)
    atomic_write(target, new_text)
    log_applied(proposal, backup)
    adapter.edit_message(target=proposal.target,
                        message_id=proposal.message_id,
                        body=speaker.render_applied(...))
    return AppliedPatch(state="applied", patch_id=proposal.id, backup=backup)
```

Backup directory: `~/.agent-doctor/backups/<patch-id>/<original-filename>.bak`
plus a `restore.sh` containing the literal copy command. 30-day retention,
configurable via `~/.agent-doctor/config.toml`.

### `agent_doctor/measurer.py`

Per applied patch:

1. Locate the trigger session(s) from `events.jsonl`.
2. Identify the user turns where the failure mode fired.
3. For each turn, replay through the host's inference twice — once with the
   patched system prompt / memory, once with the baseline. Both replays use
   `adapter.infer_text(model=<host-default>)`.
4. Use a judge model to score each pair. Source order: `--judge-model` CLI
   override → `[host.<name>].calibrator_model` in config → host's default
   text-inference model. Power users override to a stronger model
   (e.g. Sonnet/Opus) for higher-quality judging at higher cost.
5. Write `(patch_id, before_session, after_judgment, score_delta,
   judged_by_model)` to `measurements.jsonl`.

Opt-in only. Default off. Disabled gracefully when `can_infer_text=False`.

### `agent_doctor/digester.py`

Cron-driven (default Sunday 09:00 host-local). Aggregates the past 7 days from
`events.jsonl`, `proposals.jsonl`, `patch-log.jsonl`, `measurements.jsonl`
and composes a weekly digest. Posted via `speaker.render_digest` to the host's
default channel (or the most-active channel if no default is configured).

## State

| File | Schema (key fields) |
|---|---|
| `events.jsonl` (existing) | `id, platform, action, trigger, severity, session_id, evidence, finding_ids, card_path` |
| `messages.jsonl` (NEW) | `id, target, message_id, kind, event_id, posted_at, host_name` |
| `proposals.jsonl` (NEW) | `id, finding_ids, target_kind, target_file, body, baseline_hash, state, message_id, target, created_at, ttl_at, resolved_at` |
| `patch-log.jsonl` (NEW) | `id, target_file, applied_at, backup_path, undo_command, applied_by_user_id, restored_at` |
| `measurements.jsonl` (NEW) | `patch_id, before_session, after_judgment, score_delta, judged_by_model, measured_at` |
| `state.sqlite3` (existing, expanded) | tables: `emitted_events`, `seen_files`, `host_cache`, `classifier_cache`, `reaction_cursors` |
| `~/.agent-doctor/<host>/user-dict.json` (NEW) | `{positive: [phrase…], negative: [phrase…], thresholds: {…}}` |
| `~/.agent-doctor/<host>/calibration-suggestions.md` (NEW) | weekly Tier 3 output |

All artifacts `0o600`; redaction applied before write.

## Configuration

`~/.agent-doctor/config.toml` (auto-generated on first `setup autopilot`,
user-editable):

```toml
[host.openclaw]
inference_model = ""              # empty = host default
embedding_model = ""
classifier_borderline_only = true
classifier_max_calls_per_day = 100
calibrator_model = ""             # opt-in; empty = disabled
posting_identity = "shared"       # "shared" | "dedicated"

[host.hermes]
# parallel keys; populated when adapter is added

[delivery]
fallback_inbox_dir = "~/.agent-doctor/inbox"
macos_notifications = true
linux_notifications = true        # uses notify-send

[loop]
proposal_threshold = 3
proposal_max_per_session = 3
proposal_ttl_hours = 24
backup_retention_days = 30

[reactions]
fast_poll_seconds = 30
fast_poll_window_hours = 24
slow_poll_seconds = 300
slow_poll_window_days = 7

[digest]
enabled = true
schedule_cron = "0 9 * * 0"
target_channel = "auto"           # "auto" picks the most-active channel from the past 7 days; or pin to e.g. "telegram:@user"
```

## CLI surface (additions and changes)

New commands:
- `agent-doctor doctor` — capability + health discovery (existing, expanded)
- `agent-doctor adapters list` / `test <host>` — adapter contract validation
- `agent-doctor speak --event <id>` — manual delivery (debug)
- `agent-doctor propose --session <id>` — manual proposal trigger
- `agent-doctor approve <patch-id>` / `dismiss <patch-id>` / `redraft <patch-id>`
- `agent-doctor watch-reactions` — long-running poll service
- `agent-doctor undo <patch-id> | --last | --since <duration> | --all-pending`
- `agent-doctor patches list` — active patches with origin + undo command
- `agent-doctor calibrate enable | review | disable`
- `agent-doctor digest [--now]` — manual digest trigger

Changes to existing commands:
- `agent-doctor setup autopilot` — uses adapter probe, no host-specific
  branches. Installs reaction-watcher service alongside the autopilot service.
- `agent-doctor autopilot` — `run_autopilot_once` calls
  `adapter.send_message` instead of running an external `--notify-command`.
  The legacy `--notify-command` flag remains for backward compatibility but
  emits a deprecation warning.

## What changes in existing files

| File | Change |
|---|---|
| `delivery.py` | Becomes legacy. The `notify_openclaw_system_event` helper is preserved for the system-event path (still useful as a defense-in-depth nudge alongside in-channel speaking) but new delivery flows through `adapters/openclaw.py`. The bug fix from the working tree (`resolve_openclaw_binary`, PATH override) is preserved. |
| `autopilot.py` | `run_notify_command` is replaced by `dispatch_event` which calls `adapter.send_message`. Subprocess stderr/stdout are captured into `delivery-errors.jsonl` (no more `str(CalledProcessError)`). |
| `setup.py` | `setup_autopilot` is host-driven via the adapter registry. Host-specific branches removed. Installs the reaction-watcher service. |
| `install.py`, `bootstrap.py` | Skill-install logic moves into `HostAdapter.install_skill()`. Bootstrap iterates adapters; per-host branches deleted. |
| `apply.py` | `stage_patches` retained for the existing dry-run flow. New `apply_now` companion handles live writes via the applier. |
| `service.py` | Adds `Environment=PATH=/opt/homebrew/bin:…` to launchd plist (preserved from working tree). Generates a second service file per host for `watch-reactions`. |
| `cli.py` | Adds the new commands listed above. Existing commands routed through adapter where applicable. |
| `evals/replay.py`, `evals/generator.py` | Migrate from direct Anthropic SDK to `adapter.infer_text` so eval pipeline shares the host's privacy/cost choices. The `[llm]` extra and `ANTHROPIC_API_KEY` continue to work as a fallback when no host adapter is present. |
| `frustration.py`, `detectors.py` | Become Tier 1's regex layer. Wrapped by `classifier/tier1.py` which adds signal fusion + user dict. Existing tests remain valid. |
| `mcp.py` | Adds adapter-aware tool variants (e.g., `mcp.tool.send_in_channel`). Existing tools unchanged. |

## Behavior under degraded capability (graceful degradation)

The system always has *some* user-perceptible surface. The following matrix
shows what fires under reduced capability:

| Capability missing | Speak path | Propose path | Apply path |
|---|---|---|---|
| `can_send_message=False` | OS notification + inbox advisory | OS notification with CLI hint | CLI-only (`agent-doctor approve`) |
| `can_react=False` | (n/a, reaction is on propose) | message includes CLI hint footer | CLI-only |
| `can_infer_text=False` | works (no Tier 2) | works (Tier 1 only) | works |
| `can_infer_embedding=False` | works (no Tier 1.5) | works | works |
| `memory_writable=None` | works | proposes but degrades to staging-only | applier returns `state=degraded_to_staging`, posts manual instructions |
| `inject_system_event=False` | speak via channel only (no agent nudge) | works | works |

`agent-doctor doctor` calls out every degraded capability so the user knows
exactly what's working and what isn't.

## Privacy and trust boundary

Updated truthful claim:

> Agent Doctor never makes its own remote calls. Tier 1 detection is
> regex-only and never invokes a model. Tier 2 (optional) reuses your host's
> configured inference — your privacy/cost choices apply automatically.
> Tier 3 calibration is opt-in, runs weekly, uses the same host adapter and
> never auto-tunes anything. All artifacts are written `0o600` and pass
> through redaction.

The README's existing "no LLM in production path" line gets replaced by this
more accurate phrasing.

## Build sequence

This is a build-order list, not a calendar. Dependencies determine order;
duration depends on the executor.

**Phase 0 — Foundation fix.** Land the working-tree delivery fix
(`resolve_openclaw_binary`, plist `PATH=`, don't-record-on-failure) as a
standalone commit. Capture subprocess stderr/stdout properly in
`run_notify_command`. This unblocks reliable delivery before any new layer is
built on top.

**Phase 1 — Adapter substrate.** Build `adapters/`, `capabilities.py`, the
OpenClaw adapter (full surface), and the generic adapter (fallback). Refactor
`delivery.py`, `setup.py`, `install.py`, `bootstrap.py`, `service.py` to route
through adapters. Add `agent-doctor adapters list / test`. Existing tests pass;
new contract tests pass for OpenClaw and generic.

**Phase 2 — Detection intelligence.** Add `classifier/` (`tier1` signal
fusion + per-user dict, `tier2` host-inference second pass). Wrap existing
`frustration.py` + `detectors.py` as Tier 1's regex layer. Daily call cap and
classifier cache in SQLite. Doctor command surfaces classifier state.

**Phase 3 — Speak path.** `speaker.py`, `channel_router.py`, `messages.jsonl`.
Replace `--notify-command` plumbing with `adapter.send_message`. First
user-perceptible improvement: the 🩺 message appears in the channel.
Localization driven by session-language detection.

**Phase 4 — Closed loop.** `proposer.py`, `reaction_watcher.py`,
`applier.py`, `proposals.jsonl`, `patch-log.jsonl`, backups, `agent-doctor
undo / approve / dismiss / redraft / patches list`. Conflict-detect via
baseline hash; append-only patches skip the check. Reaction polling cadences
as specified.

**Phase 5 — Hermes adapter.** Research the Hermes outbound surface (message
send, reactions, system event, inference). Implement what exists; declare
what doesn't via capabilities. `docs/adapters/hermes.md` documents the gap.
Independent of Phase 3-4; can land in parallel.

**Phase 6 — Measurement and digest.** `measurer.py`, `digester.py`,
`measurements.jsonl`, opt-in `agent-doctor calibrate enable`,
`tier3.py` calibration suggestions, weekly cron. Migrate `evals/replay.py` and
`evals/generator.py` to use `adapter.infer_text`. The weekly digest in the
host's main channel closes the trust loop.

Each phase is independently shippable and independently valuable.

## Open research items (not blockers, but called out)

1. **Hermes outbound surface.** The Hermes adapter in Phase 5 needs the
   Hermes equivalent of `openclaw message send`, `openclaw infer model run`,
   reactions API, and system-event-equivalent. If Hermes ships these, the
   adapter is straightforward. If not, `HermesAdapter.capabilities()`
   declares the gaps and `GenericAdapter` covers the remainder.
2. **Posting identity for shared accounts.** v1 uses the agent's account with
   a `🩺 [Agent Doctor]:` prefix. v2 should explore dedicated bot accounts
   per channel for cleaner attribution; tracked as a follow-up.
3. **Cross-session pattern aggregation.** The current detection looks at one
   session at a time. For some patterns (e.g., "this user always asks
   questions in three rounds, agent always misses on round two"),
   cross-session aggregation would catch what per-session can't. Tracked as
   a follow-up enabled by the patch + measurement state being durable.
