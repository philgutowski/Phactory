# U21 explainer: the CLI, first slice

The `slipway` command is the operator's terminal. It is the third surface after the status page (U20) and, soon, the MCP tools (U18), and like them it holds no rule of its own: each verb parses its arguments, calls one service function with the operator's subject as the actor, and prints the result. This slice carries `onboard`, `dispatch`, `status`, `land`, `watch`, and `migrate`. The units that build `eval`, `trace`, `findings`, and `new` add one subcommand each, with this card's test pattern.

## Words used below

- **Surface.** A front end a person or an agent reaches Slipway through: the CLI, the page, or the MCP tools. A surface imports `service`, `config`, `auth`, and `onboard` only (KTD23).
- **MCP.** The Model Context Protocol, the tool protocol an agent calls Slipway through (U18).
- **Verb.** One action a surface asks the service layer to take, as one function in `service/`.
- **Subject.** The stable user id an identity provider puts in a token. The operator's is `operator_subject` in the operator file.
- **Actor.** The subject a verb records as the one who asked: in an audit row, a claim, or the `card.resume` payload.
- **Audit row.** One row in the `audit` table: the project, the subject, the client, the tool, the arguments, and the result, with secrets scrubbed (KTD24).
- **Client.** The name of the program that made a call. The CLI writes `slipway-cli`, so a reader of the audit table can tell the terminal from an agent.
- **Transition.** One row the card workflow writes each time a build changes state, with the actor and the evidence.
- **Cursor.** The id of the last transition a reader has seen, so the next read starts after it.
- **Entry point.** The function the `slipway` console script runs: `slipway.cli:main`, declared under `[project.scripts]` in `pyproject.toml`.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/cli.py` | The argument parser, one function per verb, the printers, and `main(argv, wiring)`. `Wiring` holds what the verbs reach: the config root, the service ports, onboard's ports, and the input, output, and sleep callables. `real_wiring` builds them from `SLIPWAY_HOME` and `SLIPWAY_DSN`. |
| `src/slipway/service/operate.py` | The three service functions the CLI needed that did not exist yet: `status` (compose or across, with an audit row), `events` and `tail` (transitions after a cursor), and `migrate`. |
| `src/slipway/service/verbs.py` | `Records` and `PostgresRecords` gain `events_since` (over `records.queries.events_since`) and `migrate` (over `records.migrate.apply`). |
| `src/slipway/onboard/terminal.py` | `real_ports(slug, root)` is split out of `main`, so the CLI's `onboard` verb builds onboard's ports the same way the terminal module does. |
| `pyproject.toml` | Declares the `slipway` console script. |
| `tests/unit/test_cli.py` | The card's scenarios through `cli.main` on `_service_fakes`' world. |
| `tests/_service_fakes.py` | The fake records answer `events_since` and `migrate`. |
| `tests/integration/test_cli_records.py` | `watch`, `status`, and `migrate` through `PostgresRecords`, so the new SQL runs in the gate. |

## How one verb runs

```
slipway land example EX-5 --override --reason "checked by hand"
   |
   cli.main: parse, load the operator file, actor = operator_subject
   |
   service.verbs.land(ports, "example", "EX-5", actor, True, "checked by hand", client="slipway-cli")
   |
   cli.show_land: print the outcome, exit 0 or 1
```

`main` loads the operator file first, so a missing or broken file stops every verb with exit 2 before anything else is read. The service ports are built only when a verb needs them, so `onboard` runs with no database named.

| Verb | Service call | Prints |
|:-|:-|:-|
| `onboard <slug>` | `onboard.terminal.run`, over U19's state machine | the questions, one at a time |
| `dispatch <project> <key> [--reason]` | `verbs.dispatch` | the build and its run, the owning build, or the refusal with the allowed labels |
| `status [<project>]` | `operate.status`: `status.compose`, or `status.across` with no project | the spoken sentence, then the page's five groups |
| `land <project> <key> [--override --reason]` | `verbs.land` | the build asked to land, why nothing was pushed, or the refusal with the halt's line and host link |
| `watch <project> [--since] [--limit] [--follow] [--every]` | `operate.tail`, then `operate.events` each `--every` seconds with `--follow` | one line per transition, until Ctrl C |
| `migrate [--database]` | `operate.migrate`, over `records.migrate.apply` | the versions applied |

`--json`, given before the verb, prints the service's result as JSON instead, with the same exit code.

## The actor, every time

The terminal is the operator's, so the actor is always the operator file's `operator_subject`. Where it lands:

- `dispatch` writes one audit row per call, and the claim carries the actor.
- `land` writes an audit row when it refuses; when it pushes, `card.resume` carries the actor, and the run's transition records it.
- `onboard` passes the actor to every call of U19's machine, which audits each start and answer.
- `status`, `watch`, and `migrate` each write one audit row. `watch --follow` writes one for the first read, not one per poll. A status across every project and a migration are written under the project `*`, since an audit row needs a project.

A refused `migrate` writes no row, since the audit table may be what failed to arrive.

## status shows the page's groups

`status` prints what the page draws from the same `compose` result, in the page's order and with its titles: Needs you, Being built, Waiting to land, Landed today, and Open findings, each with its count and the page's line for an empty group. A needs you entry carries its line and what to do; a landed entry carries both scores. The test renders the page for a fixture project with a card in every state and checks that each group holds the same keys in both.

## Exit codes

| Code | When |
|:-|:-|
| 0 | the verb did its work, or found nothing to do (`owned`, `waiting`) |
| 1 | the service refused it, or the card has no build |
| 2 | a usage or configuration error: an unknown project, no `SLIPWAY_DSN`, an unreachable database, a broken config file |

## Where this departs from the card, and why

- **Files outside the card's list.** `cli.py` may import only `service`, `config`, `auth`, and `onboard` (KTD23), and the events read and the migration lived under `records`, so `service/operate.py` and the two `Records` methods give the CLI a service function for each. `terminal.real_ports` is the same construction the terminal module already had, split out so the CLI does not copy it. `pyproject.toml` gains the console script the card's "through the entry point" names. The integration test covers the new SQL wrappers.
- **Reads are audited too.** The card asks that every command record the operator's subject as the actor. `status` and `watch` change nothing, so the record is an audit row naming the actor and the client; there is no other place a read could record it.
- **`status` checks who asks.** `operate.status` and `operate.tail` refuse a subject that is neither the operator nor on the project's `allowed_subjects`. The CLI always passes the operator, so this guards the MCP tools that will call the same functions.
- **`watch` reads transitions**, the stream `records.queries.events_since` already serves, and polls; it is not the five minute watch cron (U25), which shares the name.
