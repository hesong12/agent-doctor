#!/usr/bin/env sh
# Agent Doctor installer — for humans and AI agents.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh -s -- --with-mcp
#   curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh -s -- --with-autopilot
#   curl -fsSL https://raw.githubusercontent.com/hesong12/agent-doctor/main/install.sh | sh -s -- --with-all
#
# What this does, in order:
#   1. Detect platform; install pipx if missing (apt / brew / pip --user fallback).
#   2. Run `pipx ensurepath` so `agent-doctor` lands on PATH.
#   3. `pipx install` (or `pipx install --force` on re-run) Agent Doctor from GitHub.
#   4. Optionally inject [mcp] / [llm] extras into the agent-doctor venv.
#   5. Run `agent-doctor bootstrap --invalidate-cache` so every detected
#      memoryful agent framework on the machine picks up the new skill on
#      its next session, no manual restart needed where supported.
#   6. With --with-autopilot, install and start user-level sidecar services
#      for detected OpenClaw/Hermes homes. This does not modify host runtimes.
#
# This script never sudo's silently — if it needs a sudo prompt (apt
# install pipx) the user sees it. It exits non-zero on any failure so AI
# agents driving it can catch errors.

set -eu

REPO_URL="${AGENT_DOCTOR_REPO:-https://github.com/hesong12/agent-doctor.git}"
REF="${AGENT_DOCTOR_REF:-main}"
EXTRAS=""
SKIP_BOOTSTRAP=0
WITH_AUTOPILOT=0
QUIET=0

log() { [ "$QUIET" -eq 0 ] && printf '%s\n' "$*"; }
err() { printf 'install.sh: %s\n' "$*" >&2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --with-mcp)
            EXTRAS="$EXTRAS mcp>=1.0"
            ;;
        --with-llm)
            EXTRAS="$EXTRAS anthropic>=0.34"
            ;;
        --with-all)
            EXTRAS="$EXTRAS mcp>=1.0 anthropic>=0.34"
            ;;
        --with-autopilot)
            WITH_AUTOPILOT=1
            ;;
        --skip-bootstrap)
            SKIP_BOOTSTRAP=1
            ;;
        --quiet|-q)
            QUIET=1
            ;;
        --ref)
            shift
            REF="${1:-main}"
            ;;
        -h|--help)
            sed -n 's/^# \{0,1\}//; 2,18p' "$0" 2>/dev/null || true
            exit 0
            ;;
        *)
            err "unknown argument: $1"
            exit 2
            ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# 1. Locate or install pipx
# ---------------------------------------------------------------------------

ensure_pipx() {
    if command -v pipx >/dev/null 2>&1; then
        return 0
    fi

    log "pipx not found — installing it first."

    if [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
        brew install pipx
    elif command -v apt-get >/dev/null 2>&1; then
        # apt path requires sudo. Surface the prompt rather than failing silently.
        if [ "$(id -u)" -eq 0 ]; then
            apt-get install -y pipx
        else
            sudo apt-get install -y pipx
        fi
    elif command -v dnf >/dev/null 2>&1; then
        if [ "$(id -u)" -eq 0 ]; then
            dnf install -y pipx
        else
            sudo dnf install -y pipx
        fi
    elif command -v pacman >/dev/null 2>&1; then
        if [ "$(id -u)" -eq 0 ]; then
            pacman -S --noconfirm python-pipx
        else
            sudo pacman -S --noconfirm python-pipx
        fi
    elif command -v python3 >/dev/null 2>&1; then
        # Last-resort fallback: bootstrap pipx via pip itself.
        python3 -m pip install --user pipx 2>/dev/null \
            || python3 -m pip install --user --break-system-packages pipx
    else
        err "no package manager available to install pipx, and python3 is missing."
        err "install pipx manually (https://pipx.pypa.io/), then re-run this script."
        exit 3
    fi

    # Make sure the freshly installed pipx is on PATH for the rest of this run.
    if command -v pipx >/dev/null 2>&1; then
        pipx ensurepath >/dev/null 2>&1 || true
    elif [ -x "$HOME/.local/bin/pipx" ]; then
        export PATH="$HOME/.local/bin:$PATH"
        pipx ensurepath >/dev/null 2>&1 || true
    else
        err "pipx install reported success but the binary is not on PATH."
        exit 4
    fi
}

ensure_pipx

# Make sure the user's PATH for this run includes pipx-managed scripts.
if [ -d "$HOME/.local/bin" ]; then
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) export PATH="$HOME/.local/bin:$PATH" ;;
    esac
fi

# ---------------------------------------------------------------------------
# 2. Install agent-doctor (idempotent)
# ---------------------------------------------------------------------------

SPEC="git+${REPO_URL}@${REF}"
log "Installing agent-doctor from $SPEC ..."

if pipx list 2>/dev/null | grep -q "package agent-doctor "; then
    pipx install --force "$SPEC"
else
    pipx install "$SPEC"
fi

if [ -n "$EXTRAS" ]; then
    log "Injecting extras into the agent-doctor venv: $EXTRAS"
    # shellcheck disable=SC2086
    pipx inject agent-doctor $EXTRAS
fi

# ---------------------------------------------------------------------------
# 3. Bootstrap host skills + invalidate caches so reload happens automatically
#    where the host supports it.
# ---------------------------------------------------------------------------

if [ "$SKIP_BOOTSTRAP" -eq 0 ]; then
    log ""
    agent-doctor bootstrap --invalidate-cache
fi

if [ "$WITH_AUTOPILOT" -eq 1 ]; then
    log ""
    log "Installing Agent Doctor autopilot sidecar services..."
    if [ -d "$HOME/.openclaw" ]; then
        agent-doctor service install \
            --platform openclaw \
            --out "$HOME/.agent-doctor/openclaw" \
            --inbox-dir "$HOME/.agent-doctor/inbox/openclaw" \
            --start
    fi
    if [ -d "$HOME/.hermes" ]; then
        agent-doctor service install \
            --platform hermes \
            --out "$HOME/.agent-doctor/hermes" \
            --inbox-dir "$HOME/.agent-doctor/inbox/hermes" \
            --start
    fi
fi

log ""
log "✓ Agent Doctor is ready."
log "  Try: agent-doctor doctor"
log "  Autopilot: agent-doctor autopilot --platform openclaw --out ~/.agent-doctor/openclaw"
log "  Or just say to your AI agent: 'review my last session'."
