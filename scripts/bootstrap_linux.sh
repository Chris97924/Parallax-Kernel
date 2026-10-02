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
# here (UV_BIN, made absolute) is the one used for the install, even after the
# script changes directory or activates the venv, so a uv inside .venv/bin
# cannot bypass the check.
UV_MIN="0.9.17"
UV_BIN="$(command -v uv)" || die "uv not found. Install uv >= $UV_MIN first: https://docs.astral.sh/uv/"
[[ "$UV_BIN" == /* ]] || UV_BIN="$(CDPATH='' cd -- "$(dirname -- "$UV_BIN")" >/dev/null && pwd)/$(basename -- "$UV_BIN")"
UV_OUT="$("$UV_BIN" --version 2>/dev/null)" && UV_RC=0 || UV_RC=$?
UV_VER="$(awk 'NR == 1 {print $2}' <<<"$UV_OUT")"
if [[ "$UV_RC" -ne 0 || ! "$UV_VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  UV_ERR="$("$UV_BIN" --version 2>&1 >/dev/null || true)"
  if [[ "$UV_RC" -eq 0 ]]; then WHY="printed no plain release version x.y.z"
  elif [[ "$UV_VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then WHY="printed version $UV_VER but exited with status $UV_RC"
  else WHY="exited with status $UV_RC without a plain release version x.y.z"; fi
  die "\`$UV_BIN --version\` $WHY (stdout: '$UV_OUT'; stderr: '$UV_ERR'). uv >= $UV_MIN is required; pre-release and local builds such as 0.9.17rc1 or 0.12.18+3 are refused."
fi
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
    # An existing clone is updated in one state only: origin is $REPO_URL, HEAD
    # is on $BRANCH and the tracked files are clean; then only by fast-forward.
    # Every other state stops before the clone is changed (a fetch may update
    # refs/remotes/origin/$BRANCH and FETCH_HEAD, nothing else; --no-tags and
    # --no-prune keep tags and other refs out of it). No branch is created or
    # switched.
    stop_clone() { die "existing clone $REPO_DIR: $1. Run this script from a fresh directory, or bring the clone to a clean $BRANCH by hand and re-run."; }
    ORIGIN_URLS="$(git -C "$REPO_DIR" remote get-url --all origin 2>/dev/null)" || stop_clone "it has no origin remote"
    BAD_URL="$(grep -vxF -- "$REPO_URL" <<<"$ORIGIN_URLS" | head -n 1 || true)"
    [[ -z "$BAD_URL" ]] || stop_clone "origin fetches from $BAD_URL (after git's URL rewriting), not $REPO_URL"
    CUR_BRANCH="$(git -C "$REPO_DIR" symbolic-ref --quiet --short HEAD)" || stop_clone "HEAD is detached"
    [[ "$CUR_BRANCH" == "$BRANCH" ]] || stop_clone "it is on branch $CUR_BRANCH, not $BRANCH"
    DIRTY="$(git -C "$REPO_DIR" --no-optional-locks status --porcelain --untracked-files=no)" || stop_clone "git status failed"
    [[ -z "$DIRTY" ]] || stop_clone "it has uncommitted changes"
    for lock in "refs/heads/$BRANCH.lock" HEAD.lock index.lock; do
      [[ ! -e "$REPO_DIR/.git/$lock" ]] || stop_clone "lock file .git/$lock exists (another git process, or a stale lock)"
    done
    for op in MERGE_HEAD rebase-apply rebase-merge CHERRY_PICK_HEAD REVERT_HEAD sequencer BISECT_LOG; do
      [[ ! -e "$REPO_DIR/.git/$op" ]] || stop_clone "an unfinished git operation is in progress (.git/$op exists)"
    done
    say "repo already cloned at $REPO_DIR, fast-forwarding $BRANCH"
    git -C "$REPO_DIR" fetch --no-tags --no-prune origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" \
      || stop_clone "fetching $BRANCH from origin failed"
    git -C "$REPO_DIR" merge-base --is-ancestor HEAD "refs/remotes/origin/$BRANCH" \
      || stop_clone "$BRANCH has commits that are not on origin/$BRANCH, so it is not a fast-forward"
    git -C "$REPO_DIR" merge --ff-only --no-overwrite-ignore "refs/remotes/origin/$BRANCH" \
      || stop_clone "fast-forward of $BRANCH failed"
  elif [[ -e "$REPO_DIR" || -L "$REPO_DIR" ]]; then
    die "$REPO_DIR exists but is not a git clone this script can use (no .git directory). Run this script from a fresh directory, or move $REPO_DIR away and re-run."
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
