# CLAUDE.md

Rules and setup for any session that builds a card on this repository. Read this first, then the card.

## What Phactory is, in one line

A Python service that hands cards to coding agent builders, lands their pull requests through the host's own merge path, verifies the landing, and scores the result. The plan is `docs/plans/2026-10-14-1730-feat-phactory-first-version-plan.md`.

## Environment setup, run first

Python 3.12, a virtual environment (a private copy of Python and its packages inside `.venv`), and the package installed in editable mode (so edits under `src/` take effect without reinstalling):

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -q -e '.[dev]'
```

A throwaway local Postgres for the integration suite, the same one `.github/workflows/ci.yml` starts. `initdb` refuses to run as root, so a root session runs the server as the `postgres` user:

```sh
PG_BIN=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -n 1)
PG_DIR=/tmp/phactory-pg
mkdir -p "$PG_DIR"
if [ "$(id -u)" = 0 ]; then chown postgres "$PG_DIR"; RUN="runuser postgres -c"; else RUN="sh -c"; fi
[ -d "$PG_DIR/data" ] || $RUN "$PG_BIN/initdb -D $PG_DIR/data -A trust -U phactory > /dev/null"
$RUN "$PG_BIN/pg_ctl -D $PG_DIR/data -l $PG_DIR/log -w -o '-k $PG_DIR -p 54329 -c listen_addresses=127.0.0.1' start"
"$PG_BIN/createdb" -h 127.0.0.1 -p 54329 -U phactory phactory_test 2> /dev/null || true
export PHACTORY_TEST_DSN=postgresql://phactory@127.0.0.1:54329/phactory_test
```

If no Postgres binary exists and none can be installed, say so in the pull request: the integration suite then first runs in CI.

## The gate, run before every push

```sh
ruff check .
CI=true python -m pytest -q
```

With `CI` set, a skipped integration test is a failure (`tests/_gate.py`), exactly as in the pull request's `ci` check. `tests/test_imports.py` is the import rule (KTD23) and runs inside pytest.

## Rules

- **R1, the first law.** Never put a project specific value in code: no customer name, repository, board number, token name, or status label inside `src/phactory`. Every such value is configuration a project supplies.
- **KTD18.** Phactory executes no project code on the control plane. It reads logs and records; it never runs a project's tests or gate commands.
- **KTD23, the import rule.** Surfaces (`mcp`, `page`, `cli`) import `service`, `config`, `auth`, and `onboard` only. `service` imports no surface and no `workflow`. Nothing outside `workflow/` imports `workflow`. `onboard/machine.py` imports no surface. The test names any edge that breaks this.
- **Tests never reach the network.** `tests/_nonet.py` loads before anything else and turns a network call into `NetworkReached`. Use `tests/_fakes.py` and fixtures instead.
- **Define jargon on first use** in one plain sentence. The reader is a returning junior developer.
- **Write the explainer.** Each phase has one file under `docs/explainers/`, written with the code: what each file does and how the pieces connect.
- **No dashes as punctuation.** No em dash, no en dash, and no two hyphens in a row, anywhere in prose, code comments, commit messages, or docs. Use a comma, a period, or a new sentence. Command line flags inside code are code, not punctuation.
- **Commits.** Plain messages, no co author trailers.
- **Review paths.** A change under a path in `CODEOWNERS` waits for one review (KTD25) even with auto merge on. Say so in the pull request when a card touches one.

## The handoff shape

A cloud card's starting comment carries, in this order:

1. Base: the commit on `main` the branch starts from. Record it in the pull request body.
2. Branch: `phactory/u<n>-<slug>`, where `<n>` is the unit number and the issue is titled `U<n>: ...`. The board workflow finds the card from this name.
3. Rules: this file, plus "define jargon on first use and write the explainer under `docs/explainers/`".
4. Files other open cards declare, which this card must not touch.
5. Done line: tests, ruff, and the import test green, pull request opened with `Closes #<issue>` in the body.

The card's own `phactory` block (see `.github/ISSUE_TEMPLATE/card.md`) holds `good`, `paths`, `test`, and `lane`. Its `paths` is the file list, and its `good` is the definition of done.
