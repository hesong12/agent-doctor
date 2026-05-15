# Harness Rules — Auto-Installed (do not edit by hand)

**This file is managed by `harness install`. Regenerated on every run. Do not edit; edit `templates/install/AGENTS.harness.md` in `song-ai-harness` instead.**

Any agent (Claude Code, Codex, sub-agents, Hermes daemons) reading the parent `AGENTS.md` MUST follow these rules, IN ADDITION TO repo-specific instructions in the rest of `AGENTS.md`.

## Hard rules (machine-enforced where possible)

1. **PR Harness mandatory** — every code PR must go through `dev-autopilot submit <repo>`. The pre-push hook in `.git/hooks/pre-push` blocks pushes without a job-contract. To bypass, set `HARNESS_BYPASS=1` in the environment AND document the bypass in the PR body.
2. **No direct push to `main`** — enforced by branch protection in production/development repos.
3. **Required check `harness-evidence`** — PR body must include one of: `ready_for_song` · `job-contract.json` · `closure.json` · `dev-autopilot submit` · `harness-bypass:` · `--no-contract`. Workflow uses hybrid runner (GitHub-hosted primary → Hermes self-hosted fallback) per `docs/operations/ci-self-hosted-fallback.md` in song-ai-harness.
4. **Review thread resolution** — every review comment must be ACCEPTed (with fix) or REJECTed (with reasoning) AND the corresponding GitHub review thread MUST be marked resolved. `pr-review-loop.sh` does this automatically; manual reviewers must do it explicitly.
5. **Local codex-review closeout** — before first push of a job-contract execution, run `codex review` per `docs/operations/codex-review-closeout.md` (in song-ai-harness). Loop until clean, then hard-stop.
6. **Project phase / merge authority** — query orchestrator SSOT (`orchestrator project show <name>`) before assuming AI may merge. `meta-self` and `production` repos require Song approval; `development` repos require all gates green.

## Where the canonical rules live

- `~/.openclaw/workspace/AGENTS.md` — global workspace rules
- `~/Projects/song-ai-harness/AGENTS.md` — harness-owner rules
- `~/Projects/song-ai-harness/docs/operations/*.md` — per-topic SOPs
- `~/Projects/song-ai-harness/docs/decisions/*.md` — ADRs

If a rule below conflicts with a rule in `~/.openclaw/workspace/AGENTS.md`, the workspace AGENTS.md wins for OpenClaw/Luna; the harness AGENTS.md wins for non-OpenClaw agents (Codex, Claude Code).

## Self-test

To verify these rules are reaching your agent: respond with the string `HARNESS_AGENTS_INCLUDE_SEEN` followed by the current repo name when first asked.

<!-- harness template version: 2026-05-15 (sync-all live test) -->
