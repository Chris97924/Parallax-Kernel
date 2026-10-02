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
BRANCH="main-next"
TARGET_DIR="${1:-./parallax-instance}"

say() { printf '\n\033[1;36m[parallax-bootstrap]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m[parallax-bootstrap] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 1. uv + Python 3.11+ ----------------------------------------------------
# uv.lock is written with a relative exclude-newer cooldown ("7 days" in
# pyproject.toml). uv older than 0.9.17 cannot parse it, ignores the lock and
# resolves afresh, so refuse to continue with such a uv. The binary checked
# here (UV_BIN) is the one used for the install, even after the venv is
# activated, so a uv inside .venv/bin cannot bypass the check.
UV_MIN="0.9.17"
UV_BIN="$(command -v uv)" || die "uv not found. Install uv >= $UV_MIN first: https://docs.astral.sh/uv/"
UV_OUT="$("$UV_BIN" --version 2>&1)" || die "\`$UV_BIN --version\` failed (got: $UV_OUT); uv >= $UV_MIN is required"
UV_VER="$(awk '{print $2}' <<<"$UV_OUT")"
[[ "$UV_VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "could not read a release version x.y.z from \`$UV_BIN --version\` (got: $UV_OUT); uv >= $UV_MIN is required"
version_ge() { # numeric x.y.z comparison: is $1 >= $2 ?
  local -a a b
  local i
  IFS=. read -r -a a <<<"$1"
  IFS=. read -r -a b <<<"$2"
  for i in 0 1 2; do
    if (( 10#${a[i]} > 10#${b[i]} )); then return 0; fi
    if (( 10#${a[i]} < 10#${b[i]} )); then return 1; fi
  done
  return 0
}
version_ge "$UV_VER" "$UV_MIN" || die "uv $UV_VER found ($UV_BIN), but uv >= $UV_MIN is required to read uv.lock. Upgrade uv: https://docs.astral.sh/uv/"

if ! command -v python3 >/dev/null 2>&1; then
  die "python3 not found. Install Python 3.11+ first."
fi
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')
[[ "$PY_OK" == "1" ]] || die "Python >= 3.11 required (found $(python3 --version))."

# ---- 2. Clone repo if not already inside it ---------------------------------
if [[ -f pyproject.toml ]] && grep -q '^name = "parallax-kernel"' pyproject.toml 2>/dev/null; then
  REPO_DIR="$(pwd)"
  say "running inside existing parallax-kernel clone: $REPO_DIR"
else
  REPO_DIR="$(pwd)/parallax-kernel"
  if [[ -d "$REPO_DIR/.git" ]]; then
    say "repo already cloned at $REPO_DIR, updating it to the latest $BRANCH"
    # Explicit refspec: a clone made with `-b <other branch> --depth 1` only
    # tracks that one branch. Local commits and changes are never discarded:
    # anything that is not a clean switch plus fast-forward stops the script.
    git -C "$REPO_DIR" fetch origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" \
      || die "could not fetch $BRANCH from origin into $REPO_DIR"
    if git -C "$REPO_DIR" show-ref --verify --quiet "refs/heads/$BRANCH"; then
      git -C "$REPO_DIR" checkout "$BRANCH" \
        || die "could not switch $REPO_DIR to $BRANCH (local changes in the way?); commit or stash them, then re-run"
      git -C "$REPO_DIR" merge --ff-only "origin/$BRANCH" \
        || die "$BRANCH in $REPO_DIR cannot fast-forward to origin/$BRANCH (local commits or changes); nothing was discarded, resolve it by hand, then re-run"
    else
      git -C "$REPO_DIR" checkout --no-track -b "$BRANCH" "origin/$BRANCH" \
        || die "could not switch $REPO_DIR to a new $BRANCH branch (local changes in the way?); commit or stash them, then re-run"
    fi
    [[ "$(git -C "$REPO_DIR" rev-parse HEAD)" == "$(git -C "$REPO_DIR" rev-parse "origin/$BRANCH")" ]] \
      || die "$BRANCH in $REPO_DIR has local commits that are not on origin/$BRANCH; nothing was discarded, resolve it by hand, then re-run"
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
# Inexact sync: versions come from uv.lock, but nothing already in .venv is
# removed, so extras an operator added (e.g. server) survive a re-run. The dev
# extra is not installed; tools left behind by an earlier bootstrap stay until
# someone runs an exact `uv sync --locked` on purpose.
"$UV_BIN" sync --locked --inexact

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
