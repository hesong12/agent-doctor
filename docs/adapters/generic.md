# Generic Adapter

The always-available fallback. Used when no host-specific adapter
detects (or for generic JSONL inputs from other frameworks).

## Capabilities

All capability flags are False. Only `send_message` works, and only
when `Target.inbox_path` is set — it writes the message to that
path. It does not emit OS-native notifications; Doctor Pet is the
default user-facing surface.

## When to use

- A new framework not yet supported by a dedicated adapter.
- Forcing inbox-file delivery during testing.
- As a fallback when other adapters degrade.

## Contributing a new adapter

To add support for a new memoryful agent framework:
1. Copy `agent_doctor/adapters/generic.py` to
   `agent_doctor/adapters/<framework>.py`.
2. Implement detection, capabilities, and the methods you can.
3. Subclass `AdapterContractTest` in
   `tests/test_adapters_<framework>.py` to validate.
4. Add an entry in `agent_doctor/capabilities.py:ADAPTER_REGISTRY`.
5. Add a `docs/adapters/<framework>.md`.
