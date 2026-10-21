# U19 explainer: onboard, one state machine, terminal first

Onboarding is how a project gets its project file without anyone writing it by hand. The operator answers one question at a time; onboard validates each answer, saves it, checks the four properties a project needs before Slipway builds for it, and ends by handing back one trivial card to file. When that card scores, the project is verified. This card builds the state machine and its first client, the terminal. The MCP tools (U18) will be its second client, over the same three calls.

## Words used below

- **MCP.** The Model Context Protocol, the tool protocol an agent calls Slipway through (U18).
- **State machine.** Code whose next step depends only on what has been saved so far. Here the saved state is the answers, the attestations, and the last check results; every call reads them, decides what comes next, and saves them again.
- **Session.** One project's onboarding. Its id is the project's slug, so there is one session per project and a dropped one resumes where it stopped.
- **Operator only question.** A question only the operator's subject (the stable user id in the operator's token) may answer, because its answer moves spend, widens access, or names the operator's accounts (KTD15).
- **Attestation.** A statement a person makes, recorded with who made it and when, for something Slipway cannot read for itself.
- **Probe.** Onboard's reads of the host and the tracker: listings for choices (boards, labels, statuses, builders) and the property reads (the board moves, a required check exists, the machine identity is restricted).
- **Machine identity.** The credential builders push branches with (KTD6). The host must restrict it to `refs/heads/slipway/**`.
- **Trivial card.** A tiny card onboard writes for the caller to file, so the first real card is not the first test of the path (R31).

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/onboard/questions.py` | The questions as data, in the walk's order: id, text, why, kind, the project file key it fills, where its choices come from, whether free text is taken, its default, and the operator only flag. Also the on demand questions: `recheck`, `trivial_card`, and an attestation for a probe the host cannot answer. |
| `src/slipway/onboard/machine.py` | `start`, `answer`, and `status`. Validation of one answer, the state file, the project file writer, the walk, the property checks in order, and following the trivial card's build through the records. |
| `src/slipway/onboard/checks.py` | The `Probe` interface, the four property checks plus the single dispatcher rule, each returning one `Result` with what is missing and one next step, the re entry reads, and `LiveProbe` over today's adapters. |
| `src/slipway/onboard/terminal.py` | The terminal client: `run` shows a question, reads a line, calls `answer`, and repeats; `main` builds the real ports. |
| `tests/unit/test_onboard.py` | The card's scenarios on a fake probe and the service fakes. |
| `tests/fixtures/onboard/` | The operator file the tests run against (it adds a Bitbucket credential the shared fixture lacks) and the hand written project file the terminal scenario must reproduce. |

## The three calls

Each returns a plain mapping whose `outcome` tells the caller what to do next:

| outcome | meaning | what comes with it |
|:-|:-|:-|
| `question` | answer this next | `question`, `pending` |
| `refused` | the answer was not taken, nothing changed | `reason`, the same `question` |
| `needs_fix` | a property is missing | `property`, `missing`, `next_step`, and the `question` to answer once it is done |
| `file_card` | every check passed | `card`, the trivial card to file, and the `trivial_card` question |
| `waiting` | the trivial card is not done, or only the operator's questions are left | `reason` |
| `verified` | the trivial card scored | `card_key` |

Every result also carries `answers`, so `status` alone is enough to resume. `status` without an actor shows the operator's view, which is what the terminal wants; the MCP front end (U18) must pass the caller's subject so a manager agent gets its own view.

A fault in a key no question fills, such as a `[rules]` table whose file is missing, is refused with its reason rather than pinned on a question, since no answer could fix it.

## What is saved, and where

Every accepted answer goes to `$SLIPWAY_HOME/onboard/<slug>.json` with who gave it and when, and sets `verified` to false. Once every key the project file needs has an answer, onboard writes `$SLIPWAY_HOME/projects/<slug>.toml`, but only after `parse_project` and `check_names` accept the whole file (the note #74 left on this card). If either refuses, the question that fills the key at fault is asked again with the reason. After that, every accepted answer rewrites the file. Until the file is whole, nothing sits under `projects/`, so watch, dispatch, and `other_lanes` never read half a project.

The file opens with a two line comment naming who answered last; the keys follow in the walk's order, then the role tables. Keys onboard does not ask, such as a `[rules]` table, are kept as they were.

## The rules each answer passes

1. The caller must be the operator or a subject on the project's `allowed_subjects`.
2. An operator only question from anyone else is refused with "the operator sets this", and nothing is written. That subject is shown no choices for it, so it never sees the operator's account names or Langfuse projects, and `status` shows it `(set by the operator)` in place of those answers. A manager agent's walk skips those questions and ends with `waiting` once only the operator's are left.
3. Any string that matches a token pattern (`redact.TOKEN_PATTERNS`) is refused. The audit row keeps its reason, hash, and length (`redact.refusal_record`), never the value.
4. The answer is read by its kind (a number, yes or no, a list, a choice) and checked by shape: a repository is `owner/name`, a GitHub Projects board is `owner/number` or `number` (the note #47 left on this card), a Jira board is a project key, a tracker credential must be for the answered site, a host credential for the answered host, and a path must stay inside the repository.
5. The U26 fields are asked too: `checkout` (clone or worktree for a laptop build), `branch_template`, and `pull_request_title`, each optional, and `landing_path` offers `external_lander`. A branch template is checked on its own as `config.project` would check it; any answer the whole file then refuses (`parse_project` or `check_names`) is refused, and nothing is saved.
6. The gate command is stored as text. Nothing in onboard runs it (KTD18); the test suite fails any onboard test that starts a process.

## The checks

Once every question is answered, the checks run in this order and stop at the first that fails, so one next step comes back:

1. **Single dispatcher.** "Another system dispatches this board" answered yes refuses the finish until it is answered no.
2. **Durable state (KTD27).** The tracker is readable; the board exists (on GitHub that includes a single select field named Status, the note #47 left); and the board moved a card to in progress when Slipway's scratch branch was created and to in review when its scratch pull request opened. A board that did not move gets the board workflow template (`templates/slipway-board.yml`) as its next step on GitHub Projects, or the two Jira automation rules on Jira (AE13).
3. **External gate (AE6).** The host requires a check before merge; the machine identity is restricted to the branch namespace, `refs/heads/<prefix>**`, where the prefix is the literal text in front of the branch template's first placeholder (U26), `slipway/` by default; and the landing path and merge method are known. A Bitbucket project that pins neither is asked to pin them, since Bitbucket exposes no queue setting. A branch template with no prefix has no namespace to hold, so it is sent back to the branch template question.
4. **Independence** and 5. **verification**, the two attestations.

When the probe cannot read part of 2 or 3 it answers None, and the check asks the operator to attest that part instead, recorded with identity. A failing check whose fix is on the host asks `recheck`; answering yes runs the checks again. The checks also run again whenever an answer changes, and on every `start`.

## The trivial card, and a wrong gate command

The final step returns the card: the lane label, a title, a body whose `slipway` block targets `trivial_card_target`, and the instruction to file it in the ready status. Slipway never writes the tracker, so the caller files it and answers `trivial_card` with its key. Watch dispatches it like any card. Onboard then follows that card's build in the records: `scored` sets `verified`; `gate_red` sends onboard back to the gate question with the host's output as the question's note; a build that ended empty or abandoned asks for the card to be filed again.

## Re entry (R32)

`start` on a project that has a file takes the file's values as the answers, then marks as no longer true: the repository if the probe cannot reach it, the board if it is gone, each role's account if the operator file no longer has it, and anything `check_names` refuses. Only those questions are asked. The three answers with no project file key (`other_dispatcher`, `independence`, `verification`) live in the state file alone, so a re entry whose state file is gone asks them again, since they are missing. The permission check comes before any of this, so a subject off the allowed list causes no host or tracker read. The checks run again, so a restriction removed since then is caught there.

## Where this departs from the card, and why

- **`verified` lives in the onboard state file, not the project file.** `config/project.py` refuses unknown keys, and the conductor reserved that file for U26 (#89). A follow up can move the flag into the project file once U26 lands, if dispatch should read it.
- **The project file is written once it is whole**, not after the first answer, so no reader ever meets a file that cannot load. Each answer before that is saved in the state file.
- **The live probe reads only what today's adapters can.** The U8 and U9 adapters cannot list boards or labels, read rulesets, branch permissions, or required checks, or watch a board move, and `hosts/` and `trackers/` are outside this card's paths. `LiveProbe` therefore reads the tracker and the landing path and answers None for the rest, which a live onboarding meets as operator attestations. Teaching the adapters those reads is a follow up card; the checks and their next steps are already in place for when they answer.
- **The cold onboarding is recorded on fakes.** It ran through the terminal on a fresh `SLIPWAY_HOME` with the fake probe and records; no live Bitbucket or Jira credential exists in a cloud session.
- **Records.** `SLIPWAY_DSN` names the Postgres the terminal writes audit rows to and reads the trivial card's build from. Without it, audit rows go to a JSON lines file beside the session's state, and the trivial card waits until a database is named.
