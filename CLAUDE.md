# CLAUDE.md

Rules and setup for any session that builds a card on this repository. Read this first, then the card.

## What Slipway is, in one line

A Python service that hands cards to coding agent builders, lands their pull requests through the host's own merge path, verifies the landing, and scores the result. The plan is `docs/plans/2026-10-14-1730-feat-slipway-first-version-plan.md`.

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
PG_DIR=/tmp/slipway-pg
mkdir -p "$PG_DIR"
if [ "$(id -u)" = 0 ]; then chown postgres "$PG_DIR"; RUN="runuser postgres -c"; else RUN="sh -c"; fi
[ -d "$PG_DIR/data" ] || $RUN "$PG_BIN/initdb -D $PG_DIR/data -A trust -U slipway > /dev/null"
$RUN "$PG_BIN/pg_ctl -D $PG_DIR/data -l $PG_DIR/log -w -o '-k $PG_DIR -p 54329 -c listen_addresses=127.0.0.1' start"
"$PG_BIN/createdb" -h 127.0.0.1 -p 54329 -U slipway slipway_test 2> /dev/null || true
export SLIPWAY_TEST_DSN=postgresql://slipway@127.0.0.1:54329/slipway_test
```

If no Postgres binary exists and none can be installed, say so in the pull request: the integration suite then first runs in CI.

## The gate, run before every push

```sh
ruff check .
CI=true python -m pytest -q
```

With `CI` set, a skipped integration test is a failure (`tests/_gate.py`), exactly as in the pull request's `ci` check. `tests/test_imports.py` is the import rule (KTD23) and runs inside pytest.

## Rules

- **R1, the first law.** Never put a project specific value in code: no customer name, repository, board number, token name, or status label inside `src/slipway`. Every such value is configuration a project supplies.
- **KTD18.** Slipway executes no project code on the control plane. It reads logs and records; it never runs a project's tests or gate commands.
- **KTD23, the import rule.** Surfaces (`mcp`, `page`, `cli`) import `service`, `config`, `auth`, and `onboard` only. `service` imports no surface and no `workflow`. Nothing outside `workflow/` imports `workflow`. `onboard/machine.py` imports no surface. The test names any edge that breaks this.
- **Tests never reach the network.** `tests/_nonet.py` loads before anything else and turns a network call into `NetworkReached`. Use `tests/_fakes.py` and fixtures instead.
- **Define jargon on first use** in one plain sentence. The reader is a returning junior developer.
- **Write the explainer.** Each phase has one file under `docs/explainers/`, written with the code: what each file does and how the pieces connect.
- **No dashes as punctuation.** No em dash, no en dash, and no two hyphens in a row, anywhere in prose, code comments, commit messages, or docs. Use a comma, a period, or a new sentence. Command line flags inside code are code, not punctuation.
- **Commits.** Plain messages, no co author trailers.
- **Review paths.** `CODEOWNERS` names the paths where a change lands on the host holding every credential or needs a live smoke; the operator is asked to review them but the merge does not wait. Say so in the pull request when a card touches one, so the operator reads it first after it lands.

## Asking the operator

The operator is the one person who runs this system. By the operator's rule of 2026-10-20, every session that builds, dispatches, or reviews here takes agency first and asks last:

1. **Decide what you can.** Before asking the operator for anything, ask yourself whether it is a decision you can make. If it is, make it with your best recommendation, write it down where the next session will read it (the pull request body, the issue, or the plan), and keep going. Choices of design, order, wording, and which card goes next are yours.
2. **Keep working while you wait.** Ask only for what you truly cannot do: a credential, a device, a paid account, a setting only the operator's login can change, or a decision the plan reserves for the operator. Then carry on with every part of the work that does not depend on the answer, and ask only once a missing answer is the actual bottleneck.
3. **Ask in one step.** Ask for one thing at a time, as a single action or a single choice. When the answer is one of a few options, give them as multiple choice with your recommendation first and one line on what each option does. Never hand the operator a list of setup chores.

## The handoff shape

A cloud card's starting comment carries, in this order:

1. Base: the commit on `main` the branch starts from. Record it in the pull request body.
2. Branch: `slipway/u<n>-<slug>`, where `<n>` is the unit number and the issue is titled `U<n>: ...`. The board workflow finds the card from this name.
3. Rules: this file, plus "define jargon on first use and write the explainer under `docs/explainers/`".
4. Files other open cards declare, which this card must not touch.
5. Done line: tests, ruff, and the import test green, pull request opened with `Closes #<issue>` in the body, auto merge on, and the pull request watched until it merges.

The card's own `slipway` block (see `.github/ISSUE_TEMPLATE/card.md`) holds `good`, `paths`, `test`, and `lane`. Its `paths` is the file list, and its `good` is the definition of done.

`factory/handoff.py` renders this shape from the board, and the `dispatch` skill (`.claude/skills/dispatch/SKILL.md`) is the procedure that posts it and starts the session.

## The two checks on every pull request

- `ci` runs the gate above.
- `review` is a model review: `factory/review.py` hands the diff, the card, and `factory/review-brief.md` to a reviewer model (on the operator's subscription, through the `CLAUDE_CODE_OAUTH_TOKEN` secret) and posts the verdict as a pull request review. A blocking finding turns the check red; fix it and push, and both checks run again. Answer an advisory finding in one line on the pull request. Never weaken a test, force push, or push an empty commit to get green.

The ruleset on `main` also requires a branch to be up to date before it merges, so every landing puts the other open pull requests behind. When yours is green but behind `main`, merge `main` into the branch (a merge commit, never a rebase), run the gate, and push; both checks then run on the result that will actually land. A session's GitHub proxy may refuse some REST writes from `gh`, such as git refs and issue comments; use the GitHub MCP tools for those.

Auto merge waits for both and for nothing else: the review check is the review, by the operator's decision of 2026-10-20. The explainer is `docs/explainers/build-loop.md`.
