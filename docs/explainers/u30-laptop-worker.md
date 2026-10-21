# U30 explainer: the laptop worker's entry point

U13 wrote `LaptopBuilder`, which runs one build's session on the laptop as a second user, and U14 wrote `laptop_worker`, which registers the `laptop_build` task under a label. Nothing started either: the launch file ran whatever command its environment file named. This card adds the one command, `python -m slipway.workflow.laptop`, that builds both from the laptop's own environment and starts the worker, and points the launch file at it. A project whose file names a `builder_worker` then builds on the operator's laptop, in a clone or a worktree as its file says, with the tools it grants.

## Words used below

- **Hatchet worker.** A process that connects out to the Hatchet engine on the VPS and runs the tasks it registered. The laptop's runs `laptop_build` only.
- **Label.** A tag a worker registers under. A task that asks for a label goes only to a worker that carries it, so a laptop build reaches this laptop and nothing else (KTD12).
- **Launch file.** The launchd job description (a `.plist`) that macOS uses to start a program at boot and restart it when it exits.
- **Environment file.** A text file of `NAME=value` lines the launch file reads before it starts the worker, so every value lives on the machine.
- **Session user.** The second user each session runs as, through `sudo`, so a session cannot read the worker user's secrets.
- **Shared checkout.** A clone on the laptop that the operator and other sessions already work in; a worktree build adds its working folder from it (U26).
- **Task input.** The JSON the VPS sends with a task. For `laptop_build` it is the handoff, the binding, the budget, and now what the laptop needs of the project.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/workflow/laptop.py` | The entry point. `read_machine` reads the machine's variables and refuses an unset or unreadable one; `session_runner` checks each build can run here, then builds its `LaptopBuilder` and git credential and runs the session through U13's `laptop_build`; `main` hands that runner to `laptop_worker` under the label and starts it. |
| `src/slipway/workflow/card.py` | `laptop_project` puts what the laptop needs of the project file into the task input, and `laptop_build` passes it to the runner. |
| `deploy/laptop/com.slipway.worker.plist` | Runs `python -m slipway.workflow.laptop` in place of the free form `SLIPWAY_WORKER_COMMAND`. |
| `deploy/laptop/README.md` | The environment file's lines, the steps to load the launch file, and the live check. |
| `tests/unit/test_laptop_worker.py` | The card's scenarios, with a fake Hatchet and a recording build in place of a session. |

## What the laptop knows, and where it comes from

The laptop never reads a project file or the operator file (KTD12). It gets two kinds of value from two places:

```
VPS (card._start)                         laptop (workflow/laptop.py)
  project file, operator file               $HOME/.slipway-worker.env
        |                                          |
        v                                          v
  task input: handoff, binding,             SLIPWAY_WORKER_LABEL, SLIPWAY_SESSION_USER,
    project = {slug, host, repository,      SLIPWAY_SESSIONS, SLIPWAY_SESSION_PATH,
      checkout, credential {name, env}}     SLIPWAY_BROWSERS, SLIPWAY_DESKTOP_SERVER,
        |                                   SLIPWAY_CHECKOUTS, the two token variables
        +------------> laptop_build <--------------+
                            |
                            v
              LaptopBuilder(sessions/<slug>, git remote, session user, PATH,
                            checkout, shared checkout, desktop server, browsers)
```

What belongs to the project (its repository, clone or worktree, which token variable holds its git credential) comes in the task input. The binding already carried the account's variable name the same way, and the git credential's now joins it. Both are names; the values are in the laptop's own environment. What belongs to the machine (who the session user is, where the folders and programs are, where each project's shared checkout sits) comes from the environment file. A checkout path is machine specific, so it lives there and never in the project file (R1).

## Refused at start, refused at the build

The worker refuses to start, naming each one, while `SLIPWAY_WORKER_LABEL`, `SLIPWAY_SESSION_USER`, `SLIPWAY_SESSIONS`, or `SLIPWAY_SESSION_PATH` is unset, or while `SLIPWAY_DESKTOP_SERVER` or `SLIPWAY_CHECKOUTS` is set to something that is not a JSON object. A message names the variable, never its value. This follows U29's VPS worker.

The other variables are needed only by some projects, so they are checked when a build arrives, before any session or folder exists:

| The build needs | Refused when |
|:-|:-|
| `headless_browser` granted | `SLIPWAY_BROWSERS` is unset |
| `desktop` granted | `SLIPWAY_DESKTOP_SERVER` is unset |
| `checkout = "worktree"` | `SLIPWAY_CHECKOUTS` names no path for the project's slug |
| any build | the account's or the git credential's variable is unset |

A refused build ends `returned_empty`, halt class `builder_failed`, with the reason "the session did not start: ..." naming what is missing, the same way U27 ends a build whose tool a container cannot grant. `operator_browser` needs nothing of the machine: it is the `--chrome` flag on the session's command line.

A clone project needs no checkout entry.

## Where it departs from what came before

- **The browsers folder is now required for `headless_browser`.** U27 let a laptop with no browsers folder start the build anyway, because only the browser would fail. The card asks that a tool the machine has not set up be refused at the start, and `headless_browser` is the default level, so a laptop builds nothing until `SLIPWAY_BROWSERS` is set. A refusal at the start, naming the variable, is easier to act on than a screenshot step that fails deep in a session.
- **No credential helper of the worker's own.** U29's VPS worker points its git cache at a helper. The laptop worker runs no git of its own; `LaptopBuilder` writes each session's token to its `run/token` and points that session's git at it.
- **`python` comes from `PATH`.** The launch file runs `python`, and the environment file's `PATH` puts the Python environment Slipway is installed in first. The launch file then names no folder of the operator's.

## Left for the operator

Loading the launch file on the laptop and building one card in a worktree there is the live part of the card's done line. The steps are in `deploy/laptop/README.md`, under "The live check".
