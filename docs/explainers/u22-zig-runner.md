# U22 explainer: Zig runner instances and tracking

Slipway's own gate, and any project that opts in, can run on the operator's own instances of the Zig actions runner (R36). Those instances track the runner repository's main branch: each new commit is built, tested, and rolled onto every idle instance within the hour, and rolled back off an instance when a gate on it fails for a reason of the runner's own (R37). This card builds that loop: the deploy scripts that build and move binaries, the containers and launchd job that keep the runner alive, the hourly cron that drives the scripts, and the rollback evaluator that observation (U23) calls.

## Words used below

- **Runner.** A program that takes a GitHub Actions job, runs its steps, and reports the result. The Zig actions runner is the operator's own runner, written in Zig.
- **Just in time runner.** A runner registration good for one job: the process registers, runs a single job, and exits. The runner works this way, one process per job and per repository.
- **Instance.** One supervisor on one host (the VPS or the laptop) that keeps one runner process alive per repository it serves, under a name prefix, with labels a workflow's `runs-on` names. The operator file lists each instance.
- **Supervisor.** The thing that restarts a process when it exits: compose's `restart: always` around `supervise.sh` on the VPS, launchd's keep alive on the laptop.
- **Build.** One runner commit compiled, kept under `builds/<commit>` on its host.
- **Swap.** Pointing an instance at another build. The binary a running job uses is never touched; the supervisor launches the new one after the job ends.
- **Compare and set.** A database update that applies only when the row still holds the value the caller read, so two writers racing on one row never both win.
- **Runner class finding.** A finding that blames the runner, not the project: an unsupported step, a gap in the runner's log sequence, a failed write to its database, or a conclusion that disagrees with the host's check (R39). A project's own red test is never one.
- **Sandbox container.** A throwaway container with no access to the host's files, its Docker socket, or any credential, used to build and test runner commits.

## The loop

```
hourly cron (runner_track.reconcile)
  read main's head  ->  rejected?  ->  stop until a newer head
  per host: build.sh, test.sh  ->  fail: runner_build_red finding, stay
  per instance: busy in /api/runners?  ->  wait for the next hour
                idle: swap.sh, then compare and set on runner_builds
gate completion (U23 observe, then rollback.evaluate)
  runner class finding on the active build, none on this project's last
  run on the previous build  ->  rollback.sh, row rejected, finding
```

`runner_builds` is the truth about which build each instance runs. Every swap closes the active row's interval and opens the new one at the same instant, so a gate run is attributed to exactly one build by its start time (KTD20, R38). A rollback marks the regressed row `rejected` and opens a new active row for the previous commit; the cron skips a commit any rejected row names, so it is not tried again until a newer head exists.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/runner/instances.py` | The ports the cron and the evaluator share: `Records` with `PostgresRecords` over `runner_builds` and `findings`; `Scripts` with `ShellScripts`, which runs the deploy scripts on the instance's host; `ReadApi` over the runner's `/api/runners`; `git_head` over `git ls-remote`; and `finding()`, which bounds and scrubs an excerpt before it is written (KTD24). |
| `src/slipway/runner/rollback.py` | `evaluate(ports, gate_run)`, the rollback evaluator, and `RUNNER_CLASSES`, the finding classes that count. |
| `src/slipway/workflow/runner_track.py` | `reconcile(ports)`, one tracking pass; `real_ports`, from the environment and the operator file; `register`, the hourly cron on Hatchet. |
| `src/slipway/workflow/worker.py` | `vps_worker` registers the cron when `SLIPWAY_RUNNER_SCRIPTS` is set. |
| `src/slipway/records/schema/005_runner_findings.sql` | Lets a finding belong to the deployment rather than a project, and adds `runner_commit`, `gate_run_id`, and `related_finding_id` to `findings`. |
| `deploy/runner/lib.sh` | What the scripts share: the root layout, argument checks, and `in_sandbox`, which pipes the source into a sandbox container as a tar archive and the build back out the same way, so the sandbox mounts nothing from the host. |
| `deploy/runner/build.sh` | Fetches a commit into a bare clone, reads the pinned Zig version from the repository's version file, builds in the sandbox, and renames the result into `builds/<commit>` only once complete. |
| `deploy/runner/test.sh` | Runs `zig build test` for a built commit, in the sandbox, with the same Zig. |
| `deploy/runner/swap.sh` | Moves the instance's `current` link to a build and keeps the old one as `previous`. |
| `deploy/runner/rollback.sh` | Moves `current` back to a named commit, or to whatever `previous` holds. |
| `deploy/runner/supervise.sh` | The restart loop: one loop per repository, each launching the binary `current` points at, with only that repository's token. When the link moves, a process still waiting for a job is asked to stop (TERM), so its next job runs on the build the records name. |
| `deploy/runner.Dockerfile` | The image for the VPS runner container and for the build sandbox. |
| `deploy/compose.yaml` | Adds the `zig-runner` service, the `dagger` service behind a profile, the builder network, and the builds volume; makes `HATCHET_GRPC_INSECURE` required. |
| `deploy/laptop/com.slipway.zig-runner.plist` | The launchd job for the laptop instance. |
| `tests/_runner_fakes.py` | In memory records with the same rules as Postgres, scripts and an API a test scripts, and a clock a test moves. |
| `tests/unit/test_runner_track.py` | The card's tracking scenarios, AE11 end to end, the real ports' parsing, and the link scripts run for real on a scratch directory. |
| `tests/unit/test_rollback.py` | The card's rollback scenarios with fixture gate runs. |
| `tests/integration/test_runner_records.py` | AE11, a build failure, and the compare and sets through `PostgresRecords` on a real Postgres. |

## The cron, step by step

`reconcile` reads the head of the default branch the operator file names. If a rejected row names that commit, it stops. Every instance already on the head is reported `current`. The rest are grouped by host, because a binary built on the VPS does not run on the laptop, and each host builds and tests once however many instances it serves.

A build or test failure writes one `runner_build_red` finding with no project (it belongs to the deployment, as `runner_builds` does), the host and commit in its fingerprint, and the end of the script's output as its excerpt. The next hour sees that fingerprint and does not build the commit on that host again; a newer head is built as usual.

For a host that passed, the cron asks the read API for every runner, right after the build so the answer is fresh, and, per instance, looks for a busy one whose name starts with the instance's prefix. A busy instance waits for the next hour. An idle one, whose processes are waiting or offline between jobs, is swapped: first `swap.sh`, then `activate`, the compare and set. If the API cannot be reached, nothing is swapped: an unreachable API never means idle.

If the compare and set loses (the evaluator rolled the instance back in the same moment), the cron runs `swap.sh` again for whatever build the records now say is active, so the binary always follows the record.

## The evaluator, step by step

`evaluate(ports, gate_run)` takes the `gate_runs` row U23 just wrote, after U23 wrote that run's findings with `gate_run_id` set. It answers with an outcome and changes nothing unless every test passes:

1. The run carries a runner class finding. Otherwise `no_runner_finding`: a project red changes nothing.
2. The run is attributed to a build (`unattributed` if not) that is still active or already rolled back. A build a normal swap replaced is `superseded`.
3. The build has a previous build on the instance (`no_previous` for the first build ever), and this project has a gate run on it (`no_baseline` if not, since there is nothing to compare).
4. That baseline run carries no runner class finding. If it does, the finding is not new, and the run is `not_counted`.

Then, if the build is active, `rollback.sh` points the instance at the previous commit and `roll_back` marks the row rejected and opens the restored one, and a `runner_regression` finding is written: `rolled_back`. The script runs first, so a script that fails leaves the records as they were and the engine retries the step whole. If the build was already rolled back, a second regression in the hour, there is no second swap; the new finding's `related_finding_id` points at the first rollback's finding: `already_rolled_back`.

Rejection is by commit, so the cron skips it on every instance; the rollback itself is per instance, because the evaluator compares one project's runs on one instance.

## Deploying an instance

The scripts live on each host at the path `SLIPWAY_RUNNER_SCRIPTS` names in the worker's environment. A host other than the worker's own is reached through the command prefix `SLIPWAY_RUNNER_REMOTES` gives it, as JSON such as `{"laptop": ["ssh", "runner@laptop"]}`. Each host also sets `SLIPWAY_RUNNER_ROOT` (default `~/slipway-runner`), `RUNNER_IMAGE` (the sandbox image), and, for the laptop, `SLIPWAY_RUNNER_TARGET` (for example `aarch64-macos`), since the Linux sandbox cross compiles its binary.

On the VPS, the worker runs the scripts with the `runner_builds` volume mounted at the same path the runner container mounts it read only, so the links resolve the same in both. The runner container reads its settings and tokens from its own environment file (`RUNNER_ENV_FILE`), which no other service receives (KTD28). It has no Docker socket and joins only the builder network (KTD25).

On the laptop, the plist runs `supervise.sh` under a dedicated user with its own home. Install it as `/Library/LaunchDaemons/com.slipway.zig-runner.plist` with `SLIPWAY_RUNNER_USER` replaced by that user's name, copy `deploy/runner/` to `~/slipway-runner/scripts` in that home, and put the settings `supervise.sh` lists in `~/.slipway-runner.env`, mode 600.

## What this card leaves to the operator

- How the runner treats TERM. `supervise.sh` sends it to a process launched before a swap, expecting the runner to exit at once when idle and after its job when busy; the smoke confirms it.
- The runner's own command line. Its README and `docs/SPEC.md` are not readable from this repository's sessions, so `supervise.sh` passes the settings as environment (`RUNNER_REPOSITORY`, `RUNNER_TOKEN`, `RUNNER_NAME_PREFIX`, `RUNNER_LABELS`, `DATABASE_URL`) plus `RUNNER_ARGS`, and the live smoke confirms or maps them. The same holds for the shape of `/api/runners`: `busy_under` accepts a `busy` flag or a `busy` status.
- The runner read API service. It is one per deployment and listens on the compose network only (KTD28); its command is the runner's to say, so it is added with the smoke.
- Flipping `SLIPWAY_RUNS_ON` to the instance's labels once the first instance runs Slipway's CI green (card step 5).
