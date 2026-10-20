# U11 explainer: builder contract, handoff, trace shape

Slipway does not write code. A builder does: a Claude Managed Agents session (U12) or a headless Claude Code CLI session in a container or on the laptop (U13). This card writes the contract both of those adapters implement, the one document every builder receives, the result a builder returns, and the trace shape every adapter turns its session log into. It builds no adapter.

## Words used below

- **Builder.** Whatever writes the code for a card: a hosted agent session or a CLI session.
- **Adapter.** The class that drives one kind of builder behind a shared set of methods, so the card workflow never knows which builder it holds.
- **Primitive.** One of the six things any builder needs: harness, environment, tools, skills, credentials, schedule (R11).
- **Carrier.** The one method or field that delivers a primitive to the builder.
- **Protocol.** A Python `typing.Protocol`: a class that lists the methods an object must have, checked by shape rather than by inheritance, so an adapter never imports a base class it must subclass.
- **Session reference.** The adapter's own name for a running session, such as a provider session id or a container name. The workflow stores it and passes it back.
- **Handoff.** The one message that starts a build. Every builder gets the same fixed shape.
- **In flight files.** The files other builds of the same project are editing right now, which this card must not touch.
- **Machine bound test.** A test that needs hardware a remote builder lacks, such as a USB device. The handoff names them so a remote builder never tries to run them (R16).
- **Side reference.** A git reference outside `refs/heads`, so it is not a branch. Slipway keeps each card's action log on one.
- **Action log.** The list of what a builder did: tool calls, file changes, commands, and outside connections.
- **Trace.** The record of one build as OpenTelemetry spans.
- **OpenTelemetry, span, event, attribute.** OpenTelemetry is the standard wire format for traces. A span is one timed operation, an event is one instant inside a span, and an attribute is a key and value pair on either.
- **OTLP.** OpenTelemetry's own protocol. This card writes its JSON form, which Langfuse accepts (U15 sends it).
- **GenAI semantic conventions.** OpenTelemetry's agreed attribute names for model calls and tool calls, such as `gen_ai.request.model` and `gen_ai.usage.input_tokens`, so any OpenTelemetry viewer reads the spans without a Slipway plugin.
- **Shape record.** One of the five small types an adapter's mapper produces from a provider's log: `ModelRequest`, `ToolCall`, `FileChange`, `Command`, `Connection`.
- **JSON lines.** A text file with one JSON object per line.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/builders/base.py` | The `Builder` protocol, the primitive table (in the docstring and as `PRIMITIVES`), `Schedule`, `Credential`, `BuilderResult`, `SessionStatus`, and `check_base`, the base validation. |
| `src/slipway/builders/handoff.py` | `render(project, card, base, branch, in_flight, rules, closing)` returns a `Handoff`, the fixed document plus the fields an adapter needs. `in_flight_files` computes the in flight list. `TOOLS` is the fixed tool list. |
| `src/slipway/trace/shape.py` | The five shape records, `Trace` (the span builder), the OTLP JSON writer, JSON lines read and write, and the side reference convention: `side_reference(key)`, the file names, the commit message. |
| `tests/unit/test_handoff.py` | The golden handoff, in flight files, the base check, the primitive table, the protocol, and the credential. |
| `tests/unit/test_shape.py` | Spans with GenAI names, a structural OTLP check, events on their tool call, JSON lines, the side reference, and the import rule for `builders` and `trace`. |
| `tests/fixtures/handoff/EX-12.txt` | The golden handoff, rendered from the U6 fixture project. The example below is this file. |

## The six primitives and their carriers

KTD26 maps each primitive to exactly one carrier. A primitive with no row is not in the contract, and a test holds every protocol member to exactly one row.

| Primitive | Carrier | What it means |
|:-|:-|:-|
| Harness | `start(handoff, binding, credential) -> session_ref` | The agent loop. `start` hands it the handoff, the role's resolved binding (account, model, effort, from U6's `resolve_binding(slug, role)`), and the credential. |
| Environment | `poll`, `release`, `delete`, `events`, `schedule` | Where the session runs and what it leaves. `poll(session_ref)` returns a `SessionStatus`; a terminal one carries the `BuilderResult`. `release` archives a provider session or removes a container. `delete` removes the provider's record, only after the trace is read back (KTD17). `events` yields shape records from the adapter's own mapper, or `None` when the provider keeps no log. `schedule` is `remote_session` (the workflow polls) or `worker_process` (the task runs on a labelled worker and blocks). |
| Tools | `Handoff.tools` | The fixed tool list: shell, file read, file edit, git. Each adapter grants its own tools that provide these and nothing else. |
| Skills | `Handoff.rules` | The operator's rules text for the project, plus the project's machine bound tests. |
| Credentials | `Credential` | A refreshable secret scoped as KTD6 says: a repository wide machine identity, with the host's ruleset enforcing the `slipway/` branch namespace. The adapter injects it; the handoff names it and never prints it. |
| Schedule | `ResolvedBinding.time_budget_minutes` | The role's time budget. The workflow (U14) enforces it as a deadline from the moment the build child starts (KTD22). No adapter method waits or counts time. |

### The credential

`Credential(name, mint)` calls `mint()` once at construction and again on every `refresh()`. `mint` returns a new value and its expiry. `current()` returns the value. `refresh()` drops the old value before it returns, so nothing on the object still holds it; it refuses an empty value or the same value again. `due(now)` says whether the value expires within ten minutes, the cadence U12 and U13 refresh on, since a GitHub App installation token lives one hour. `repr` and `str` show the name only, the class has no `__dict__`, and pickling it raises, so the value cannot leak through a log line or a serialised task input.

## The handoff

`render` builds the document in one fixed order. Every value comes from the project file, the card, the caller, or the operator's rules text; nothing about any project is in the code (R1).

| Section | From |
|:-|:-|
| Title line | the card's key and title |
| 1. Base | the caller: the commit on the base branch the branch was created at |
| 2. Branch | the caller: the branch Slipway created at dispatch (KTD27) |
| 3. Rules | the `rules` argument (the operator's rules text), then the project file's `machine_bound_tests` |
| 4. Tools | `TOOLS` |
| 5. In flight files | `in_flight_files(in_flight, exclude_key=card.key)` |
| 6. Done | the card's `good` and `test` through `cards.contract.parse`, the project's `gate_command`, the branch |
| 7. Credential | the project file's `host_credential`, the operator file entry's name, never its value |
| 8. Side reference | `side_reference(card.key)` and the action log instructions |
| The card | the card's body as the tracker returned it |

Two checks run first. A missing base or branch is refused. When the tracker's `ClosingReference` (U8) carries a `branch_token`, as Jira's does, the branch must contain it, or the tracker would never see the branch and the card would never move to in progress.

An absent `good` renders "The card names no definition of done; meet its text as written." An absent `test` renders "The card names no test." No in flight files render "None."

### In flight files

The in flight files are the union over the project's other builds whose state is from `dispatched` to `merged`, the same list as `records.queries.IN_FLIGHT_STATES` (a test holds the two equal). A build before `returned` contributes the paths its card declares. A build at `returned` or later contributes the paths its pushed diff touches instead, when the caller has read them, because by then the diff is the truth. A card that declared no paths contributes nothing until it pushes (KTD5). The card being handed off is never its own in flight file.

```
other builds of the project
  |
  state outside dispatched to merged?          -> skipped
  |
  state returned or later, and pushed known?   -> pushed diff paths
  |
  otherwise                                    -> declared paths (may be none)
  |
  v
sorted union, this card's own key excluded
```

### A rendered example

This is `tests/fixtures/handoff/EX-12.txt` byte for byte, and a test checks that it is. It is rendered from the U6 fixture project (a Jira project whose host credential is named `example-app`, gate command `make check`, one machine bound test), the card `EX-12`, three other builds (`EX-9` building and declaring `src/nav.py`; `EX-10` at `pr_open`, declaring `src/old.py` but having pushed `src/footer.py` and `src/nav.py`; `EX-7` already `scored`), and two lines of rules.

````text
Slipway handoff for card EX-12: Add the hello page

1. Base: 0123456789abcdef0123456789abcdef01234567
   Start from this commit. Slipway created the branch below at it.

2. Branch: slipway/EX-12-hello-page
   Push your commits to this branch and to no other branch.

3. Rules:
   Write tests first.
   Keep each function under forty lines.

   These tests are machine bound: they need hardware this session does not have.
   Do not run them here; the project's gate runs them.
   * tests/test_usb_device.py

4. Tools: shell, file read, file edit, git.

5. Files other in flight builds declare or have pushed, which this card must not touch:
   * src/footer.py
   * src/nav.py

6. Done: The hello page renders the visitor's name. The card's test, tests/test_hello.py, passes. The project's gate command, make check, would pass. Every commit is pushed to slipway/EX-12-hello-page. Do not open a pull request; Slipway opens it.

7. Credential: git pushes with the credential named example-app, already configured.
   Never print it, and never write it to a file or a commit.

8. Side reference: refs/slipway/log/EX-12
   Keep an action log in actions.jsonl: one JSON object per line, with its type under "type",
   for each tool call, file change, command, and outside connection.
   Before you finish, push it as the only file of a commit to refs/slipway/log/EX-12.
   Never commit it to the branch.

The card:

Add a hello page that greets the visitor by name.

```slipway
{"good": "The hello page renders the visitor's name.", "paths": ["src/hello.py", "tests/test_hello.py"], "test": "tests/test_hello.py", "lane": "cloud"}
```
````

`src/old.py` is gone because `EX-10` pushed a diff without it, and `src/done.py` is gone because `EX-7` is no longer in flight.

## The builder result

`BuilderResult` is what a terminal `poll` carries:

| Field | Meaning |
|:-|:-|
| `branch` | the branch the builder pushed to |
| `commit` | the branch tip it pushed, or `None` when it pushed nothing |
| `reason` | why the session ended: a provider stop reason, an exit code, a budget |
| `provider_ref` | the provider's own session or container name |
| `cost` | the provider's reported spend, when it reports one |
| `model`, `account` | the binding the session ran under |
| `halt_class` | the KTD11 class when the build ends `returned_empty` for one |

`outcome` is `returned` when there is a commit and `returned_empty` when there is none.

`check_base(result, base, is_ancestor)` is the base validation (KTD22). `is_ancestor(base, commit)` answers from the project's repository; the workflow passes gitread's (U10), and the test passes `git merge-base --is-ancestor` on a throwaway repository. A commit that descends from the base passes unchanged. A commit that does not, or a branch still at the base (Slipway created the branch there, so it means nothing was pushed), becomes `returned_empty` with the reason, halt class `builder_failed`, and the cost, model, and account kept, so the spend is still recorded.

## The trace shape

An adapter never builds spans. Its mapper turns the provider's own events into shape records and yields them from `events`. `Trace` builds the spans, and `otlp()` writes the OTLP JSON body U15 sends to Langfuse.

| Record | Becomes | Main attributes |
|:-|:-|:-|
| `ModelRequest` | span `chat <model>`, kind client | `gen_ai.operation.name` chat, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, the two cache token counts |
| `ToolCall` | span `execute_tool <name>`, kind internal | `gen_ai.operation.name` execute_tool, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`; an error sets the span status |
| `FileChange` | event `slipway.file_change` | `slipway.file.path`, `slipway.file.action` |
| `Command` | event `slipway.command` | `process.command_line`, `process.exit.code` |
| `Connection` | event `slipway.connection` | `server.address`, `server.port`, `url.full` |

One root span, `invoke_agent <builder>`, parents every other span. It carries `slipway.card.key`, the branch as `vcs.ref.head.name`, the base as `vcs.ref.base.revision`, and, after `finish`, the pushed commit as `vcs.ref.head.revision` and the diff counts as `slipway.diff.files_changed`, `slipway.diff.insertions`, and `slipway.diff.deletions`. Its start and end cover every child and event. An event carries the `tool_call_id` of the tool call that caused it and lands on that span; with no tool call, it lands on the root.

Span ids come from a hash of the trace id and a counter, so the same records always give the same spans, which keeps tests and a replay stable. A trace id is 32 hex characters; one is drawn at random when none is given.

How one adapter's events map onto the shape, using a CLI transcript as the example (U13 writes the real mapper):

```
provider log (CLI transcript jsonl)       shape records             spans and events

assistant message, usage 1200 / 340   ->  ModelRequest          ->  span  chat model-large
tool_use bash id tool_1               ->  ToolCall tool_1       ->  span  execute_tool bash
  tool_result "3 passed"                    (joined by id)
  ran "pytest -q", exit 0             ->  Command tool_1        ->        event slipway.command
tool_use edit id tool_2               ->  ToolCall tool_2       ->  span  execute_tool edit
  wrote src/hello.py                  ->  FileChange tool_2     ->        event slipway.file_change
tool_use bash id tool_3, error        ->  ToolCall tool_3       ->  span  execute_tool bash (error)
  fetched pypi.org:443                ->  Connection tool_3     ->        event slipway.connection
session end, branch tip               ->  Trace.finish(commit)  ->  root  invoke_agent cli
                                                                          card key, branch, base,
                                                                          commit, diff counts
```

The Managed Agents mapper (U12) reads `agent.tool_use` and model events from the session's event list into the same five records.

The OTLP test checks the JSON mapping by structure: ids are lower case hex of 32 and 16 characters and not all zero, 64 bit integers and timestamps are decimal strings, every attribute value is exactly one typed wrapper, every parent id exists, and the body survives a JSON round trip.

## The action log side reference

Every build's records also live in git on the project's repository, so the trace lasts as long as the project (R22) even after Langfuse's thirty days. The convention, fixed in `trace/shape.py` so the handoff, both adapters, and the exporter agree:

| Part | Convention |
|:-|:-|
| Reference | `refs/slipway/log/<card key>`. Outside `refs/heads`, so it is no branch and never shows on the board. The host ruleset (KTD6) names it beside `refs/heads/slipway/**` for the machine identity. A key that cannot name a git reference is refused. |
| Commits | One per build attempt and writer, each the child of the reference's previous tip, so the history is the attempt list. |
| `actions.jsonl` | The builder's own action log, which the handoff's section 8 asks it to keep and push. |
| `trace.jsonl` | The shape records the adapter mapped from the provider's log, written before the session is released (KTD17). |
| `transcript.jsonl` | The raw transcript, when the builder has one (the CLI builder does). |
| Message | `slipway log <card key> build <build id>` from `log_commit_message`. |

Each line is one record as JSON with its type under `type` (`model_request`, `tool_call`, `file_change`, `command`, `connection`), written by `dump_jsonl` and read by `load_jsonl`, which refuses an unknown type or field. When a provider returns no event log after a session ends, the trace is built from `actions.jsonl` (AE5). Records are written after redaction (KTD24); this module never scrubs.

## Imports

`builders` imports `cards` (the block parser), `trackers` (`ClosingReference`), `config` (`ResolvedBinding`), and `trace`. `trace` imports only the standard library. Neither imports `service`, `workflow`, `records`, or a surface; a test in `test_shape.py` holds that. The in flight state list is repeated rather than imported from `records` for that reason, with a test holding the two equal.

## Where this differs from the plan

- **`render` takes two more arguments.** `rules`, the operator's rules text, because the project file has no rules field and this card does not change config. `closing`, the tracker's closing reference, so a branch missing the tracker's branch token is refused before dispatch.
- **The handoff has a Tools section and the card's own text.** KTD26 says the handoff names the fixed tool list, and a builder needs the card it is building. The six sections the test scenario names keep their order.
- **The side reference has no excluded path alternative.** The plan offers an excluded path the project file names; the project file has no such field, so the reference is always `refs/slipway/log/<key>`.
- **The builder pushes its own action log.** R22 and AE5 say the trace is built from the action log the builder pushed when the provider returns none, so section 8 asks for `actions.jsonl` on the side reference, beside the adapter's `trace.jsonl`.
- **`BuilderResult` has `halt_class`,** and `check_base` sets it to `builder_failed`. A branch still at the base is also `returned_empty`, since Slipway created the branch there.
- **`check_base` takes `is_ancestor` as a callable** instead of calling git, because `builders` may not import gitread and U10 owns it.
- **No OpenTelemetry library.** `pyproject.toml` is not this card's to change, so `Trace` writes the OTLP JSON mapping with the standard library and the test checks it by structure, not with the protobuf schema.
- **The in flight state list is repeated** from `records.queries`, held equal by a test, because `builders` may not import `records`.
- **The plan's pattern, `lanes.py` `cmd_handoff`, is not in this checkout.** The shape follows the plan's list and this repository's own handoff shape in `CLAUDE.md`.
- **Extra files.** `src/slipway/builders/__init__.py`, `src/slipway/trace/__init__.py`, and the golden fixture `tests/fixtures/handoff/EX-12.txt`.
