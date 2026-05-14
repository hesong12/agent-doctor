# `agent-doctor dictate` — voice → optimized-prompt clipboard

Press a hotkey, speak, paste an LLM-rewritten prompt into any AI application
(Cursor, Claude Code, ChatGPT, Claude Desktop, …). Same general shape as
[Handy](https://github.com/cjpais/Handy), but the transcript is routed through
an OpenAI-compatible chat completion endpoint so what lands on your clipboard
is a *clean, structured prompt* instead of a literal transcription.

> Status: macOS-first. Recording uses `sox`/`rec` (with `ffmpeg` as a
> fallback), clipboard uses `pbcopy`, and notifications use `osascript`. The
> pipeline itself runs anywhere Python runs, but the user-facing commands
> assume Darwin.

## Install

```bash
pip install 'agent-doctor[dictate]'
# or with pipx:
pipx inject agent-doctor 'agent-doctor[dictate]'
```

System dependencies (one-time):

```bash
brew install sox            # primary recorder; or 'brew install ffmpeg' for the fallback
# faster-whisper picks up CoreML / Metal automatically on Apple Silicon
```

## Quick start

```bash
# 1. Start recording (defaults to the 'chat' mode).
agent-doctor dictate start

# 2. Speak. Take your time.

# 3. Stop, transcribe, enhance, and copy the result.
agent-doctor dictate stop

# 4. Cmd+V in any AI app.
```

For hotkey use, bind a single command:

```bash
agent-doctor dictate toggle --mode coding
```

`toggle` starts a recording if none is in flight, otherwise stops the existing
one and runs the rest of the pipeline. Bind it to a Karabiner / Hammerspoon /
Raycast / Stream Deck shortcut.

## Modes

| Mode | What the LLM does |
|------|-------------------|
| `chat` (default) | Light cleanup. Drop filler words, fix obvious typos, keep the user's voice. |
| `coding` | Rewrite as an engineering task for a coding agent: one-line summary, bulleted acceptance criteria, explicit constraints — only including what the user actually said. |
| `research` | Rewrite as a research brief: objective, scope/entities/timeframe, output format. |
| `raw` | Skip the LLM. Copy the verbatim transcription. |

Switch with `--mode`:

```bash
agent-doctor dictate toggle --mode research
agent-doctor dictate toggle --mode raw            # no LLM at all
agent-doctor dictate toggle --no-enhance          # same effect: copy transcript only
```

## LLM configuration

The enhancer expects an **OpenAI-compatible** chat completion endpoint. The
default targets a local `ds4-server` (DeepSeek V4 Flash) on `http://localhost:8080`:

```
URL    = http://localhost:8080/v1/chat/completions
Model  = ds4
API key = (none)
```

Override at any level (CLI > env > default):

```bash
agent-doctor dictate toggle \
  --llm-url   http://localhost:11434/v1/chat/completions \
  --llm-model llama3.1:8b-instruct
```

Or via environment variables:

```bash
export AGENT_DOCTOR_DICTATE_LLM_URL="https://api.openai.com/v1/chat/completions"
export AGENT_DOCTOR_DICTATE_LLM_MODEL="gpt-4o-mini"
export AGENT_DOCTOR_DICTATE_LLM_KEY="sk-..."
```

If the LLM endpoint is unreachable, the pipeline **degrades to the raw
transcript** so you always get something on the clipboard. The fallback is
announced on stderr and in the notification ("Dictate (raw)").

## Whisper configuration

```bash
agent-doctor dictate toggle --whisper-model medium   # default: small
agent-doctor dictate toggle --language zh            # default: auto-detect
```

Or via environment:

```bash
export AGENT_DOCTOR_DICTATE_WHISPER_MODEL="medium"
```

Larger models (`medium`, `large-v3`) are slower but more accurate. On an M-series
Mac the `small` model is typically faster than real-time.

## Files

| Path | Purpose |
|------|---------|
| `~/Library/Application Support/agent-doctor/dictate.state.json` | In-flight recorder PID, audio path, mode. Set `AGENT_DOCTOR_DICTATE_STATE_DIR` to override. |
| `$TMPDIR/agent-doctor-dictate-<uuid>.wav` | Captured audio. Deleted after a successful run unless `--keep-audio` is passed. |

## Commands

```text
agent-doctor dictate start    [--mode ...] [--whisper-model ...] [--llm-url ...] ...
agent-doctor dictate stop     [--whisper-model ...] [--llm-url ...] ...
agent-doctor dictate toggle   (same flags as start; recommended for hotkey use)
agent-doctor dictate status   # JSON: recording? pid, mode, elapsed seconds
agent-doctor dictate cancel   # discard the in-flight recording
```

Every variant accepts `--print-transcript` (echo the raw transcription to
stderr alongside the JSON receipt) and `--keep-audio` (preserve the WAV for
debugging).

## Privacy

- Audio and transcript stay on the machine when both `faster-whisper` and the
  LLM endpoint are local (e.g. ds4, Ollama, llama.cpp).
- If you point `--llm-url` at a hosted API, the transcript leaves the machine
  in a single chat completion call. The audio never does.
- The state file and audio path are written under your user account; no
  daemons, no background sidecars.

## Karabiner example

```jsonc
{
  "description": "Hyper+V → agent-doctor dictate toggle (coding)",
  "manipulators": [{
    "type": "basic",
    "from": { "key_code": "v", "modifiers": { "mandatory": ["left_command","left_option","left_control","left_shift"] }},
    "to": [{ "shell_command": "/Users/<you>/.local/bin/agent-doctor dictate toggle --mode coding" }]
  }]
}
```
