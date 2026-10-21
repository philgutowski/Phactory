# U14 explainer: the card workflow on Hatchet

This card is the loop at the centre of Phase 2. Every unit before it built a piece a card needs: the records (U7), the card contract and trackers (U8), the hosts and webhooks (U9), verify and the halt classes (U10), the builder contract and handoff (U11), and the CLI builder (U13). This card strings them into one durable run per build, from the moment watch or the dispatch verb claims a card to the moment the build is scored. U25 (ownership, watch, and the verbs) is the other half of Phase 2's loop and calls this one through `service/engine.py`.

## Words used below

- **Engine.** Hatchet, the server that keeps every run's progress in Postgres and hands tasks to workers (KTD2).
- **Worker.** A Python process that connects out to the engine and runs the tasks it registered.
- **Durable task.** A task whose progress the engine writes to a log, so after a crash or a redeploy the engine replays the log and the task carries on where it was.
- **Parent.** The durable task `card_workflow`, one run per build.
- **Child.** A task the parent starts and waits for. Its result is written to the parent's log once, so a replay reads the result instead of running the child again.
- **Replay.** The engine rerunning the parent's code from the top after a crash or a redeploy, feeding it each child's logged result. The parent's own lines run again; its children do not.
- **Concurrency key.** A value read from a child's input that the engine counts running children under. At `max_runs` N, N children with the same value run and the rest wait.
- **Durable wait.** The parent parked until an event or a sleep fires, holding no worker slot. An **or group** is a set of such conditions where the first one met ends the wait.
- **CEL.** The small expression language Hatchet evaluates against an input or an event payload, as in `input.project` or `input.build_id == 7`.
- **Claim before act.** A side effect step writes its claim column (`pr_number`, `session_ref`, and the rest) in the same transaction as the transition that follows it, and skips the call when the column is already set (KTD16).
- **Seam.** A named point in the workflow where an ordered list of steps runs, so a later unit adds work there without editing the workflow.
- **Needs you.** A state a person must act on: `awaiting_approval`, `gate_red`, `halted`, `returned_empty`, `abandoned`.
- **Operator run smoke.** A live test only the operator runs, because it needs an engine (KTD19).

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/workflow/card.py` | The parent body, every child body, the table of children with their keys and timeouts, and the ports (the records, config, tracker, host, builder, git, verify, and clock a child reaches). Imports nothing from the engine's SDK. |
| `src/slipway/workflow/events.py` | The kinds a wait listens for, the CEL filter that keeps another build's events out, how a wait's answer is read, and the poll path that turns a re read pull request into the event the host would have sent. |
| `src/slipway/workflow/post_landed.py` | The seam registry: `post_build`, `gate_observed`, `post_landed`, and how a later unit's `steps` module registers onto them without importing `workflow`. |
| `src/slipway/workflow/worker.py` | The Hatchet side: declares one task per child and the durable parent, adapts the engine's two primitives to `card.Engine`, and builds the real ports (`PostgresRecords`, the git cache, the adapters) from `SLIPWAY_HOME`. Also the VPS worker and the laptop worker. |
| `src/slipway/service/engine.py` | The engine client facade `service` calls: `start_card` and `push`. Names the workflow by its registered name, since `service` may not import `workflow` (KTD23). |
| `src/slipway/records/schema/004_workflow_edges.sql` | Seven allowed edges the workflow needs that 001 and 003 lacked (below). |
| `tests/_card_fakes.py` | The fakes both test files share: an in process engine that honours the concurrency keys, in memory records that refuse any edge the migrations do not allow, and a fake host, tracker, builder, git, and clock. |
| `tests/unit/test_card_workflow.py` | Every test scenario on the card, through the fakes. |
| `tests/integration/test_workflow_live.py` | Whole cards on the real records in Postgres (in the gate), and the operator run smoke through a local Hatchet. |

## The shape of one run

```
card_workflow(project, key, build_id)                     holds no key
  attach_run          write this run's id on the build, or exit (KTD3)
  build               key input.project, max runs input.cap
    (laptop_build)    on the laptop worker's label, for a worker_process binding
  post_build          when a session was started
  open                key input.repository, max runs 1
  loop until the build is scored, abandoned, or returned_empty:
    wait              any of seven event kinds for this build, or one hour
    on_wake           re read the pull request, then move
    verify            when merged
    post_landed       when landed
```

The parent does nothing but start children, wait, and branch on what they return, because a replay reruns its code (the U4 spike measured this). Every record, host call, and tracker read is in a child, and every child is safe to run twice, because a worker lost mid child gets its child handed to another worker even with no retries. The parent's execution timeout is thirty days: the engine counts wall time across durable waits, and the spike found the sixty second default ends one.

## The children

**attach_run** writes the run id onto the build with a compare and set on a null run id (KTD3). A second parent for the same build finds another id there and ends as a no op, which is how two starts for one build leave one live run. When the project's dispatched and building builds already fill its cap, it records `claimed` to `waiting_for_slot`, so the page can say why the card is not moving; the engine's key on `build` is what actually holds it.

**build** holds the project's key. Before dispatch it re reads the card through the whole ready rule, resolves the builder binding from the project and operator files as they are now (so an edit after the claim is seen), creates the branch on the host at the base commit (KTD27), and records `dispatched`. The branch is `slipway/<key>-<build id>`, one per build, so a reopened card gets a new name. Then it renders the handoff and starts the session. A `remote_session` builder is polled every thirty seconds until it is terminal or the role's budget runs out, counted from the `building` transition (KTD22). A `worker_process` binding instead runs `laptop_build` on the label the operator file gives the builder worker, and records `building` with the time the laptop picked the task up, so a card queued overnight is not expired before it starts.

When the session ends, the result is checked against the base (`check_base`) and against the host (`commit_on_remote`), and the card is read again: `returned` with the commit, `returned_empty` with its halt class (`builder_failed`, `builder_expired`, `binding_invalid`), or `abandoned` when the card was taken back.

**post_build** runs the `post_build` seam whenever a session was started, whatever `building` ended in.

**open** holds the repository's key, so two cards on one repository never open at once. It confirms the commit is still the branch head (when it is not, the build records `abandoned` with the reason, since no retry brings the head back, and the card can be claimed again), pins it in the git cache for verify (`gitread.hold`), opens the pull request with the tracker's closing reference, posts the deterministic check on the confirmed commit, arms auto merge when the project has it on, and records `pr_open` with `pr_number`, `check_run_id`, and `confirmed_commit` in one transaction.

**on_wake** handles one wake of the landing wait. Every move it makes follows a fresh read of the pull request from the host (KTD10), so an event is a wake up, never evidence:

| Wake | After the re read | Move |
|:-|:-|:-|
| `pr.closed` | closed and not merged | to `abandoned`; a forged event whose re read shows open changes nothing |
| `pr.merged` | merged | to `merged`, recording approvals, who merged, and whether auto merge was on (R19) |
| `pr.synchronize` | head is not the confirmed commit | a failing deterministic check on the new head; no move (AE12) |
| `queue.requested` | tested head is the confirmed commit | `tested_commit` recorded, a passing check on the tested commit, to `gate_running` |
| `queue.requested` | tested head is another commit | a failing check on the tested commit; no move |
| `pr.dequeued` | still open | to `gate_red`, with a failing landing check |
| `check.completed` | | no move |
| `card.resume` while halted | | re verify, or land on the operator's override |
| sleep | | the poll path: the re read becomes the event the host would have sent, written to `host_events` with source `poll`, then handled as above; a moved head already flagged on an earlier look is not flagged again; every sleep then runs the seven day deadline check |

`check.completed`, `queue.requested`, and `pr.dequeued` also reach the `gate_observed` seam.

The seven day deadline: a pull request still in `pr_open` seven days after it opened moves to `awaiting_approval` with a failing landing check, and the run keeps waiting. A later merge still lands it.

**verify** moves `merged` to `verifying` and calls `slipway.verify.verify` with the commits the records hold and the landing path the host reports. When the card has not moved yet it reads the tracker again every thirty seconds for up to ten minutes before it records `merged_card_not_moved` (KTD11). A landed verdict records `landed`; anything else records `halted` with the halt class, its needs you line, and its evidence, and posts a failing `slipway/landing` check with that line (R42). A halted build waits again: the hourly sleep re verifies, and a `card.resume` with an override, from the operator's subject only and with a reason, lands a `commit_mismatch` and records who, why, and both commits.

**post_landed** runs the `post_landed` seam and records `scored`.

## The two checks Slipway posts

- `slipway/deterministic` says one thing before the merge: the commit it sits on is the one Slipway confirmed, or a queue commit built from it (R18, KTD7). It passes on the confirmed head at open and on a tested commit built from it, and fails on a head pushed after open or a tested commit built from another head. The deterministic score U16 computes is a different thing and runs after landing.
- `slipway/landing` fails with the reason whenever the build enters a needs you state that keeps the run alive: `gate_red`, `halted`, and `awaiting_approval` after the deadline (KTD27).

## Claim before act, and the one place it is not enough

Most host calls are safe to repeat. Creating the branch at the same commit is a no op, and opening a pull request on a branch that has one returns it, so a crash between such a call and its record repeats the call harmlessly; the test for it opens no second pull request. Starting a builder session is not safe to repeat. So `build` writes an attempt row before it calls `start`, and a re executed step that finds an attempt with no `session_ref` recorded ends the build as `returned_empty` (`builder_failed`, "a session may have started before a crash") instead of starting a second one. The operator dispatches again with a reason. That trades a rare lost build for never paying for two sessions.

## The seams

Each seam is an ordered list of step callables. This card registers none, so all three are no ops. A later unit adds a `steps` module in its own package holding one line:

```python
SLIPWAY_STEPS = (("post_build", export_trace), ("post_build", commit_log), ("post_build", release_session))
```

`post_landed.load()` imports `slipway.trace.steps`, `slipway.evals.steps`, and `slipway.runner.steps` in that order, skipping any that does not exist yet, and registers their pairs as written. The registration is data rather than a call because the import rule lets nothing outside `workflow/` import `workflow` (KTD23). A step receives a `SeamContext` (the project, the build row, the ports, and the event that reached the seam) and returns structured fields, which land in the evidence of the next transition.

## Migration 004

Seven edges the state diagram implies and 001 and 003 did not carry:

- `claimed` and `waiting_for_slot` to `returned_empty`: a binding that does not resolve ends the build before the branch exists.
- `dispatched`, `building`, and `returned` to `abandoned`: the card is read again at every transition Slipway causes, and a card taken back is abandoned at the next one.
- `pr_open` and `awaiting_approval` to `merged`: on the auto merge paths no queue event ever comes, so the merge finds the build there.

## Where this departs from the card, and why

- **Timeouts are ceilings.** Hatchet declares execution and scheduling timeouts per task, not per run, so `build` and `open` carry a ceiling (twelve hours plus a margin for `build`), and the project's own role budget is the deadline the `build` child enforces itself. `laptop_build` may wait two days for a laptop.
- **The parent's execution timeout is thirty days**, not eight. Eight would end a run one day after the seven day deadline had moved it to needs you, which the card says must not end the run. A run past thirty days ends; the build row keeps its state for U25's watch to see.
- **The laptop's budget.** `laptop_build` blocks on the laptop until its session ends, and the engine's execution timeout is the only stop for a session that runs long there. The VPS records the start time the laptop reports.
- **Git credentials.** `worker.CacheGit` reads the project's repository over HTTPS from the host's own address and relies on the worker's git credential helper, which the deploy card sets up.

## Running the operator smoke

```sh
createdb -h 127.0.0.1 -p 54329 -U slipway hatchet
docker run -d --name hatchet-lite --network host \
  -e RABBITMQ_ENABLED=false \
  -e DATABASE_URL="postgresql://slipway@127.0.0.1:54329/hatchet?sslmode=disable" \
  -e SERVER_DEFAULT_ENGINE_VERSION=V1 -e SERVER_URL=http://localhost:8888 \
  -e SERVER_AUTH_COOKIE_DOMAIN=localhost -e SERVER_AUTH_COOKIE_INSECURE=t \
  -e SERVER_AUTH_SET_EMAIL_VERIFIED=t -e SERVER_GRPC_BIND_ADDRESS=127.0.0.1 \
  -e SERVER_GRPC_PORT=7077 -e SERVER_GRPC_BROADCAST_ADDRESS=localhost:7077 \
  -e SERVER_INTERNAL_CLIENT_INTERNAL_GRPC_BROADCAST_ADDRESS=localhost:7077 \
  -e SERVER_GRPC_INSECURE=t ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.110.5
export HATCHET_CLIENT_TOKEN=$(docker exec hatchet-lite /hatchet-admin token create --config /config | tail -n 1)
export HATCHET_CLIENT_TLS_STRATEGY=none HATCHET_CLIENT_HOST_PORT=localhost:7077 SLIPWAY_TEST_HATCHET=1
CI=true python -m pytest -q tests/integration/test_workflow_live.py
```

The smoke starts a worker subprocess with the real task declarations over a fake builder and host and the real records, starts one card through `HatchetEngine.start_card`, waits for `pr_open`, pushes `pr.merged` through `HatchetEngine.push`, and asserts the transition log from `claimed` to `scored`. Without `SLIPWAY_TEST_HATCHET` it skips, and `tests/_gate.py` lets that one skip stand under CI because the test is marked `operator_smoke`; every other skipped integration test still fails the gate.
