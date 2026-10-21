# U25 explainer: ownership, watch, and the verbs

This card is the other half of Phase 2's loop. U14 built the card workflow, the durable run that takes one claimed build from `claimed` to `scored`. This card builds what starts those runs and what every surface calls: the claim rule, the `dispatch` and `land` verbs, the composed status, and the watch cron that dispatches ready cards on its own. The MCP tools (U18), the page (U20), and the CLI (U21) are thin clients of the functions here.

## Words used below

- **MCP.** The Model Context Protocol, the tool protocol an agent calls Slipway through; U18 serves it.
- **Verb.** One action a person or an agent asks Slipway to take, as a function in `service/`. Every surface calls the same function, so a rule is written once (KTD23).
- **Claim.** The row in `builds` that says one build owns one card. `records.store.claim` writes it.
- **Ownership terminal set.** `scored`, `abandoned`, and `returned_empty`. A build in one of these no longer owns its card, so a new build may be claimed.
- **Run.** One execution of the card workflow on the engine (Hatchet), started through `service/engine.py`.
- **Cron.** A task the engine starts on a clock; watch runs every five minutes.
- **Audit row.** One row in the `audit` table: who asked (their OAuth subject, the stable user id in their token), which tool, the arguments, and the result, both with secrets scrubbed (KTD24).
- **Host link.** The pull request's page on the host, such as `https://github.com/<repository>/pull/<number>`.
- **Bucket.** The page group a state belongs to, from the `states` table: `being_built`, `waiting_to_land`, `needs_you`, or `done`.

## The claim rule

At most one build per card is outside the ownership terminal set (KTD3). The database enforces it with a partial unique index on `builds`, a uniqueness rule that applies only to the rows outside the terminal set, so the rule holds whoever calls `claim`, and when two callers race the database decides and the loser gets the winner's build back. Watch and the dispatch verb both go through it, and neither starts a run without a claim.

The run writes its own id onto the build in its first step, a compare and set on a null run id (U14's `attach_run`). That is why `dispatch` can start a run with no fear of starting two: a second run for the same build finds another id there and ends as a no op. It is also why a failed start is safe. The claim stays with no run id; watch sees it after one interval and starts the run again, and never claims the card a second time.

Two terminal states reopen only by hand: a card whose last build ended `returned_empty` or `abandoned` is dispatched again only through the dispatch verb with a reason. Watch leaves those cards alone, so a build that failed does not loop every five minutes. The new build gets a new branch name, since the workflow names the branch after the build id.

AE9 is the rule seen from outside: three dispatch calls in a minute for one card make one claim and one run, and the second and third calls return the first build and its state. Each call writes an audit row, so all three are on the record.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/service/verbs.py` | `dispatch` and `land`, the `Ports` they reach (records, engine, the two config files, the tracker, a clock), the `Records` interface, `PostgresRecords` over the real schema, and `real_ports` from `SLIPWAY_HOME`. Also `host_link`, `day_start`, and `other_lanes`, which status and watch share. |
| `src/slipway/service/status.py` | `compose(project)`: counts, the per card list, needs you entries, and the `spoken` sentence, from one snapshot of the records. `across(actor)`: `compose` over every project, for the operator alone. |
| `src/slipway/workflow/watch.py` | The five minute cron: one `cycle` per project, `run` over every project, and `register` to declare it on Hatchet. |
| `src/slipway/workflow/worker.py` | `vps_worker` now registers the watch cron beside the card workflow's tasks. |
| `tests/_service_fakes.py` | In memory records that keep the claim rule and read each state's bucket from the migration, a fake engine, a fake tracker, and `world()`, which puts the config fixtures under a temporary `SLIPWAY_HOME`. |
| `tests/unit/test_verbs.py` | The card's scenarios for `dispatch`, `land`, and `compose`, and the refusals around them. |
| `tests/unit/test_watch.py` | The card's scenarios for watch. |
| `tests/integration/test_service_records.py` | The same scenarios through `PostgresRecords` on a real Postgres, so the SQL the fakes stand in for runs in the gate too. |

## dispatch

`dispatch(ports, project, key, actor, reason)` reads the card from the tracker and then, in this order:

1. Returns the owning build when the card's latest build is outside the ownership terminal set. This comes first because the board moves a card out of the ready status once its branch exists, and a caller asking again still learns which build owns it.
2. Refuses a card that is not ready by the ready rule (`cards/contract.py`), naming the allowed labels. The rule is passed the lane labels of every other configured project, so a card labelled for two lanes is caught (the note card #47 carried onto this card from the review of #45).
3. Refuses a card whose last build ended `returned_empty` or `abandoned` when no reason is given.
4. Refuses when the project has claimed `daily_dispatch_cap` builds since midnight UTC, naming the cap.
5. Claims the card, carrying its declared paths and title onto the `cards` row, and starts the run.

Every call writes one audit row with the result. The result is a plain mapping with an `outcome` of `dispatched`, `owned`, or `refused`, so a surface branches on it without parsing text.

## land

`land(ports, project, key, actor, override, reason)` pushes `card.resume` into the card's run and never runs verify itself (KTD11):

| Call | Build | What happens |
|:-|:-|:-|
| no override | `halted` or `gate_red` | `card.resume` pushed: the run re verifies a halt, or re reads the pull request in `gate_red` |
| no override | any other state | nothing pushed; the state and the host link come back as `waiting` |
| override, no reason | any | refused and audited |
| override, reason, any subject but the operator's | any | refused with the halt and its host link, and audited |
| override, reason, the operator's subject | `halted` on `commit_mismatch` | `card.resume` with `override` pushed; nothing else written |
| override, reason, the operator's subject | anything else | refused and audited: an override lands a commit mismatch only |

A land that pushes writes no audit row here. Its record is the transition the run makes, which carries the actor, the reason, and both commits.

## compose and the spoken sentence

`compose` reads each card's latest build in one read only snapshot of the records, so the counts, the list, and the needs you entries agree with each other. A needs you entry carries the line that says why (the halt's own line, or the reason the transition recorded), what to do, and the host link. The `spoken` sentence names every non zero bucket and then reads out up to three needs you cards, such as:

> 3 being built, 1 waiting to land, 1 needs you. EX-5 is halted: commit mismatch: the merge is not the confirmed commit.

Nothing in `compose` calls a host or a tracker, so a status read is cheap enough to ask for by voice.

## watch

Each cycle, for each project:

1. Starts the run again for any claim with no run id untouched for more than five minutes.
2. Reads the ready cards from the tracker. An unreadable tracker ends the cycle for that project with its reason, and nothing is dispatched.
3. Skips a card whose latest build still owns it (an `awaiting_approval` card, for one) or ended `returned_empty` or `abandoned`.
4. Dispatches the rest with actor `watch`, writing one audit row each, until the daily cap stops it.

Halted builds are not watch's to touch: the hourly re verify is the card workflow's own sleep, raced against `card.resume` inside the run. A project whose file fails to load is reported in the cycle's result and the others carry on.

## Where this departs from the card, and why

- **Files outside the card's list.** `workflow/worker.py` gains three lines so the VPS worker registers the cron. `tests/_service_fakes.py` holds the fakes both test files share, as `tests/_card_fakes.py` does for U14. `tests/integration/test_service_records.py` runs the scenarios against Postgres, because `PostgresRecords` is new SQL the unit tests cannot reach.
- **The reopen rule lives in dispatch.** KTD3 says `returned_empty` and `abandoned` reopen only through the dispatch verb with a reason; the card's approach does not list it, so dispatch adds the refusal and watch skips those cards.
- **An override is checked against the halt too.** The workflow ignores an override on any halt but `commit_mismatch`, so the verb refuses it up front instead of pushing an event that would do nothing.
- **across is the operator's.** It shows every project's cards at once, so any other subject is refused, the same rule the page keeps (U20).
- **The daily cap counts claims since midnight UTC**, including watch's own, not a rolling day.
- **Audit and U18.** dispatch audits every call and land audits its refusals. U18's per tool audit should not write a second row for these two; it can pass its registered client name through the `client` argument instead.
