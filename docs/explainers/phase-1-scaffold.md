# Phase 1 explainer: the repository scaffold (U5)

This card builds no Phactory feature. It builds the room every later card is built in: packaging, a lint and test gate, a guard that keeps tests off the network, a test that holds the import rule, the CI workflow, and the workflow that moves cards on the board. This page walks through each file, then three mechanisms in more detail.

## Words used below

- **Gate.** The checks a pull request must pass before it can merge. Here that is one GitHub Actions job named `ci`.
- **Runner.** The machine that executes a GitHub Actions job. Today it is a GitHub hosted Ubuntu machine; later it is an instance of the operator's own `zig-actions-runner`.
- **Runner subset (v0).** The small set of workflow features that runner supports: a checkout step and `run:` steps. Phactory's own workflows stay inside it (KTD19), which is why there are no service containers or marketplace actions below.
- **Pytest plugin.** A Python module pytest loads to add options or change how results are reported.
- **Import graph.** The set of edges "module A imports module B" across the package.
- **Layer.** The first name under `phactory`. `phactory.mcp.tools` is in layer `mcp`.
- **Board item.** A card's row on a GitHub Projects board. Its Status field is the column it shows in.

## What each file does

| File | What it does |
|:-|:-|
| `pyproject.toml` | Declares the package (`src` layout, Python 3.12, no runtime dependencies), the `dev` extra with `pytest` and `ruff`, and the pytest and ruff settings. Its `addopts` line loads `_nonet`, `_gate`, and `pytester` as pytest plugins on every run. |
| `src/phactory/__init__.py` | The empty package, so there is something to install and import. |
| `tests/_nonet.py` | The no network guard. Patches `urllib`, `socket`, and `subprocess` so a network call raises `NetworkReached`. |
| `tests/_gate.py` | Pytest plugin that marks tests under `tests/integration/` as integration tests and, when `CI` is set, turns a skipped integration test into a failure. |
| `tests/_repo.py` | `make_repo(path)` builds a bare `origin` repository and a working clone with one commit pushed and `origin/HEAD` set, isolated from your own git config. |
| `tests/_fakes.py` | `FakeRun`, a scripted stand in for the `run` callable adapters use to call `gh` or `git` (KTD8). Unscripted calls fail. |
| `tests/test_imports.py` | The KTD23 import rule as a test. |
| `tests/unit/test_scaffold.py` | U5's test scenarios: the guard, the repository helper, the integration gate, the fakes, the board workflow's script, and a check that the scaffold's own files carry no em or en dashes. |
| `tests/integration/test_postgres.py` | The first integration test: asks the local Postgres for `1 + 1` through `psql`. |
| `.github/workflows/ci.yml` | The gate. Job `ci`, one checkout step and one `run:` step. |
| `templates/phactory-board.yml` | The board status workflow a project copies into its own repository (KTD27). |
| `.github/workflows/phactory-board.yml` | Phactory's own copy, byte for byte the template, so Phactory's board moves the way a customer's does. A test holds the two equal. |
| `.github/ISSUE_TEMPLATE/card.md` | The card form, with the fenced `phactory` block of JSON holding `good`, `paths`, `test`, and `lane`. |
| `CODEOWNERS` | The KTD25 paths that wait for one review, plus the file itself. |
| `CLAUDE.md` | Setup and rules for a cloud session building a card. |

## How the guard is imported first

The guard only works if it patches `subprocess.Popen` before any other module grabs a reference to it. A module that ran `from subprocess import Popen` before the patch keeps the original class forever. So the guard is not imported by a test file or a `conftest.py`; it is named as a plugin in `pyproject.toml`, and pytest imports `-p` plugins while it is still reading its configuration, before it collects a single test.

```
python -m pytest
  |
  v
pytest reads pyproject.toml, sees addopts "-p _nonet -p _gate -p pytester"
  |
  v
import tests/_nonet.py                    <- patches urllib, socket, subprocess
  |                                          (nothing has opened a socket yet)
  v
import tests/_gate.py                     <- registers the integration rule
  |
  v
collect tests: import test_imports.py, test_scaffold.py, test_postgres.py,
               and through them _repo.py, _fakes.py, phactory
  |                                          (every one sees the patched modules)
  v
run tests
```

What the guard refuses:

- `urllib.request.urlopen` and every `OpenerDirector.open`, whatever the URL.
- `socket.connect`, `connect_ex`, and `create_connection` to any address that is not loopback (127.0.0.1, ::1, localhost). Unix domain sockets and loopback stay open, so the local Postgres works.
- `subprocess` launching `gh`, `curl`, `wget`, or `ssh`, or any command with an argument that starts with `http://`, `https://`, `ssh://`, `git://`, or `git@` (so `git fetch` from a real host is refused, while `git` on a local path is not).

`NetworkReached` derives from `BaseException`, not `Exception`, so code under test that catches `Exception` or `OSError` around an HTTP call cannot hide it.

## How the import graph test walks `src/phactory` and names a forbidden edge

`tests/test_imports.py` never imports the package. It reads every `.py` file under `src/phactory` with Python's `ast` module (which parses source into a tree without running it), and for each `import` or `from ... import` statement it works out the full module name being imported:

1. `import phactory.records.store` gives `phactory.records.store`.
2. `from phactory.workflow import card` gives `phactory.workflow`.
3. `from phactory import workflow` gives `phactory.workflow`, because the name after `import` is itself a layer.
4. `from ..workflow import card` inside `phactory/mcp/tools.py` climbs one package up from `phactory.mcp` and gives `phactory.workflow`.

Each result becomes an edge (importer, imported, file, line). Each rule is a small function that says whether an edge is forbidden, judged by the two layers:

| Rule | Forbidden edge |
|:-|:-|
| surfaces import service, config, auth, and onboard only | importer in `mcp`, `page`, or `cli`, imported in any other layer except its own |
| service imports no surface, workflow, or app | importer in `service`, imported in `mcp`, `page`, `cli`, `workflow`, or `app` |
| workflow is imported by nothing outside workflow | imported in `workflow`, importer not in `workflow` (this is how "only `worker.py`" holds, since `worker.py` lives in `workflow/`) |
| workflow imports nothing above it | importer in `workflow`, imported in a surface or `app` |
| onboard/machine.py imports no surface | importer is `phactory.onboard.machine`, imported in a surface |

A violation prints as the rule and the edge, for example:

```
surfaces import service, config, auth, and onboard only: phactory.mcp.tools -> phactory.workflow (src/phactory/mcp/tools.py:1)
```

Each rule needs one layer to exist to mean anything (`service`, `workflow`, or `onboard`). Until that layer has a file, the rule's test skips with "phactory.service does not exist yet". The checker itself is still exercised today by tests that build a small fake package in a temporary directory, including the U5 scenario of a module under `mcp/` importing `workflow/`.

The service rule is written as what service may not reach rather than as KTD23's list of what it may reach. See "Where this differs from the plan" below.

## How `ci.yml` starts Postgres in a run step

The runner subset has no service containers, so the database is started by hand inside the single `run:` step, in this order:

1. `apt-get install` Python 3.12, its `venv` module, and PostgreSQL. On the hosted image these are already present and the install is quick; on a bare runner it makes the step self sufficient.
2. Find the newest `/usr/lib/postgresql/<version>/bin`.
3. `initdb` a fresh data directory under `$RUNNER_TEMP` as the runner user, with `trust` authentication and a superuser named `phactory`. Trust means no password, which is safe only because the next step listens on loopback.
4. `pg_ctl start` with its socket in the same temporary directory, port 54329 (chosen so it never collides with a system Postgres on 5432), and `listen_addresses=127.0.0.1`. The `-w` flag waits until the server accepts connections.
5. `createdb phactory_test` and `export PHACTORY_TEST_DSN=postgresql://phactory@127.0.0.1:54329/phactory_test`.
6. Create `.venv`, install the package with its `dev` extra, run `ruff check .`, then `python -m pytest -q`.

Everything is one step because an `export` only lasts for the step that ran it. GitHub Actions sets `CI=true`, so `_gate` makes the integration suite required: if the database failed to start, `test_postgres.py` would skip, and the skip is reported as a failure. `CLAUDE.md` carries the same commands, plus the one difference a cloud session needs: `initdb` refuses to run as root, so a root session runs the server as the `postgres` user.

`runs-on` reads the repository variable `PHACTORY_RUNS_ON` and falls back to `ubuntu-latest`. When a runner instance exists (U22), setting that variable moves the gate onto it with no edit to this file. There is no `merge_group` trigger, because U2 found this repository has no merge queue.

## How the board workflow finds the card

The workflow runs on two events: a branch whose name starts with `phactory/` is created (target column In progress), and a pull request from such a branch is opened (target column In review). Moving to Done is GitHub Projects' own built in "item closed" workflow, which fires when the merge closes the issue.

It finds the issue in two ways, in this order:

1. On a pull request, a closing keyword in the body, such as `Closes #8`. This comes first because it is the same link GitHub uses to close the issue on merge, so the In review move and the Done move always agree on the issue.
2. Otherwise, and always on branch creation (a new branch has no body to read), the branch name `phactory/u<n>-<slug>` maps to the open issue whose title starts with `U<n>:`. The match is on the exact prefix, so `U5:` never matches `U50:`. The branch carries the unit number rather than the issue number because unit numbers and issue numbers differ (U5 is issue #8).

Then it adds the issue to the project (`gh project item-add` returns the existing item when it is already there), reads the Status field's options with `gh project field-list`, and sets the option by name with `gh project item-edit`.

The workflow is configured only through repository settings, never by editing the file, so the template and Phactory's copy stay identical and the template holds no project's values (R1):

- secret `PHACTORY_BOARD_TOKEN`, a token that can read issues and write the project;
- variable `PHACTORY_BOARD_NUMBER`, the project number (11 for Phactory);
- variable `PHACTORY_BOARD_OWNER`, which falls back to the repository owner;
- optional variables for the Status field name and the two option names, which fall back to `Status`, `In progress`, and `In review`.

With the secret or the project number missing, the job prints a notice and passes. An issue it cannot find is also a notice. A project that lacks the named Status option is an error, because that is a setup mistake the operator should see.

The unit test runs the workflow's own shell script, cut out of the YAML, against a fake `gh` on the `PATH` that records each call, so the script under test is the script that ships.

## Where this differs from the plan

- **Extra files.** `tests/_gate.py` (the integration rule as a plugin, so it loads by name like the guard and can be tested in a child pytest run) and `tests/integration/test_postgres.py` (an integration suite has to contain a test for "executed" to mean anything). Neither is on the card's Files line.
- **The guard is a `-p` plugin, not a first import.** The plan points at native-relay's `tests/_paths.py` pattern, which is not in this checkout. Loading through `addopts` gives the same order with no line any test file must remember.
- **The guard also covers raw sockets and network URLs given to any program,** not only `urllib` and the four named programs.
- **The service rule is a deny list.** KTD23 lists what `service` imports. Read as an allow list it would forbid `service` reading `config`, which later units need. The test forbids what would invert the layering (surfaces, `workflow`, `app`) and leaves the leaf modules open.
- **`CODEOWNERS` protects itself.** KTD25 does not list it, but without that line an auto merged change could remove the review rule.
- **The board workflow reads the project number and owner from repository variables.** The facts for this card named only the secret. Variables keep the template and Phactory's copy identical and keep Phactory's board number out of the file.
