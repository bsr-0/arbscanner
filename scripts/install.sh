#!/usr/bin/env bash
# install.sh — bootstrap the arbscanner development environment.
#
# Usage:
#   bash scripts/install.sh           # full install
#   bash scripts/install.sh --help    # show usage
#   bash scripts/install.sh --skip-node   # skip `npm install -g pmxtjs`
#   bash scripts/install.sh --skip-tests  # skip the pytest smoke test
#
# Note: this script does not chmod itself. If you prefer to run it as
#   ./scripts/install.sh
# first make it executable with:
#   chmod +x scripts/install.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Color helpers (basic ANSI). Fall back to plain text if stdout is not a TTY.
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    C_RESET="\033[0m"
    C_GREEN="\033[0;32m"
    C_RED="\033[0;31m"
    C_YELLOW="\033[0;33m"
    C_BLUE="\033[0;34m"
    C_BOLD="\033[1m"
else
    C_RESET=""
    C_GREEN=""
    C_RED=""
    C_YELLOW=""
    C_BLUE=""
    C_BOLD=""
fi

ok()    { printf "${C_GREEN}[ ok ]${C_RESET} %s\n"    "$*"; }
warn()  { printf "${C_YELLOW}[warn]${C_RESET} %s\n"   "$*"; }
err()   { printf "${C_RED}[fail]${C_RESET} %s\n"      "$*" >&2; }
info()  { printf "${C_BLUE}[info]${C_RESET} %s\n"     "$*"; }
banner(){ printf "\n${C_BOLD}==> %s${C_RESET}\n"      "$*"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
SKIP_NODE=0
SKIP_TESTS=0

usage() {
    cat <<'EOF'
Installing arbscanner — development environment bootstrapper.

Usage:
  bash scripts/install.sh [options]

Options:
  --skip-node     Do not run `npm install -g pmxtjs`
  --skip-tests    Do not run the pytest smoke test
  -h, --help      Show this help message and exit

This script verifies prerequisites (python3 >= 3.12, node/npm >= 18, uv),
syncs Python dependencies with uv, installs arbscanner in editable mode
with dev extras, installs the pmxtjs Node sidecar globally, seeds .env
from .env.example if needed, and (optionally) runs a pytest smoke test.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-node)  SKIP_NODE=1; shift ;;
        --skip-tests) SKIP_TESTS=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)
            err "Unknown argument: $1"
            usage
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Resolve project root (parent of this scripts/ directory) so the installer
# works regardless of where it is invoked from.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

printf "${C_BOLD}Installing arbscanner...${C_RESET}\n"
info "Project root: ${PROJECT_ROOT}"

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
banner "Checking prerequisites"

# --- python3 >= 3.12 -------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found on PATH. Install Python 3.12+ (https://www.python.org/downloads/) and retry."
    exit 1
fi

# `python3 --version` prints e.g. "Python 3.12.3"
PY_VERSION_RAW="$(python3 --version 2>&1 | awk '{print $2}')"
PY_MAJOR="${PY_VERSION_RAW%%.*}"
PY_REST="${PY_VERSION_RAW#*.}"
PY_MINOR="${PY_REST%%.*}"

if [[ -z "${PY_MAJOR}" || -z "${PY_MINOR}" ]]; then
    err "Could not parse python3 version string: '${PY_VERSION_RAW}'"
    exit 1
fi

if (( PY_MAJOR < 3 )) || { (( PY_MAJOR == 3 )) && (( PY_MINOR < 12 )); }; then
    err "python3 is ${PY_VERSION_RAW}, but arbscanner requires >= 3.12."
    err "Install a newer Python from https://www.python.org/downloads/ and retry."
    exit 1
fi
ok "python3 ${PY_VERSION_RAW}"

# --- node + npm >= 18 ------------------------------------------------------
if (( SKIP_NODE == 0 )); then
    if ! command -v node >/dev/null 2>&1; then
        err "node not found on PATH. Install Node.js 18+ (https://nodejs.org/) or rerun with --skip-node."
        exit 1
    fi
    if ! command -v npm >/dev/null 2>&1; then
        err "npm not found on PATH. Install Node.js 18+ (which bundles npm) or rerun with --skip-node."
        exit 1
    fi

    # `node --version` prints e.g. "v20.11.0"
    NODE_VERSION_RAW="$(node --version 2>&1)"
    NODE_VERSION="${NODE_VERSION_RAW#v}"
    NODE_MAJOR="${NODE_VERSION%%.*}"

    if [[ -z "${NODE_MAJOR}" ]] || ! [[ "${NODE_MAJOR}" =~ ^[0-9]+$ ]]; then
        err "Could not parse node version string: '${NODE_VERSION_RAW}'"
        exit 1
    fi
    if (( NODE_MAJOR < 18 )); then
        err "node is ${NODE_VERSION_RAW}, but arbscanner requires >= 18."
        err "Upgrade Node.js (https://nodejs.org/) or rerun with --skip-node."
        exit 1
    fi
    ok "node ${NODE_VERSION_RAW}"

    NPM_VERSION_RAW="$(npm --version 2>&1)"
    ok "npm ${NPM_VERSION_RAW}"
else
    warn "--skip-node set; skipping Node.js / npm checks and pmxtjs install."
fi

# --- uv --------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    warn "uv not found on PATH."
    # Only prompt interactively; in non-interactive runs fall back to a clear error.
    if [[ -t 0 ]]; then
        printf "${C_YELLOW}[warn]${C_RESET} Install uv now via the official script? [y/N] "
        read -r REPLY
    else
        REPLY="n"
    fi
    if [[ "${REPLY}" =~ ^[Yy]$ ]]; then
        info "Installing uv from https://astral.sh/uv/install.sh ..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        # The uv installer drops the binary in ~/.local/bin or ~/.cargo/bin; make
        # sure the current shell can see it for the rest of this script.
        export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
        if ! command -v uv >/dev/null 2>&1; then
            err "uv installation finished but 'uv' is still not on PATH."
            err "Open a new shell (so your shell profile re-sources PATH) and rerun this script."
            exit 1
        fi
        ok "uv installed: $(uv --version 2>&1)"
    else
        err "uv is required. Install it manually with:"
        err "    curl -LsSf https://astral.sh/uv/install.sh | sh"
        err "then rerun this script."
        exit 1
    fi
else
    ok "uv $(uv --version 2>&1 | awk '{print $2}')"
fi

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
banner "Syncing Python dependencies with uv"
uv sync
ok "uv sync complete"

banner "Installing arbscanner in editable mode with dev extras"
# Quotes around the extras spec are important so the shell doesn't treat the
# brackets as a glob.
uv pip install -e ".[dev]"
ok "arbscanner[dev] installed"

# ---------------------------------------------------------------------------
# Node sidecar (pmxtjs)
# ---------------------------------------------------------------------------
if (( SKIP_NODE == 0 )); then
    banner "Installing pmxtjs (Node sidecar) globally"

    # Global npm installs usually need root unless the user configured a
    # user-writable prefix. Detect both cases and warn before escalating.
    NPM_PREFIX="$(npm config get prefix 2>/dev/null || echo '')"
    NEEDS_SUDO=0
    if [[ -n "${NPM_PREFIX}" && -d "${NPM_PREFIX}" && ! -w "${NPM_PREFIX}" ]]; then
        NEEDS_SUDO=1
    fi

    if (( NEEDS_SUDO == 1 )); then
        warn "npm global prefix '${NPM_PREFIX}' is not writable by $(whoami)."
        if command -v sudo >/dev/null 2>&1; then
            warn "Will retry 'npm install -g pmxtjs' via sudo — you may be prompted for your password."
            if ! npm install -g pmxtjs; then
                sudo npm install -g pmxtjs
            fi
        else
            err "sudo is not available and '${NPM_PREFIX}' is not writable."
            err "Either configure a user-writable npm prefix (e.g. 'npm config set prefix ~/.npm-global')"
            err "or rerun this script with --skip-node and install pmxtjs yourself."
            exit 1
        fi
    else
        npm install -g pmxtjs
    fi
    ok "pmxtjs installed globally"
fi

# ---------------------------------------------------------------------------
# .env seeding
# ---------------------------------------------------------------------------
banner "Configuring environment file"
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    ok ".env already exists — leaving it untouched."
elif [[ -f "${PROJECT_ROOT}/.env.example" ]]; then
    cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
    ok "Copied .env.example -> .env"
else
    warn "No .env.example found in ${PROJECT_ROOT}; skipping .env creation."
fi

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if (( SKIP_TESTS == 0 )); then
    banner "Running pytest smoke test"
    if [[ -d "${PROJECT_ROOT}/tests" ]]; then
        if uv run pytest tests/ -q; then
            ok "Smoke test passed"
        else
            err "Smoke test failed. Investigate with: uv run pytest tests/ -v"
            exit 1
        fi
    else
        warn "No tests/ directory found; skipping smoke test."
    fi
else
    warn "--skip-tests set; not running pytest."
fi

# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------
banner "Next steps"
cat <<EOF
  1. Edit your environment file:
       \$EDITOR ${PROJECT_ROOT}/.env
     Fill in API keys for Polymarket / Kalshi / Anthropic, Telegram webhook, etc.

  2. Build the market-match cache (maps Polymarket markets to Kalshi tickers):
       uv run arbscanner match

  3. Start the scanner dashboard:
       uv run arbscanner scan

  Helpful extras:
    - Re-run this installer any time:  bash scripts/install.sh
    - Skip the Node sidecar:           bash scripts/install.sh --skip-node
    - Skip the pytest smoke test:      bash scripts/install.sh --skip-tests

EOF
ok "arbscanner install complete. Happy scanning!"
