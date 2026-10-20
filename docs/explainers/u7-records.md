# U7 explainer: the records store, transition integrity, migrations

This card builds Slipway's memory: the Postgres tables every later unit writes to and every surface reads from, the rules that keep a build's history honest even when two workers race, and the small command that creates and upgrades those tables. Nothing here talks to a host, a tracker, or the engine. It is the floor the workflow (U14), the verbs (U25), and the status page (U20) stand on.

## Words used below

- **Record.** A row Slipway writes about something that happened. The engine's own history is never the record (KTD16): if Hatchet's database were lost, Postgres alone would still say what every card did.
- **Build.** One attempt to build one card. A card can have several builds over its life (a re dispatch starts a new one), but only one at a time owns it.
- **State.** Where a build is right now, such as `building` or `pr_open`. **Transition.** One move from one state to another, written as a row with who did it and why.
- **Edge.** A pair (from state, to state) the state machine allows.
- **Ownership terminal set.** The states `scored`, `abandoned`, and `returned_empty`. A build in one of them no longer owns its card (KTD3).
- **Compare and set.** An update that only happens if a column still holds the value the caller expects, written as `UPDATE ... WHERE state = expected`. If someone else changed it first, zero rows update and the caller learns it lost.
- **Row lock.** Postgres locks a row while a transaction updates it; a second transaction updating the same row waits until the first commits.
- **Advisory lock.** A lock on a number rather than a row, which Postgres holds for you and never interprets. Slipway locks the number pair (project space, project id) so writes for one project take turns.
- **Partial unique index.** A uniqueness rule that only applies to rows matching a condition. Here: unique (project, card key) only among builds outside the ownership terminal set.
- **Claim column.** A column on `builds` that records a side effect has happened (a branch created, a pull request opened). A step writes it in the same transaction as its transition, and skips the side effect next time when the column is already set ("claim before act").
- **Migration.** A numbered SQL file that changes the schema. **Ledger.** The `schema_migrations` table, one row per applied file.
- **Forward only.** Migrations only ever add: an applied file is never edited, and a file is never applied below one already applied. A fix is a new, higher numbered file.
- **Checksum.** The SHA 256 of a file's bytes. If one byte changes, the checksum changes.
- **Schema (Postgres namespace).** A named folder of tables inside one database. The integration tests give each test its own.
- **Poll identity.** The text that stands in for a delivery id on an event the poll path found, built from what the event says, so the same state polled twice is recognised as one event.
- **Cursor.** The id of the last event a reader has seen. The next read asks for ids strictly greater than it.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/records/__init__.py` | Makes `records` a package. One docstring line. |
| `src/slipway/records/schema/001_init.sql` | Every table KTD16 names, the state list with its page bucket, the allowed edges, the partial unique index, the trigger that guards a build's state, and the unique keys that make writes idempotent. |
| `src/slipway/records/schema/002_poll_identity.sql` | Adds `host_events.poll_identity` and the unique key (project, poll identity), so a polled event seen on every poll cycle is one row (card #41). |
| `src/slipway/records/schema/003_abandon_edges.sql` | Adds the edges to `abandoned` from `claimed`, `waiting_for_slot`, `queued`, and `gate_running`, and replaces the check on `transitions` with one that pins `seq` 0 to the claim both ways (card #42). |
| `src/slipway/records/migrate.py` | Finds the numbered files, compares them with the ledger, applies the new ones, refuses a changed one or a lower numbered one that arrives late. Also runs as `python -m slipway.records.migrate DSN`. |
| `src/slipway/records/store.py` | The writes: `ensure_project`, `claim`, `attach_run`, `record_transition`, `ingest_host_event`, `activate_runner_build`, `record_gate_run`. |
| `src/slipway/records/queries.py` | The reads: `status_buckets`, `card_detail`, `events_since`, `in_flight_files`, `open_findings`, `stale_claims`, `latest_gate_run`, `runner_build_active_at`. |
| `tests/unit/test_migrate.py` | The ledger rules without a database: ordering, naming, duplicate versions, skip when applied, refuse when changed, the no transaction marker. |
| `tests/integration/test_records.py` | U7's test scenarios against the local Postgres. |
| `pyproject.toml` | Gains the one runtime dependency, `psycopg[binary]`, and ships the `.sql` files inside the package. |

## The record types

| Table | One row per | Notes |
|:-|:-|:-|
| `projects` | project | `slug` is unique. `events_floor` is the highest event id ever pruned (0 for now). |
| `cards` | card in a project | `paths` is the card's declared file list, NULL when it declared none. |
| `builds` | attempt to build a card | `state`, who claimed it and why, the engine `run_id`, the claim columns (`branch`, `session_ref`, `pr_number`, `check_run_id`, `trace_id`, `confirmed_commit`, `tested_commit`, `runner_commit`), and `last_seq`. |
| `transitions` | state change of a build | `seq` counts per build from 0 (the claim). `actor` and `evidence` (structured JSON) say who and why. |
| `attempts` | builder session | account, model, effort, cost, and tokens, so cost questions (R15) need only Postgres. |
| `host_events` | event from a code host | written by both the webhook endpoint and the poll path, deduplicated. |
| `scores` | score of a build | unique on (build, kind); the rubric result lives here, the Langfuse id is only a link. |
| `findings` | finding | open or closed, with an already redacted excerpt. |
| `runner_builds` | runner binary on an instance | active, previous, or rejected, with `activated_at` and `deactivated_at`. Not per project. |
| `gate_runs` | gate job on a host | unique on (host, host run id); `runner_build_id` says which runner binary ran it. |
| `audit` | MCP tool call | keyed by project slug text, no foreign key, so it outlives a deleted project. |
| `states`, `allowed_edges` | state, edge | the state machine as data. |
| `schema_migrations` | applied migration file | created by `migrate.py`, not by `001`. |

Every project table has a foreign key to `projects` with `ON DELETE CASCADE`, so deleting a project removes its records in one statement. Nothing deletes a project in this version; the format is ready for the command when it comes.

## A card's record transitions

Each arrow is one allowed edge, one row in `allowed_edges`. Each move writes one `transitions` row with the next `seq`. The claim writes `seq` 0 with no from state.

```
 claim (seq 0)
     |
     v
  claimed ======================> waiting_for_slot
     |                                   |
     +============> dispatched <=========+
                     |      \
                     v       \
                  building ===+==> returned_empty   (terminal: card free again)
                     |
                     v
                  returned
                     |
                     v
                  pr_open ==========================+
                     |                              |
                     v                              v
            awaiting_approval =================> abandoned  (terminal)
                     |                              ^
                     v                              |
                   queued <========= gate_red ======+
                     |                 ^
                     v                 |
                gate_running ==========+
                     |
                     v
                   merged
                     |
                     v
                 verifying <======> halted
                     |
                     v
                   landed
                     |
                     v
                   scored                           (terminal)
```

Since migration 003 four more edges lead to `abandoned`, left out of the drawing to keep it legible: from `claimed` and `waiting_for_slot` (the card stopped being ready before dispatch) and from `queued` and `gate_running` (the pull request was closed while it waited on the merge queue or its gate).

## How transition integrity is enforced

Four guards, three of them in the database itself, so a bug in Python, or a person typing SQL by hand, still cannot write a history that never happened.

1. **The edge must exist.** `transitions(from_state, to_state)` has a composite foreign key to `allowed_edges`. A row naming `claimed -> merged` fails with a foreign key violation. The claim row is the only one with a NULL from state (a NULL skips a foreign key), and a check constraint pins that row both ways: `seq` 0 is the claim, with a NULL from state and `to_state = 'claimed'`, and every row after it names a from state. (Before migration 003 the check only ran one way, so a `seq` 0 row could carry a from state.)
2. **The build's state can only move along an edge.** A trigger on `builds` refuses any `UPDATE` that changes `state` to a pair not in `allowed_edges`. So even an update that skips `record_transition` cannot jump a build forward.
3. **Compare and set on the from state, and on the seq when the caller knows it.** `record_transition(build, from_state, to_state, actor, evidence, claims, expected_seq)` runs `UPDATE builds SET state = to ... WHERE id = build AND state = from`, plus `AND last_seq = expected_seq` when `expected_seq` is given. If the build already left `from_state`, as when the engine replays a step after a crash, zero rows update, nothing is written, and the result says already past with the build's current state. That is how "the same transition recorded twice yields one row" holds.

   The from state alone is not enough where the machine has a cycle: `gate_red -> queued -> gate_running -> gate_red`, and `verifying <-> halted`. Picture a worker that records `gate_running -> gate_red` in lap one and crashes before reporting back. The host queues the build again, it reaches `gate_running` a second time, and then the engine replays the lap one step. The build is in `gate_running` again, so a check on the from state alone passes and writes a `gate_red` the host never reported. `expected_seq` is the build's `last_seq` as the caller saw it when it chose the move, which is the seq of the transition that brought the build into `from_state`. Every transition adds one to `last_seq`, so a build that went round the cycle and came back has a higher `last_seq` than the replay remembers, and the replay is already past. Leaving `expected_seq` out keeps the from state only check, for a caller that does not know the seq. The workflow (U14) is to pass it wherever it does.
4. **seq under the row lock, ids under the project lock.** The same `UPDATE` adds one to `last_seq` and returns it; the row lock it holds means no other writer can take the same number. Before that, the function takes the project's advisory lock, held until commit. So within one project only one transaction at a time inserts a `transitions` row, ids are assigned and committed in the same order, and a reader asking `events_since(project, cursor)` for ids greater than its cursor can never miss a row that committed late with a smaller id.

Claim columns ride in the same transaction: `claims={"pr_number": 7}` sets `builds.pr_number` in the same `UPDATE` as the state, so "PR opened" and "state is pr_open" commit together or not at all. Only the columns in `CLAIM_COLUMNS` are accepted.

`claim(project, key, actor, reason)` follows the same lock order (project lock, then rows). It inserts the build with `ON CONFLICT ... DO NOTHING` against the partial unique index. If another live build owns the card, the insert does nothing and the function returns that build's id and state with `won` false, which is what the losing caller reports (AE9). Two callers racing from two connections: the second waits on the project lock, then sees the winner's committed row.

`attach_run(build, run_id)` is a compare and set on `run_id IS NULL`: the first run id stays, a second call gets the existing one back and exits as a no op.

`ingest_host_event(source, normalised)` is the single entry for the webhook endpoint and the poll path. It stores the normalised mapping as canonical JSON text (sorted keys, no spaces) so the same event from either source is a byte equal row apart from `source`. Two unique keys dedupe: (host, delivery id) for webhook retries, (project, pull request, kind, merge commit) so a merge seen first by webhook and later by poll is one row, and (project, poll identity) so a polled event seen again on the next cycle is one row.

**The rule for polled events (card #41), which the poll path (U10) inherits.** A polled event has no delivery id, and a polled `pr.synchronize` or `pr.closed` has no merge commit. Postgres treats NULLs as distinct in a unique key, so without a third key every poll cycle inserted a fresh row and woke the workflow again. The poll path does not mint an identity itself: it calls `ingest_host_event("poll", normalised)` with `delivery_id` None, and the function sets `poll_identity` to `poll:` followed by the canonical JSON of `POLL_IDENTITY_FIELDS` (host, repository, pull request, kind, head commit, merge commit, tested commit, tested head, check name, check state). Two polls that see one pull request at one head with one outcome share it and write one row; a new head, a merge, or a check that finished another way is a new identity, a new row, and a new wake up. A webhook row leaves it NULL, because its delivery id already dedupes it. One consequence to know: a pull request pushed back to a head it had before is not a new polled row, so a wait that needs that wake up relies on its own re read of the pull request, which every event driven transition already does (KTD10). And a poll that finds a state a webhook already delivered is still its own row, since the two rows share neither a delivery id nor a poll identity; that is one extra wake up, never two per cycle.

Runner builds: `activate_runner_build(instance, commit, at)` closes the active row at `at` and opens the new one at the same instant, so the intervals meet with no gap. `record_gate_run` stores the runner build whose interval contains the job's start, so a job that started a minute before a swap is attributed to the old binary even if it finished after.

## How migrations run

```
apply(conn, database="slipway")
  |
  +== current_database() is not the expected name?  -> refuse (WrongDatabase)
  +== take a session advisory lock (one migrator at a time)
  +== CREATE TABLE IF NOT EXISTS schema_migrations
  +== read ledger {version: checksum}
  +== for each schema/NNN_name.sql in number order:
  |      in ledger, same checksum      -> skip
  |      in ledger, other checksum     -> refuse (ChecksumMismatch, both checksums named)
  |      not in ledger, below the      -> refuse (OutOfOrder, both versions named)
  |      ledger's highest version
  |      not in ledger, normal file    -> BEGIN; SET LOCAL lock_timeout = '5s';
  |                                       run file; insert ledger row; COMMIT
  |      not in ledger, first line is  -> run each statement on its own (no transaction,
  |      /* slipway: no transaction */   which CREATE INDEX CONCURRENTLY needs), then
  |                                       insert ledger row
  +== release the advisory lock
```

A file that fails part way rolls back whole, ledger row included, so the next run tries it again.

A no transaction file is the exception: each statement commits on its own, so when the second fails the first stays applied and no ledger row is written. The next run starts that file from the top. That is safe only because `discover()` refuses such a file unless every statement says `IF NOT EXISTS`, so the rerun skips what already landed. One trap remains: a `CREATE INDEX CONCURRENTLY` that fails leaves an invalid index (one Postgres keeps but never uses) under its name, which `IF NOT EXISTS` would then skip, so drop it by hand before the rerun.

Migrations are forward only in order as well as in content (card #42). Say 003 is applied, and a branch that added 002 merges afterwards. Without a guard the next run would apply 002 after 003, an order no other database ever saw. `pending()` refuses it instead, naming both versions, and the fix is to renumber the late file above the highest applied one. Versions compare as numbers, so 10 is above 9. A changed file that was already applied is refused rather than silently ignored: migrations are forward only, and a fix is a new numbered file. SQL comments in the schema are block comments, because a line comment would put two hyphens in the file.

## How migrations and the store are tested

- `tests/unit/test_migrate.py` drives `discover` and `pending` over small files in a temporary directory: order, bad names, duplicate versions, skip when applied, refuse when changed (the message names both checksums), the marker, a no transaction statement without `IF NOT EXISTS`, and a lower numbered file pending below the ledger's highest (the message names both versions).
- The integration suite repeats the last two against Postgres: a no transaction file that fails on its second statement keeps its first and writes no ledger row, and the fixed file then reruns from the top; a `002` arriving after `001` and `003` are applied is refused and creates nothing.
- The abandon test walks a build to each of `claimed`, `waiting_for_slot`, `queued`, and `gate_running`, records the move to `abandoned`, and claims the card again. The claim row test inserts a `seq` 0 row with a from state and a later row without one, and expects both refused.
- `tests/integration/test_records.py` reads `SLIPWAY_TEST_DSN`. Each test creates a fresh Postgres schema with a random name, points its connections' `search_path` at it, runs `migrate.apply`, and drops the schema at the end, so tests share one database but no rows. Without the variable each test skips, and under `CI` `tests/_gate.py` turns that skip into a failure.
- The cycle replay test walks a build round each cycle (the red gate and the halt), replays the lap one step with the seq it saw then, and checks that nothing is written, while a lap two step naming the current seq still goes through.
- The race tests use real concurrency: the claim test holds one connection's transaction open while a second connection's claim runs in a thread and waits; the cursor test runs two writer threads on two connections while the main thread reads by cursor, then checks the ids it saw equal every id in the table, once each, in order.
- The poll identity tests ingest the same polled `pr.synchronize` twice and expect one row, check that a new head, another check name, and another check state each make a new row, and check that the identity is per project and NULL on a webhook row.
- The test connections ask for `client_encoding=utf8`, because a database created by `initdb` under the C locale is SQL_ASCII and psycopg would otherwise hand back bytes instead of text. Production does not rely on that: `deploy/README.md` says the `slipway` database is created with UTF8 encoding.
- The byte equal test needs a second schema for the poll side. Its name is random per run, like the per test schema, and it is dropped in a `finally`, so an interrupted run cannot leave a fixed name behind for the next run to trip over.

## Where this differs from the plan

- **`record_transition` takes the expected from state as an argument, and optionally the expected seq.** The plan's signature is `(build, to_state, actor, evidence, claims)`. The compare and set needs an expected from state, and reading it from the build inside the call would make a replay look like a fresh move from wherever the build is now, so the caller (the workflow step) names it. `expected_seq` came out of the review of #35 (issue #40), so a replayed step cannot re enter a cycle.
- **The claim is `seq` 0.** The test scenario says `dispatched` then `building` are `seq` 1 and 2, and the claim still belongs in the event stream, so the claim row is `seq` 0 with a NULL from state.
- **A trigger on `builds` in addition to the foreign key on `transitions`**, so the build row itself cannot skip the state machine.
- **One extra edge, `dispatched -> returned_empty`,** for a provider error at session start (KTD22). The state diagram only shows `building -> returned_empty`.
- **`landed` and `scored` are in a fifth page bucket, `done`.** The plan's bucket list leaves them out.
- **The second host event key only dedupes events that carry a merge commit.** Events without one (opened, synchronize) compare as distinct under it. A webhook event dedupes by delivery id; a polled one, since card #41, by its poll identity (migration 002).
- **The events floor is a column, `projects.events_floor`.** Nothing prunes yet, so `truncated` is false until a pruning step raises the floor.
- **Writes for `attempts`, `scores`, `findings`, and `audit` are not in `store.py`.** Their tables exist; the units that produce those records (U12, U16, U23, U18) add the writes.
- **The database guard is a name argument.** `apply(conn, database=...)` refuses any other database; the command line defaults it to `slipway`. Reading the connection string from configuration is U6's.
- **`records/__init__.py` is an extra file**, needed for the package, and `pyproject.toml` gains `psycopg[binary]` and the package data line for the `.sql` files.
- **Four edges to `abandoned` the first version of the diagram lacked** (card #42): from `claimed`, `waiting_for_slot`, `queued`, and `gate_running`. The plan's diagram now shows them too.
- **`stale_claims` is an extra query** for KTD3's "a claimed row with no run id older than one watch interval is retried by watch".
