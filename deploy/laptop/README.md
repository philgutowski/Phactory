# The laptop worker

The laptop worker is the Hatchet worker on the operator's laptop. It connects out to the engine on the VPS, takes `laptop_build` tasks only, under the label of this machine's entry in the operator file's `builder_workers`, and runs each build's session as a second user (KTD12). The launch file `com.slipway.worker.plist` keeps it running; it starts `python -m slipway.workflow.laptop` (U30). How the pieces fit is in `docs/explainers/u30-laptop-worker.md`.

Two users take part. The **worker user** is the dedicated user the launch file runs as; it holds the secrets below. The **session user** is the second user every session runs as, through `sudo`, with only the session's own variables kept. Setting both up is in `docs/explainers/u13-cli-builder.md`, and the shared checkout's group access in `docs/explainers/u26-first-project-lane.md`.

## The environment file

The launch file reads `$HOME/.slipway-worker.env` in the worker user's home: mode 600, owned by the worker user. Each line is `NAME=value`, read by `/bin/sh`, so a value holding spaces or quotes goes in single quotes. Every value is this machine's, and none appears in Slipway's code (R1).

| Line | Needed | What it holds |
|:-|:-|:-|
| `PATH` | always | Puts the `bin` folder of the Python environment Slipway is installed in first, so the launch file's `python` is that one. |
| `SLIPWAY_WORKER_LABEL` | always | The `label` of this machine's entry in the operator file's `builder_workers`. |
| `SLIPWAY_SESSION_USER` | always | The second user each session runs as. Never `root`. |
| `SLIPWAY_SESSIONS` | always | The folder session directories live in, one folder per project under it. The worker user writes it; the session user reads it through the worker's group. |
| `SLIPWAY_SESSION_PATH` | always | The `PATH` a session gets: where `claude`, `git`, and `playwright-mcp` are for the session user. |
| `SLIPWAY_BROWSERS` | a project granting `headless_browser`, the default level | The folder Playwright's browsers are installed in (`docs/explainers/u27-builder-tools.md`). |
| `SLIPWAY_DESKTOP_SERVER` | a project granting `desktop` | This machine's computer use MCP server entry, a JSON object with `command`, and optionally `args` and `env`. |
| `SLIPWAY_CHECKOUTS` | a project with `checkout = "worktree"` | A JSON object from project slug to the path of that project's shared checkout. |
| `HATCHET_CLIENT_TOKEN` | always | This worker's own Hatchet token, minted in the Hatchet dashboard for this worker alone. The token names the engine's public gRPC address, `SLIPWAY_DOMAIN:7077` (`HATCHET_GRPC_BROADCAST_ADDRESS` in `deploy/README.md`), so the laptop sets no address of its own. |
| the account variable | always | The subscription token, under the variable name the operator file's `accounts` entry gives (its `env`). |
| the git credential variable | always | The repository token, under the variable name the operator file's `host_credentials` entry gives (its `env`). |

A sketch, every value invented:

```sh
PATH=/Users/worker/slipway/.venv/bin:/usr/bin:/bin
SLIPWAY_WORKER_LABEL=example-laptop
SLIPWAY_SESSION_USER=slipway-session
SLIPWAY_SESSIONS=/Users/Shared/slipway/sessions
SLIPWAY_SESSION_PATH=/opt/homebrew/bin:/usr/bin:/bin
SLIPWAY_BROWSERS=/Users/Shared/slipway/browsers
SLIPWAY_DESKTOP_SERVER='{"command": "/opt/homebrew/bin/computer-use-mcp", "args": []}'
SLIPWAY_CHECKOUTS='{"example-app": "/Users/operator/code/example-app"}'
HATCHET_CLIENT_TOKEN=...
EXAMPLE_SUBSCRIPTION_TOKEN=...
EXAMPLE_REPOSITORY_TOKEN=...
```

The worker refuses to start while one of the four lines marked "always" that start with `SLIPWAY_` is unset, and names it in the log. The last three `SLIPWAY_` lines are needed only by a project that uses them: a build that needs one this machine lacks ends `returned_empty` before any session starts, with the variable named in its reason. A missing account or git token is refused the same way.

## Loading the launch file

1. Install Slipway for the worker user: clone the repository, then `python3.12 -m venv .venv` and `.venv/bin/python -m pip install .` inside it. Put that `.venv/bin` first on the environment file's `PATH`.
2. Write `$HOME/.slipway-worker.env` as above, as the worker user, then `chmod 600` it.
3. Copy the launch file and name the worker user in it:

   ```sh
   sudo cp deploy/laptop/com.slipway.worker.plist /Library/LaunchDaemons/
   sudo plutil -replace UserName -string <worker user> /Library/LaunchDaemons/com.slipway.worker.plist
   sudo launchctl bootstrap system /Library/LaunchDaemons/com.slipway.worker.plist
   ```

4. Read `$HOME/Library/Logs/slipway-worker.log` in the worker user's home. A refusal names the variable; otherwise the worker appears in the Hatchet dashboard under its label.
5. After a change to the environment file, restart it with `sudo launchctl kickstart -k system/com.slipway.worker`.

To unload it, `sudo launchctl bootout system/com.slipway.worker`.

## The live check

Dispatch one card of a project whose file names this machine's `builder_worker` and sets `checkout = "worktree"`. The build moves to `building` with this worker's label, the session works in a worktree of the shared checkout under `SLIPWAY_SESSIONS`, the worktree is gone after the build returns, and the shared checkout's own branch and files are unchanged. Record the run in the pull request that asks for it.
