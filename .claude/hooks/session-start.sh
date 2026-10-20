#!/bin/bash
# SessionStart hook for Claude Code cloud sessions.
#
# It runs the environment setup from CLAUDE.md before the session reads its
# card: the virtual environment, the editable install, and the throwaway
# Postgres the integration suite needs. With it in place a builder session
# can run the gate (ruff check . and CI=true python -m pytest -q) at once.
#
# Every step is safe to repeat. A cached container already holds .venv and
# the Postgres data directory, so a later start only confirms them and
# starts the server. The hook runs only in a cloud session; on a laptop the
# developer owns the setup.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Python and the package. The venv is created once and reused from the
# cached container; the install is repeated because it is quick and picks up
# a dependency a card added.
if [ ! -x .venv/bin/python ]; then
  python3.12 -m venv .venv
fi
.venv/bin/python -m pip install -q -e '.[dev]'

# Put the venv first on the session's PATH so python, pytest, and ruff are
# the project's own.
{
  echo "export VIRTUAL_ENV=\"$CLAUDE_PROJECT_DIR/.venv\""
  echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\""
} >> "$CLAUDE_ENV_FILE"

# The throwaway Postgres, the same one ci.yml starts. initdb refuses to run
# as root, so a root session runs the server as the postgres user.
PG_BIN=$(ls -d /usr/lib/postgresql/*/bin 2> /dev/null | sort -V | tail -n 1 || true)
if [ -z "$PG_BIN" ]; then
  echo "session-start: no Postgres binary found; the integration suite will skip here" \
    "and first run in CI, so say so in the pull request" >&2
  exit 0
fi

PG_DIR=/tmp/slipway-pg
PG_PORT=54329
mkdir -p "$PG_DIR"
if [ "$(id -u)" = 0 ]; then
  chown postgres "$PG_DIR"
  RUN="runuser postgres -c"
else
  RUN="sh -c"
fi

if [ ! -d "$PG_DIR/data" ]; then
  $RUN "$PG_BIN/initdb -D $PG_DIR/data -A trust -U slipway > /dev/null"
fi
if ! $RUN "$PG_BIN/pg_ctl -D $PG_DIR/data status > /dev/null 2>&1"; then
  $RUN "$PG_BIN/pg_ctl -D $PG_DIR/data -l $PG_DIR/log -w \
    -o '-k $PG_DIR -p $PG_PORT -c listen_addresses=127.0.0.1' start > /dev/null"
fi
"$PG_BIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U slipway slipway_test 2> /dev/null || true

echo "export SLIPWAY_TEST_DSN=\"postgresql://slipway@127.0.0.1:$PG_PORT/slipway_test\"" \
  >> "$CLAUDE_ENV_FILE"

echo "session-start: venv installed, Postgres on port $PG_PORT, SLIPWAY_TEST_DSN set"
