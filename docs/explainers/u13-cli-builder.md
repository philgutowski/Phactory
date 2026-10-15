# U13 explainer: the headless CLI adapter and the laptop worker

U11 wrote the contract every builder adapter implements. This card writes the second adapter: it runs the Claude Code command line, logged in to a subscription account (or an API key), with no person at the keyboard, either in a Docker container on the VPS or as a process on the laptop. It returns the same `BuilderResult` and the same shape records as the Managed Agents adapter, so the card workflow cannot tell them apart.

## Words used below

- **Headless session.** The `claude` command run with `-p` (print mode): it reads one prompt, works until done, writes its answer, and exits. Nobody approves its steps.
- **Permission mode.** How the CLI decides whether a tool call needs a person's approval. `bypassPermissions` approves everything, so the container or the session user is the boundary instead of a prompt.
- **Container.** A process Docker runs with its own filesystem, user, and network, started from an image. The image here is built from `deploy/builder.Dockerfile`.
- **Bind mount.** A directory on the host that appears inside the container at a path. Each session gets its own; no two sessions share one.
- **Read only root.** The container's own filesystem cannot be written; only the mounted workspace and a scratch `/tmp` can.
- **Isolated network.** A Docker network whose traffic can reach the code host and package registries and nothing else, not the other compose services and not the VPS's private addresses. The deploy card creates it; the adapter only names it.
- **`CLAUDE_CONFIG_DIR`.** The directory where the CLI keeps its settings, login, and transcripts. Each session starts with an empty one.
- **Transcript.** The CLI's own log of a session, one JSON object per line, at `$CLAUDE_CONFIG_DIR/projects/<project>/<session id>.jsonl`.
- **Credential helper.** A small program git asks for a username and password when it needs them. Here it is a shell function that prints the contents of a token file, so the token is never in a URL or on a command line.
- **Hatchet worker.** A process that connects out to the Hatchet engine and runs the tasks the engine hands it. A worker label is a name the worker registers under; a task that asks for a label only goes to a worker with it.
- **launchd, plist.** launchd is the macOS service supervisor. A plist is the XML file that tells it what to run, as which user, and to restart it when it exits.
- **Session reference.** The adapter's name for one session. Here it is also the container name, `phactory-<card key>-<8 random hex>`.

## What each file does

| File | What it does |
|:-|:-|
| `src/phactory/builders/cli.py` | `ContainerBuilder` (one container per card, `remote_session`), `LaptopBuilder` (a session as a second user, `worker_process`), `laptop_build` (the body of the laptop child task), `worker_label` (routing), `map_transcript` (transcript to shape records), `scrub_record`, and `push_side_reference` (the one writer of the build's log commit). |
| `deploy/builder.Dockerfile` | The builder image: a base image, git, the CLI at a pinned version, and a non root user. Every value is a build argument. |
| `deploy/laptop/com.phactory.worker.plist` | The launchd job that keeps the laptop's Hatchet worker running as its dedicated user. |
| `tests/unit/test_cli_builder.py` | The card's test scenarios, plus the mapper, the side reference, the deploy files, and the import rule. |

## How a session starts in the container

`ContainerBuilder(root, git, image, network, user)` is built once per project by the worker. `git` is a `GitAccess`: the clone URL with no credential in it, and the username the host expects beside a token. `start(handoff, binding, credential)` then:

1. Reads the account token from the environment variable the operator file names for the binding's account. An unset variable raises `BindingInvalid`, which ends the build as `returned_empty` before any spend.
2. Makes the session directory, `<root>/<session ref>/`, with `run/` (the token file at mode 600 and `handoff.txt`) and `workspace/` (`home/`, an empty `config/`, an empty `repo/`, `out/`).
3. Runs `docker run --detach` with the KTD12 baseline:

| Setting | Value |
|:-|:-|
| `--user` | the non root `uid:gid` the adapter was given; `0` and `root` are refused |
| `--read-only`, `--tmpfs /tmp` | nothing outside the workspace and `/tmp` can be written |
| `--network` | the isolated builder network; `host`, `bridge`, and `default` are refused |
| `--cap-drop ALL`, `--security-opt no-new-privileges` | no Linux capabilities, no privilege gain |
| mounts | `workspace/` at `/workspace`, `run/` at `/run/phactory` read only. No Docker socket. |
| `--env NAME=value` | every non secret value the session script reads |
| `--env CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) | the name only. Docker copies the value from its own client environment, which the adapter sets for that one call, so the token is never on a command line where `ps` would show it. |

4. The container runs `SESSION_SCRIPT`, the same script the laptop runs: set the git credential helper and identity, clone the card's branch fresh into `/workspace/repo`, then `claude -p` with a fixed `--session-id`, the binding's model, the unattended permission mode, and the handoff on standard input. Never `--bare`, so the session reads the repository's own `CLAUDE.md`.

`poll` asks `docker inspect` for the container's state. While it runs, `poll` also refreshes the credential when it is due. Once it has exited, `poll` finishes the session (below) and returns a terminal status; after that it answers from `state.json` without asking Docker again.

`release` removes the container and the whole session directory. `delete` does nothing more, because a CLI session leaves no provider record.

## How a session starts on the laptop

The laptop runs a Hatchet worker as a dedicated user with its own home. That user holds three secrets only: its Hatchet worker token, the subscription token, and the git credential. It never holds the Postgres connection or the operator file.

`LaptopBuilder(root, git, session_user, path)` makes the same session directory, then runs:

```
sudo --non-interactive --user <session user> --preserve-env=<the session's own names> -- /bin/sh -c SESSION_SCRIPT
```

The environment it passes is built from nothing: `PATH`, the session's variables, and the account token. The worker's own variables (a database URL, `HATCHET_CLIENT_TOKEN`, `PHACTORY_HOME`) are never copied. `start` blocks until the session exits, which is what `worker_process` means; a background thread refreshes the token file while it waits. The token file is mode 640 here, because the session user reads it through the group it shares with the worker user.

`laptop_build(builder, handoff, binding, credential)` is the body of the `laptop_build` child task that U14 registers. It runs one session and returns a `LaptopReturn`: the `BuilderResult`, the already scrubbed transcript, and the count of skipped lines, all plain JSON so Hatchet can carry them. It then releases the session. The parent on the VPS writes every record and the side reference from what came back.

`worker_label(project, operator)` is the routing. The project file's `builder_worker` names an entry in the operator file's `builder_workers`, and that entry's `label` is what the child task asks for. The worker connects out to the engine, so a laptop that is closed or asleep simply leaves the task queued.

Operator setup the code assumes (recorded here because no code can check it from the VPS):

- Two macOS users: the worker user and the session user, sharing one group. The session root directory is owned by the worker user with group write.
- A sudoers rule letting the worker user run `/bin/sh` and `rm` as the session user without a password, keeping the variables the adapter lists.
- `claude` and `git` on the `path` the adapter is given.

## How the worker is supervised on the laptop

`deploy/laptop/com.phactory.worker.plist` is a launch daemon, so it runs without anyone logged in. It carries no comment, because an XML comment is written with two hyphens, and no value except its own label: the user name is a placeholder replaced at install. Install it with:

```sh
sudo cp deploy/laptop/com.phactory.worker.plist /Library/LaunchDaemons/
sudo plutil -replace UserName -string <worker user> /Library/LaunchDaemons/com.phactory.worker.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.phactory.worker.plist
```

It runs `/bin/sh`, which loads `$HOME/.phactory-worker.env` (mode 600, owned by the worker user) and starts `$PHACTORY_WORKER_COMMAND`, appending its output to `$HOME/Library/Logs/phactory-worker.log`. The environment file sets:

| Variable | What it is |
|:-|:-|
| `PHACTORY_WORKER_COMMAND` | the command that starts the Hatchet worker; U14 provides it |
| `PHACTORY_WORKER_LABEL` | the label the operator file gives this worker |
| `HATCHET_CLIENT_TOKEN` | this worker's own Hatchet token |
| the account variable, the git credential variable | the two builder secrets |

`KeepAlive` restarts the worker whenever it exits, `ThrottleInterval` waits thirty seconds between restarts, and `Umask` 027 keeps new files out of reach of other users.

## How credentials reach the process, and never the handoff

```
operator file names a variable           handoff text says "the credential named example-app"
        |                                (a name only; render never sees a value)
        v
worker environment holds the value
        |
        +-> account token: docker client env for one call -> container env by name
        |                  (laptop: sudo with that one variable kept)
        |
        +-> git credential (Credential object)
                |
                v
           run/token, mode 600 (laptop 640), replaced atomically on refresh
                |
                v
           git asks the credential helper, which prints the file at that moment
```

The handoff never carries a value: `render` takes no credential argument. The account token is a variable the CLI reads. The git credential is a file the helper reads at the moment git asks, so a token rewritten after fifty minutes is the one the next push uses, and a remote URL with a credential in it is refused when `GitAccess` is built.

Refresh follows follow up #54. `refresh_if_due` refreshes only when the credential has an expiry and `Credential.due` says it ends within ten minutes, which for a one hour installation token is a fifty minute cadence. A credential with no expiry, such as a static Bitbucket repository access token, is never refreshed, so it never raises in the middle of a build.

Every value the adapter injected into a session (the account token and every git token it ever wrote) is kept in memory for that session so redaction can remove it, then forgotten at `release`.

## How the transcript becomes shape records

When the container exits (or the laptop session returns), `_finish`:

1. Copies `config/projects/*/<session id>.jsonl` out of the workspace, passes it through `redact.scrub` with every injected value, and writes it as `transcript.jsonl` in the session directory. The raw copy is never written anywhere else.
2. Reads `out/result.json`, the CLI's final JSON answer, for the cost and stop reason.
3. Asks the remote, with `git ls-remote` and the credential helper, where the card's branch now points. A missing branch, or one still at the base, means nothing was pushed: the result is `returned_empty` with the exit code in its reason and halt class `builder_failed`. Otherwise the tip is the result's commit. The workflow still runs `check_base` and confirms the commit itself (KTD6).

`map_transcript` reads the transcript in two passes. The first collects every `tool_result` by the id of the `tool_use` it answers, and the last line of each assistant message (the CLI splits one message over several lines that share its id). The second walks the lines in order:

```
transcript line                                  shape records
assistant message msg_1 (first line seen)    ->  ModelRequest, from the line before to msg_1's last line
  tool_use Bash toolu_1, command "pytest -q" ->  ToolCall toolu_1 (joined with its tool_result)
                                                 Command "pytest -q", exit 0, or none on error
  tool_use Write toolu_2, file_path          ->  ToolCall toolu_2, FileChange created or modified
  tool_use WebFetch toolu_3, url             ->  ToolCall toolu_3, Connection host and port
  tool_use with no tool_result               ->  ToolCall with an error
a line that is not a JSON object             ->  skipped and counted, never fatal
```

`events(session_ref)` maps the scrubbed transcript and passes every string field of every record through `scrub_record` once more, so a value that only appears after joining is still removed.

## The side reference: one writer, one procedure

Follow up #56 asks who writes the log commit. This adapter answers: the adapter is the one writer of the commit that closes a build. `push_side_reference` (called through `push_log` for a container, or by the parent with a `LaptopReturn` for the laptop) runs only git plumbing on a bare repository in the session directory, with hooks pointed at nothing, so no project code runs (KTD18):

1. Read the tip of `refs/phactory/log/<key>` with `ls-remote`, and fetch it when there is one.
2. If that tip carries `actions.jsonl` (the builder's own action log, which the handoff asks it to push), carry it over, scrubbed.
3. Write `trace.jsonl` (the scrubbed shape records) and `transcript.jsonl` (the scrubbed transcript) as blobs, make a tree of the files, and commit it with the tip as its parent and the message `phactory log <key> build <build id>`.
4. Push that commit to the reference. The token file it used is deleted when it is done.

So the builder's own commit stays in the history, and the build's last commit always holds the full set.

## The two deployment shapes

```
VPS                                                   laptop
==================================================    ======================================
Hatchet engine  <== gRPC over TLS, worker connects out ==  worker user (launchd KeepAlive)
  |                                                      holds: worker token, account
  v                                                      token, git credential
worker: card workflow, ContainerBuilder                  runs: the laptop_build child only
  holds: Postgres URL, operator file, tokens               |
  |                                                        | sudo, session variables only
  | docker run, one container per card                     v
  v                                                      session user
builder container on the isolated network                  empty CLAUDE_CONFIG_DIR, clone,
  non root, read only root, no Docker socket,              claude -p, git push
  empty CLAUDE_CONFIG_DIR, clone, claude -p,               |
  git push                                                 v
  |                                                      result and scrubbed transcript
  v                                                      go back to the parent on the VPS
worker writes the records and the side reference  <=====  (as the laptop_build output)

Both push the card's branch to the code host under the machine identity.
```

## Imports

`cli.py` imports `builders.base`, `builders.handoff`, `config`, `redact`, `trace.shape`, and the standard library. It never imports `service`, `workflow`, `gitread`, `records`, or a surface; a test holds that.

## Where this differs from the plan

- **Two classes, not one.** The container and the laptop differ in `schedule`, `start`, `poll`, and `release`, so `ContainerBuilder` and `LaptopBuilder` share a base and differ only there.
- **One session script for both shapes,** in `cli.py`, not an entrypoint in the image, so the container and the laptop run the same steps and the image holds no logic.
- **The adapter reads the pushed commit from the remote** with `ls-remote`, rather than trusting a file the session writes, because the agent can write anything in its workspace.
- **`push_log` and `push_side_reference` are added methods** beyond the protocol, because the protocol has no carrier for the side reference write and base.py is not this card's to change. The workflow calls `push_log` before `release`.
- **The adapter, not the builder, writes the build's final log commit** (the answer to #56), carrying the builder's `actions.jsonl` over. The handoff text still asks the builder to push it; changing that text is #56's.
- **No cost on the trace root** and no shared `trace.scrubbed` helper: both are #55's changes to `trace/shape.py`, which this card does not touch. `scrub_record` here is the seam until then.
- **Effort is recorded but not passed** to the CLI, because this card could not confirm a stable command line flag for it.
- **The laptop token file is mode 640,** read by the session user through a shared group, where the container's is 600.
- **The plist has no comment and a placeholder user name,** because an XML comment needs two hyphens and the user name is a value supplied at install.
- **No live card was run.** A real container build, a real `docker run`, and a real `launchctl bootstrap` cannot run in the session that built this card; the pull request says so, and the operator runs the two live cards before review (KTD25).
