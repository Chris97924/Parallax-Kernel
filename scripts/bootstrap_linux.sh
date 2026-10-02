#!/usr/bin/env bash
# Bootstrap a fresh Parallax instance on Linux (or any POSIX shell).
# Idempotent — safe to re-run. Each machine gets its own independent brain;
# there is no cross-host memory sharing until the v0.6 HTTP server ships.
#
# Usage:
# After cloning:
#   bash scripts/bootstrap_linux.sh [TARGET_DIR]
#
# TARGET_DIR defaults to ./parallax-instance. The venv is created inside the
# cloned repo; the DB + vault live under TARGET_DIR.

set -euo pipefail

REPO_URL="https://github.com/Chris97924/parallax-kernel.git"
BRANCH="feat/adr-006-xcouncil-phase1"
TARGET_DIR="${1:-./parallax-instance}"

say() { printf '\n\033[1;36m[parallax-bootstrap]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m[parallax-bootstrap] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 1. Python 3.11+ ---------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 not found. Install Python 3.11+ first."
fi
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')
[[ "$PY_OK" == "1" ]] || die "Python >= 3.11 required (found $(python3 --version))."
command -v uv >/dev/null 2>&1 || die "uv not found. Install uv first: https://docs.astral.sh/uv/"

# ---- 2. Clone repo if not already inside it ---------------------------------
if [[ -f pyproject.toml ]] && grep -q '^name = "parallax-kernel"' pyproject.toml 2>/dev/null; then
  REPO_DIR="$(pwd)"
  say "running inside existing parallax-kernel clone: $REPO_DIR"
else
  REPO_DIR="$(pwd)/parallax-kernel"
  if [[ -d "$REPO_DIR/.git" ]]; then
    say "repo already cloned at $REPO_DIR, fetching latest $BRANCH"
    git -C "$REPO_DIR" fetch origin "$BRANCH"
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" pull --ff-only origin "$BRANCH"
  else
    say "cloning $REPO_URL (branch $BRANCH)"
    git clone -b "$BRANCH" --depth 1 "$REPO_URL" "$REPO_DIR"
  fi
  cd "$REPO_DIR"
fi

# ---- 3. venv + install -------------------------------------------------------
if [[ ! -d .venv ]]; then
  say "creating .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
say "installing parallax-kernel (editable) from uv.lock, runtime dependencies only"
uv sync --locked

# ---- 4. Bootstrap DB + vault at TARGET_DIR ----------------------------------
TARGET_DIR_ABS="$(cd "$(dirname "$TARGET_DIR")" && pwd)/$(basename "$TARGET_DIR")"
say "bootstrapping instance at $TARGET_DIR_ABS"
python bootstrap.py "$TARGET_DIR_ABS"

# ---- 5. .env template --------------------------------------------------------
ENV_FILE="$TARGET_DIR_ABS/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  say "writing .env template — fill in your API keys before running eval"
  cat >"$ENV_FILE" <<EOF
# Parallax instance config
PARALLAX_DB_PATH=$TARGET_DIR_ABS/db/parallax.db
PARALLAX_VAULT_PATH=$TARGET_DIR_ABS/vault
PARALLAX_SCHEMA_PATH=$REPO_DIR/schema.sql

# API keys (optional — only needed for LLM-backed features / eval harness)
# GEMINI_API_KEY=
# GEMINI_API_KEY_2=
# NVIDIA_API_KEY=
EOF
else
  say ".env already exists at $ENV_FILE (not overwritten)"
fi

# ---- 6. Smoke: CLI works -----------------------------------------------------
say "smoke test — parallax inspect --help"
parallax inspect --help >/dev/null

cat <<EOF

\033[1;32m✓ Parallax instance ready.\033[0m

Instance dir:   $TARGET_DIR_ABS
Repo dir:       $REPO_DIR
Activate venv:  . $REPO_DIR/.venv/bin/activate
Config file:    $ENV_FILE

Next:
  1. Edit $ENV_FILE to add API keys if you plan to run the eval harness.
  2. Run \`parallax inspect --help\` to see CLI options.
  3. Each machine keeps its own DB — no memory sharing until v0.6 server.
EOF
