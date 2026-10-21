# U4 spike: Hatchet durability

Status: steps 2 to 7 proved on 2026-10-21 against a local engine; step 1 (sizing on the VPS itself) is open for the operator. Nothing disproved a settled decision, so U14 starts as planned on Hatchet.
Unit: U4 in `docs/plans/2026-10-14-1730-feat-slipway-first-version-plan.md`.
Source of engine facts: `docs/research/2026-10-14-plan/framework-docs-engines.md`, Hatchet section.
Consumer: U14 (the card workflow) follows `docs/spikes/hatchet/task.py` and the rules under "What U14 must do" below; U22 extends `deploy/compose.yaml`.

## Terms used below

- VPS: the virtual private server, the one rented machine Slipway's services run on.
- Engine: the Hatchet server, which stores every run in Postgres and hands tasks to workers. `hatchet-lite` is the image that bundles the engine, its API, and its dashboard in one container.
- Worker: a Python process that connects out to the engine over gRPC (a binary request protocol over HTTP/2) and runs the tasks it registered.
- Durable task: a task whose progress the engine writes to a log, so after a crash or a redeploy the engine replays the log and the task carries on from where it was, instead of starting over.
- Child task: a task a durable task starts and waits for. Its result is written to the durable log once, so a replay reads the result instead of running the child again.
- Concurrency key: a value computed from a task's input by a CEL expression (a small expression language Hatchet evaluates) that the engine counts running tasks under. A key at `max_runs` N lets N tasks with the same value run and holds the rest in a queue.
- Durable wait: `ctx.aio_wait_for`, which parks a durable task until an event or a sleep fires. An or group is a set of conditions where the first one met ends the wait.
- HMAC: a signature made by hashing the body with a shared secret; GitHub sends one in `X-Hub-Signature-256` so the receiver can tell the body came from a holder of the secret.
- SIGTERM and SIGKILL: the two signals that stop a process. SIGTERM asks it to stop and lets it clean up first; SIGKILL ends it at once, the way a crash or a lost machine would.
- Loopback: the address 127.0.0.1, which only programs on the same machine can reach.
- Eviction: the engine taking a parked durable task off its worker to free the slot, and replaying it on whichever worker is up when the wait ends.

## Where it ran

| Item | Value |
|:-|:-|
| Host | A Claude Code cloud container with Docker, not the VPS. Every fact below is about the engine and the SDK, which do not change with the host. Sizing on the VPS is step 1, still open. |
| Engine | `ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.110.5` (the `latest` tag on 2026-10-21, same image digest `sha256:cc81f078…`), above the 0.106.0 floor that expression based `max_runs` needs. |
| Postgres | `postgres:15.6`, Postgres as the message queue too (`RABBITMQ_ENABLED=false`). |
| SDK | `hatchet-sdk` 1.42.1 on Python 3.12. |
| Code | `docs/spikes/hatchet/task.py` (the durable task U14 follows), `drive.py` (starts runs and pushes events), `timeouts.py` (step 6 probes). |

## The seven facts

| Step | Fact | Result |
|:-|:-|:-|
| 1 | RAM, CPU at idle, published ports on the VPS | **Operator, open.** In the container after the runs below: `hatchet-lite` 52 MiB and 3% of one core, Postgres 363 MiB. The VPS numbers go here once `deploy/compose.yaml` is up there. Published ports in the seed: gRPC 7077 public, dashboard and API 8888 on loopback only, Postgres none. |
| 2 | Per input concurrency keys on children | **Proved.** `build` at `input.project` with `max_runs="input.cap"`: two `alpha` cards at cap 1 ran one at a time (a2's build started the instant a1's ended), three `beta` cards at cap 2 ran two at a time, in the same run of the spike. `open` at `input.repository` at 1 never ran two on one repository. |
| 3 | A waiting parent holds no key | **Proved.** Card g1 entered `open` on repository `shared` for 60 seconds; card g2 on the same repository and the same project at cap 1 started 11 seconds later. g2's `build` ran at once, so g1's parent held no project key while g1 was in its second child. g2's `open` was held until g1's `open` ended (to the tenth of a second), and then ran to completion while g1's parent sat in its wait. |
| 4 | A second run with the input key of a running one | **Not deduplicated.** A second parent with key `b1` ran both children again and parked in its own wait; one `pr.merged` event for `b1` woke both parents. KTD3 already assumes this and guards with `claim` and `attach_run`; nothing changes. SDK 1.42.1 also offers `idempotency=` on a task (a key expression with a TTL or a status rule); the plan does not rely on it. |
| 5 | Survives engine restart, worker restart, and redeploy while waiting; and redeploy mid child | **Proved.** With nine parents parked: `docker compose restart hatchet-lite`, then the worker stopped and started, then a worker started with `SPIKE_VARIANT=v2` (the trivial code change). Pushing `pr.merged` for a1 and g1 through the SDK (`drive.py merge`, the call Slipway's own endpoint makes, in place of the plan's tiny HTTP endpoint; step 7 covers an HTTP delivery) woke each exactly once, under v2, and both runs completed. Mid child: a worker stopped with SIGTERM during a 30 second `build` drained it first (the child finished, then the worker exited). A worker killed with SIGKILL 10 seconds into `build` lost the child; the engine handed it to the next worker 39 seconds later as attempt 1, although the task declares `retries=0`, and the run completed. |
| 6 | Raised scheduling and execution timeouts | **Proved.** A task requiring the worker label `laptop=yes` with `schedule_timeout` of 30 minutes stayed queued with no such worker past the five minute default, and ran the moment a labelled worker connected (times below). A task with `execution_timeout` of 5 minutes ran 90 seconds, past the 60 second default, and completed. |
| 7 | Hatchet's inbound webhook route on the self hosted engine | **Works.** A `GITHUB` source webhook with HMAC SHA256 on `X-Hub-Signature-256`, created through the SDK, accepted a correctly signed POST at `/api/v1/stable/tenants/<tenant>/webhooks/github`, turned it into event `pr.merged` by a CEL expression, and woke the waiting parent. A POST signed with the wrong secret got 403 "invalid HMAC signature" and woke nothing. Slipway's own endpoint stays primary all the same (KTD10): the route lives on the API port, which the seed keeps off the internet, and an event is a wake up, never evidence. |

Step 6 times, for the record: the labelled task was started at 15:53:13 UTC and was still queued at 15:59:34, six minutes and twenty one seconds later. The labelled worker connected at 15:59:37 and the task ran at 15:59:40 and completed.

## Two findings the research did not have

1. **The 60 second default execution timeout ends a durable wait.** A durable task left at the default that slept durably for two minutes was failed by the engine at exactly 60 seconds. The research marked this unknown. So the card workflow's parent must declare an `execution_timeout` longer than its longest wait; `task.py` uses eight days, one more than U14's seven day deadline step.
2. **A replay reruns the parent's own code, but not its children.** After the redeploy, each woken parent logged its "start" and "child done" lines again under v2, while no child started again; their results came from the durable log. Anything the parent does outside a child runs once per replay.

## What U14 must do

- Give the durable parent an `execution_timeout` past the seven day deadline (eight days in `task.py`) and a `schedule_timeout` of its own.
- Keep every side effect out of the parent body: transitions, host calls, and records go in children or behind a claim. The parent only starts children, waits, and branches on their results. This is plan step 7 ("keep the parent tiny"), now with the reason measured.
- Treat every child as able to run twice. A worker lost mid child gets the child handed to another worker even at `retries=0`, so the claim before act guards (KTD3) are what keep a second session or a second pull request from appearing.
- Expect one event to wake every parent waiting on a matching filter, and two parents for one build to both run unless `attach_run` stops the second.
- Stop the worker with SIGTERM for a deploy. It drains running children first.
- Pin the engine image by tag (`v0.110.5`), as the seed does.

## The seed compose file

`deploy/compose.yaml` holds Hatchet and its Postgres only, with every secret taken from the environment file KTD28 names. Two things the spike learned are in it:

- The built in dashboard account (`admin@example.com` with a published password) is replaced on first boot by `ADMIN_EMAIL` and `ADMIN_PASSWORD`. The `SEED_` prefixed names from older docs are ignored; a fresh stack with them set still accepted the built in login, and one with the two plain names refused it and accepted the new one.
- The API and dashboard port binds to loopback. The gRPC port is public for the laptop worker, and TLS on it is left to the deploy card, as `deploy/README.md` already says.
