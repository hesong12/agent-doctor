<!-- managed by harness install: CLAUDE.md = AGENTS.harness.md -->
<!-- Claude Code auto-discovers this file; we mirror AGENTS.harness.md here -->
<!-- so harness rules reach Claude Code sessions spawned via ACP / direct CLI. -->

# Harness Rules — Auto-Installed (do not edit by hand)

**This file is managed by `harness install`. Regenerated on every run. Do not edit; edit `templates/install/AGENTS.harness.md` in `song-ai-harness` instead.**

Any agent (Claude Code, Codex, sub-agents, Hermes daemons) reading the parent `AGENTS.md` MUST follow these rules, IN ADDITION TO repo-specific instructions in the rest of `AGENTS.md`.

## Hard rules (machine-enforced where possible)

1. **PR Harness mandatory** — every code PR must go through `dev-autopilot submit <repo>`. The pre-push hook in `.git/hooks/pre-push` blocks pushes without a job-contract. To bypass, set `HARNESS_BYPASS=1` in the environment AND document the bypass in the PR body. The hook also auto-allows branches whose names start with `auto-backup/`, `harness-bypass/`, `renovate/`, or `dependabot/` (use these prefixes instead of `HARNESS_BYPASS=1` for those branch types). Note the hook **fails open**: if the audit root (`$HOME/.openclaw/workspace/audit/submit`, override with `HARNESS_AUDIT_ROOT`) is absent — fresh clone, CI, or a worktree without the workspace — the push is allowed with only a stderr warning. The authoritative gate is the server-side `harness-evidence` check, not the local hook.
2. **No direct push to `main`** — enforced by branch protection in production/development repos.
3. **Required check `harness-evidence`** — PR body must include one of: `ready_for_song` · `job-contract.json` · `closure.json` · `dev-autopilot submit` · `harness-bypass:` · `--no-contract`. Workflow uses hybrid runner (GitHub-hosted primary → Hermes self-hosted fallback) per `docs/operations/ci-self-hosted-fallback.md` in song-ai-harness.
   **v0.3.1 ui-conditional check (2026-05-20):** when the diff touches frontend files (`tsx/jsx/vue/svelte/astro`, `routes|pages|app/**`, route manifest), the PR body must ALSO include one of `UI_PROBE: <story-id>`, `UI_JOURNEYS:`, `dev-autopilot verify --url …`, `coverage.by_layer.ui`, or an explicit `ui-bypass: <reason>` / `harness-bypass:` / `--no-contract`. See ADR-0009.
4. **Review thread resolution** — every review comment must be ACCEPTed (with fix) or REJECTed (with reasoning) AND the corresponding GitHub review thread MUST be marked resolved. `pr-review-loop.sh` does this automatically; manual reviewers must do it explicitly.
5. **Local codex-review closeout** — before first push of a job-contract execution, run `codex review` per `docs/operations/codex-review-closeout.md` (in song-ai-harness). Loop until clean, then hard-stop.
6. **Project phase / merge authority** — run `orchestrator project show <name>` early in any goal and record the phase. Merge authority by phase (per `~/.openclaw/workspace/orchestrator/README.md` and `lib/risk-classifier.mjs`, Song decision 2026-05-16):
   - `development` → **AI has full merge authority** once gates are green. No schema/migration/secrets/auth/`.github` approval asks. Hard-rule floor does NOT apply. Auto-merge low/medium/high.
   - `production` → LLM risk classifier; low/medium auto-merge, high escalates. Hard-rule floor applies (schema, secrets, `.github/workflows/*`, IAM, payment, auth/session, destructive DDL → auto-high).
   - `meta-self` → Song approval always required, regardless of risk.

   The SSOT key is the short project name (e.g. `knokit`, not `knokit-desktop`); list all with `orchestrator project list`. If a goal prompt instructs "ask before push/PR/merge" in a `development` repo, treat that prompt as **overriding** this rule only for that one goal; otherwise default to AI self-merge.

7. **Product north star** — if the repo has a `docs/PRODUCT.md`, read it before changing code and hold it as the product's intent. The harness makes a change *land*; this rule makes it *right for the product*. Judge every change against it: does it advance the must-never-regress journeys and fit the architectural north star, and is it a root-cause fix or a band-aid (if a band-aid, name the real fix)? Record product fit in the PR body as `HOLISTIC_REVIEW: <root-cause|band-aid> — <fit>` (use `HOLISTIC_REVIEW: n/a — <reason>` for pure tooling/infra). Where the `HOLISTIC_REVIEW` gate is wired into `harness-evidence`, the merge check requires this line; bypass with `holistic-bypass: <reason>`. **These projects are developed and tested 100% by AI agents**, so weigh effort and priority in *agent* terms — agent turns, run/token budget, build→gate cycles, parallel fan-out — never human engineer-days or sprints, and never default to the smallest/simplest/minimal fix to save effort. Agent effort is cheap next to the product's long-term health, so don't shrink a fix to conserve it — the finite run/token budget is real, but it is spent *doing* the root-cause fix, not minimizing the diff. Do the holistic, root-cause fix even when it is larger, and prioritize by product leverage (removing a whole class of issue) rather than smallest diff — repetitive patch-fixing of the same symptom is the anti-pattern this rule exists to stop.
8. **Dual code intelligence tools** — use `codebase-memory-mcp` as the fast default for repo indexing, architecture overview, and broad structural search. Use GitNexus for precise known-symbol context, route sanity checks, and impact analysis. For critical edits, compare both when available and verify against source. Do not let either tool fight repo instructions: routine GitNexus refreshes must use `gitnexus analyze --index-only` or `--skip-agents-md --skip-skills`; do not run `codebase-memory-mcp install` or any config-writing installer unless the PR documents the design and rollback path. See `docs/operations/code-intelligence.md` in song-ai-harness.

## Where the canonical rules live

- `~/.openclaw/workspace/AGENTS.md` — global workspace rules
- `~/Projects/song-ai-harness/AGENTS.md` — harness-owner rules
- `~/Projects/song-ai-harness/docs/operations/*.md` — per-topic SOPs
- `~/Projects/song-ai-harness/docs/decisions/*.md` — ADRs

If a rule below conflicts with a rule in `~/.openclaw/workspace/AGENTS.md`, the workspace AGENTS.md wins for OpenClaw/Luna; the harness AGENTS.md wins for non-OpenClaw agents (Codex, Claude Code).

## Self-test

To verify these rules are reaching your agent: respond with the string `HARNESS_AGENTS_INCLUDE_SEEN` followed by the current repo name when first asked.

<!-- harness template version: 2026-06-24 (dual code-intelligence tools) -->
