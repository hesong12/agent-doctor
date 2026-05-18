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
agent-doctor dictate toggle --whisper-model medium     # default: small
agent-doctor dictate toggle --language zh              # default: auto-detect
agent-doctor dictate toggle --backend whisper-cpp ...  # force a backend (default: auto)
```

Or via environment:

```bash
export AGENT_DOCTOR_DICTATE_WHISPER_MODEL="large-v3-turbo"
export AGENT_DOCTOR_DICTATE_BACKEND="whisper-cpp"   # auto | faster-whisper | whisper-cpp
```

### Backends

| Backend | When | Install |
|---------|------|---------|
| `faster-whisper` (default) | Size aliases (`small`, `medium`, `large-v3-turbo`) and HF repo ids. CTranslate2 + int8 on CPU; portable. | `pip install 'agent-doctor[dictate]'` |
| `whisper-cpp` | Local GGML / GGUF model files (e.g. Handy's `ggml-large-v3-turbo.bin`). Uses **Apple Metal on M-series**, no re-download if you already have the model. | `pip install 'agent-doctor[dictate-cpp]'` (builds whisper.cpp from source; Xcode CLT required) |

**Auto-detect rules** (when `--backend auto`, the default):

- `model_name` ending in `.bin` or `.gguf` → `whisper-cpp`
- `model_name` is a local path that exists → `whisper-cpp`
- Anything else (e.g. `large-v3-turbo`, `Systran/faster-whisper-small`) → `faster-whisper`

### Reusing Handy's GGML model (recommended on Apple Silicon)

If you have [Handy](https://github.com/cjpais/Handy) installed and have downloaded a model, you already have a Metal-accelerated `ggml-large-v3-turbo.bin` you can point at directly:

```bash
agent-doctor dictate toggle --mode coding \
  --whisper-model "$HOME/Library/Application Support/com.pais.handy/models/ggml-large-v3-turbo.bin"
```

Benchmark on M-series Mac (M5 Max): the 1.5 GB `large-v3-turbo` GGML model loads cold in ~4.5 s and transcribes 3-4 s of audio in **~0.5 s** (≈8× real-time), all on the GPU. Pin the path via env var so you do not retype it:

```bash
export AGENT_DOCTOR_DICTATE_WHISPER_MODEL="$HOME/Library/Application Support/com.pais.handy/models/ggml-large-v3-turbo.bin"
```

Set `AGENT_DOCTOR_DICTATE_DEBUG=1` to keep whisper.cpp's verbose stdout logging (Metal init, model metadata, segment timings). Default behaviour suppresses it so the CLI receipt stays clean.

### Larger faster-whisper models

If you do not have a GGML/GGUF file already, faster-whisper will download whatever size you ask for from HuggingFace on first use:

```bash
agent-doctor dictate toggle --whisper-model large-v3-turbo   # ~1.5 GB one-time download
```

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
agent-doctor dictate history  # show recent transcripts + final prompts
```

Every recording variant accepts:

| Flag | Effect |
|------|--------|
| `--print-transcript` | Echo the raw transcription to stderr alongside the JSON receipt. |
| `--keep-audio` | Preserve the captured WAV under `$TMPDIR` for debugging. |
| `--buffer-ms <N>` | Extra recording tail in milliseconds after `stop` is invoked. Avoids cutting your final syllable when releasing the hotkey. Default 150 ms; env: `AGENT_DOCTOR_DICTATE_BUFFER_MS`. |
| `--beep` / `--no-beep` | macOS system sound on start / done / failure. Off by default; env: `AGENT_DOCTOR_DICTATE_BEEP=1`. |
| `--timing` | Print a per-phase millisecond breakdown to stderr (stop, transcribe, enhance, clipboard, buffer, total). |
| `--no-history` | Skip writing this run into the SQLite history. |

## History

Every successful run is logged to a small SQLite database at `~/Library/Application Support/agent-doctor/dictate-history.sqlite3`. Older rows are pruned to the retention limit in the same transaction as the insert, so the file never grows unbounded.

```bash
agent-doctor dictate history                # last 20, table view
agent-doctor dictate history --limit 5      # last 5
agent-doctor dictate history --full         # do not truncate transcripts
agent-doctor dictate history --json         # machine-readable
agent-doctor dictate history --clear        # delete the database
```

Retention: 100 entries by default; override with `AGENT_DOCTOR_DICTATE_HISTORY_LIMIT`. Set `--no-history` (or `AGENT_DOCTOR_DICTATE_HISTORY_LIMIT=0`) to opt out.

Each row captures the transcript, the final clipboard prompt, mode, backend, whisper model, language, and an `enhancer_failed` flag so you can audit fallbacks.

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

## Hotkey configuration

Default binding: `right_cmd` (hold Right Command). Override via
Preferences → Hotkey or by editing `~/.agent-doctor/dictate.json`:

```json
{ "hotkey": { "binding": "right_option", "push_to_talk": true, "daemon_enabled": true } }
```

Valid modifier-only tokens: `left_cmd`, `right_cmd`, `left_option`,
`right_option`, `left_ctrl`, `right_ctrl`, `left_shift`, `right_shift`,
`fn`. Chord tokens follow the existing `mod+mod+key` format.

The Preferences capture overlay disambiguates left vs right; manual
JSON edits are the only way to bind `fn` today.
