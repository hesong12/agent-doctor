"""Eval harness for Agent Doctor.

The eval subsystem is what lets us answer "is Agent Doctor actually useful?"
empirically. It deliberately lives in a separate subpackage from the
production scan path so that:

- the only thing imported during ``agent-doctor scan`` stays deterministic and
  network-free, and
- LLM-backed pieces (synthetic generator, judge, replay) can grow without
  contaminating the local-first guarantee.

See ``docs/evaluation.md`` for the full framework description.
"""
