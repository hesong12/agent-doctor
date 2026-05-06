# Hermes Adapter (stub)

Hermes is a memoryful agent framework similar to OpenClaw. Agent
Doctor's Hermes adapter currently:
- detects `~/.hermes/`
- declares skill_dir / memory_writable / identity_writable so
  `bootstrap` and `setup autopilot` continue to install SKILL.md
  into the right location
- declares all outbound capabilities False until the Hermes outbound
  CLI surface is identified

## Why partial

Hermes does not yet expose `hermes message send` / reactions /
system-event / infer commands in a stable form (or this maintainer
does not yet have access to them). The adapter is shipped in this
state so Hermes users get graceful degradation through local artifacts and
Doctor Pet rather than runtime errors.

## How to extend

If you maintain a Hermes installation with a stable outbound CLI:
1. Implement the missing methods in `agent_doctor/adapters/hermes.py`.
2. Flip the corresponding capability flags to True.
3. Run `agent-doctor adapters test hermes` to validate against the
   contract.
4. Submit a PR.
