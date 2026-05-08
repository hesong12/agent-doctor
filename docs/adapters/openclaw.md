# OpenClaw Adapter

This adapter integrates Agent Doctor with OpenClaw via the public
`openclaw` CLI.

## Detection

The adapter reports detected when `~/.openclaw/` exists. Capability
flags additionally require the `openclaw` binary to be reachable
(via `PATH` or `HOST_BIN_DIRS` fallback to `/opt/homebrew/bin` and
similar paths).

## Capabilities

When the binary is reachable, all flags are True:
- `can_send_message` — uses `openclaw message send`
- `can_edit_message` — uses `openclaw message edit`
- `can_react` — uses `openclaw message react`
- `can_list_reactions` — uses `openclaw message reactions list`
- `can_inject_system_event` — uses `openclaw system event`
- `can_infer_text` — uses `openclaw infer model run`
- `can_infer_embedding` — uses `openclaw infer embedding create`

## Configuration

The adapter inherits OpenClaw's existing model and channel
configuration. To override the model used for Tier 2 classifier
calls, set `[host.openclaw].inference_model` in
`~/.agent-doctor/config.toml` (Phase 2 will introduce this config
file; not yet present).

## TUI fallback

OpenClaw's local TUI does not have a separate-identity surface; the
adapter's `send_message` falls through to inbox-file delivery via
GenericAdapter when `Target.kind() == "tui"` (or `"inbox"`). Phase 3
may revisit this if OpenClaw exposes an in-TUI advisory mechanism.

## Known gaps

None at v1.

## Contributing

If you find an OpenClaw subcommand you'd like the adapter to wrap,
add a method, raise the corresponding capability flag, and run
`agent-doctor adapters test openclaw` to verify. Pull requests
welcome.
