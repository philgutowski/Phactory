# Spec flow analysis: Phactory first version

Written 2026-10-14 against the contract at
`/Users/pgutowski/Documents/PhilAI/Phactory/docs/plans/2026-10-14-1730-feat-phactory-first-version-plan.md`,
grounded on the carried rules in `docs/ideation/2026-10-14-software-factory.md`, the landing research in
`docs/research/2026-10-14-brainstorm/research-landing.md`, and the earlier runner's behaviour in
`docs/research/2026-10-14-brainstorm/grounding.md`. The codebase is empty, so every default below is drawn
from those four files, not from code.

Scope held: the Key Decisions are settled and are not reopened here. The three spikes (Managed Agents event
log, exact tested commit and enqueue API per host, MCP on Grok and the Claude app) are known and are not
listed as gaps. Where a gap touches a spike it says so and stops.

Two perspectives are used throughout. The end user is the manager agent filing cards and reading status over
MCP. The operator is Phillip, approving pull requests in the host app and reading the page.

## Part 1. The card state machine

Every state a card can occupy from filing to scoring, the event that moves it, and who or what causes the
event. States are written as `snake_case` names so the planner can lift them straight into a workflow
definition. "A5" means Phactory itself; the other actor codes are the plan's.

### 1.1 The happy path (F1)

1. `filed`. Card exists on the tracker with a lane label and a definition of good. Event: the manager agent
   (A2) or the operator (A1) files it. Phactory has not seen it.
2. `eligible`. Phactory's watch cycle reads the card as ready: lane label present, card in the project's
   ready status, no Phactory workflow already owns this card key. Event: a watch cycle or an explicit
   `dispatch` call (A5, caused by A2 or A1). The contract does not say whether a labelled card dispatches
   itself or waits for the verb; see gap C2.
3. `waiting_for_slot`. The project's cap is full. Event: the cap check (A5). See Part 1.6.
4. `dispatched`. Handoff written (base commit, branch name, rules, in flight files, done line), builder
   session started in a fresh environment, role binding (account, model, effort) and caller identity
   recorded. Event: a slot frees or is free (A5).
5. `building`. The builder session is running. Phactory has no view inside it until it ends, except what the
   provider streams. Event: none; this is a wait.
6. `returned`. The session ended and a branch at a commit exists on the remote. Event: the builder (A3)
   finishes. Trace export (R21, R23) belongs here, before landing, so a card that never lands still has a
   trace.
7. `pr_open`. One pull request titled with the card key, carrying the host's closing reference, opened
   against the default branch. Event: Phactory (A5), or the builder (A3); the contract says both, see gap C1.
8. `awaiting_approval`. The pull request sits with its head CI run and waits for a human approval, or, with
   auto merge on, for green. Event: none; this is the long wait. On both hosts approval precedes the queue,
   not the other way round; see gap C3.
9. `queued`. The pull request is in the host's native merge queue. Event: the host (A4) after approval and
   required checks on GitHub; the operator or an API call pressing "Add to merge queue" after merge checks
   pass on Bitbucket. Who presses it on Bitbucket is unspecified; see gap C3.
10. `gate_running`. The queue runs the project's CI plus Phactory's deterministic checks on the merge
    result. Event: the host (A4).
11. `merged`. The queue merged. Event: the host (A4). Phactory records approved by whom, or auto merge
    (R19), read from the host.
12. `verifying`. Phactory reads git and the tracker. Event: a host event reaching Phactory, or a poll. The
    contract names neither; see gap I2.
13. `landed`. Merged commit equals the tested commit and the tracker moved the card to the project's landed
    status by its own automation. Event: verify (A5).
14. `scored`. Deterministic score and judged score recorded against the trace. Event: eval (A5). Terminal.

The page buckets (R28) map onto these: being built is 4 to 6, waiting to land is 7 to 12, needs you is the
branch states below that carry a human action. The contract does not state the mapping; see gap M3.

### 1.2 F1 branch: the builder returns no branch

* From `building`, the session ends with no branch on the remote, or with a branch at the base commit.
* New state `returned_empty`. Event: the builder (A3) ends or the provider reports failure.
* What the contract says: nothing. R10 says the builder "returns a branch at a commit." No requirement
  covers the builder not doing so.
* Earlier runner: a task whose branch never appeared was not landed; the run moved on, and the task stayed on
  the tracker for the next run to pick up again (`verify.py` turns an unreadable input into a blocking skip,
  and the runner "resumes at the first task that did not land").
* Consequence without a rule: a watch cycle sees the card still labelled and still ready, and dispatches it
  again, every cycle, at full builder cost. This is the loop the runner avoided with its state file keyed on
  task id, which Phactory has deliberately given up.
* Default: `returned_empty` is terminal for that workflow, shown under needs you with the trace. The card
  is not re dispatched until a re dispatch trigger named in gap C2 fires.

### 1.3 F1 branch: the branch fails the gate in the merge queue

* From `gate_running`, the queue removes the pull request on a red run (both hosts do this).
* New state `gate_red`. Event: the host (A4).
* The pull request stays open and red. The operator has already approved it (see gap C3), so this is a
  second visit to the same card.
* Earlier runner: "a nonzero exit strands the branch." No automatic rebuild, no second dispatch.
* Default: `gate_red` is shown under needs you with the link into the host and the gate log. Phactory does
  not re dispatch, since Phactory does not build and has no feedback channel to a builder whose session is
  gone. The operator either fixes by hand on the branch (the queue then retries when re added) or closes the
  pull request (Part 1.6 below). A later version may offer "rebuild with the gate log as a comment," which
  is a re dispatch under gap C2's trigger.

### 1.4 F1 branch: the pull request sits unapproved for days

* The card stays in `awaiting_approval`. Nothing in Phactory times out, and nothing should: the durable
  engine exists so this wait costs nothing.
* Three things do change while it waits. The base moves, which the queue handles by testing the merge
  result. The builder session is long gone, so no builder can respond to review comments. And the card holds
  whatever it holds against the cap and the in flight files fact for as long as it waits.
* Default: no timeout. The card appears under needs you from the moment it enters `awaiting_approval`,
  with age shown. What it occupies while waiting is gap C4, and the answer decides whether a slow approving
  operator starves the project of builders.

### 1.5 F1 branch: the queue merges but the tracker rule does not fire

* From `merged`, verify finds the commit on main but the card still in its old status. The usual cause is a
  missing key in the title (Jira) or a missing closing keyword in the body (GitHub issues need "Closes #N"
  in the body, the title is not enough, and the project item moves only through the "item closed" workflow).
* New state `halted` with class `merged_card_not_moved`. Event: verify (A5). This is F3 and AE2.
* Default: as the contract says, needs you with evidence. The repair is the operator moving the card by hand
  (the one tracker write Phactory is allowed to cause, since a person does it) and then resuming. How
  resume is triggered is gap I1. The prevention is the per host closing reference form carried in the pull
  request at open time, which the runner's adapter interface already models as `closing_reference(id, ref)`;
  the contract's "titled with the card's key" is sufficient for Jira and insufficient for GitHub.

### 1.6 F1 branch: a second card edits the same files while the first is in the queue

* Card B reaches `dispatched` while card A is anywhere from `dispatched` to `merged`. B's handoff carries
  A's files under "files other in flight builds are editing" (R10).
* What the lanes script did: informed, never blocked. The builder decides how to work around it.
* What happens next: B's pull request enters the queue after A's. The queue tests B on top of A's merge.
  A textual conflict fails B's queue run; B goes to `gate_red` (Part 1.3). A semantic conflict is caught
  only if the gate covers it.
* Default: inform, do not block, exactly as the lanes did. The queue is the arbiter. Gap C5 covers how
  Phactory knows A's files at all before A has pushed anything.

### 1.7 F1 branch: the operator closes the pull request without merging

* From `pr_open`, `awaiting_approval`, or `gate_red`, the pull request closes unmerged.
* New state `abandoned`. Event: the operator (A1) in the host app, observed by Phactory through the same
  channel as merges (gap I2).
* The card on the tracker keeps its lane label and its status, because Phactory never writes the tracker and
  no automation fires on a closed pull request. So the card is still "ready" to a watch cycle.
* Default: `abandoned` is terminal for that workflow. The card is not re dispatched until gap C2's trigger
  fires. The page shows it under needs you once, with the action "move or relabel the card, or re dispatch
  with a reason," and then the manager agent or operator does one of those.

### 1.8 F1 branch: the card is relabelled or closed on the tracker mid build

* From `building`, the lane label is removed, changed to another lane, or the card is closed or moved to a
  non ready status.
* What the lanes rule says: "a label added while the loop runs must be honoured on the next cycle." The
  removal case is not written down anywhere.
* What the runner did: it read readiness once at cycle start and finished the task it had started. Verify
  then compared against the tracker, so a card closed by hand showed up as landed by hand or as a halt.
* Options: cancel the builder session (saves cost, loses a possibly good branch), or finish the build and
  stop at the next step boundary (open no pull request if the card no longer carries the lane label or is
  closed).
* Default: re read the card at every state transition Phactory causes (dispatch, pr_open, enqueue, verify).
  If the card is no longer ready at a boundary, move to `abandoned` with the reason and do not take the
  step. Do not cancel a running session in the first version; cancellation per provider is adapter work
  under R11 and can come later. A label changed to a different lane counts as not ready for this workflow
  and eligible for a new one, which is where "two systems landing the same card" bites: see gap C2.

### 1.9 F2 onboarding, including the failed property branch (R30, R32)

1. `intake`. One question at a time with choices. Event: the operator or manager agent (A1 or A2) answers.
2. `draft_saved`. Every answered question is persisted to the project file as it is answered, so re entry
   (R32) has something to diff against. The contract says onboard "ends with a working project file"; it
   does not say what exists before the end. Default: save after every answer, mark the file not yet
   verified.
3. `checking_properties`. The four properties are checked. Two are mechanical: durable state (the tracker
   and board exist and are readable with the given credentials) and an external gate (required checks
   exist in the ruleset or merge checks, the queue is on, a workflow or pipeline triggers on the queue's
   event). Two are attestations Phactory cannot test: independent tasks and a written definition of
   verification. Default: ask for the file path of the written definition and store it; ask the operator to
   confirm tasks are independent and record who confirmed.
4. `property_failed`. One property fails. Event: the check (A5). Phactory states what is missing and offers
   the fix as the next single step (AE6). The operator leaves to add CI, a ruleset, a board.
5. Re entry (R32): the operator runs onboard again. Phactory loads the saved file, re checks every stored
   answer that can go stale (repo reachable, board exists, gate command present, accounts still in the
   operator file), asks only what is missing or false, and runs the property check again. "No longer
   true" is not defined in the contract; the default list above is the one the planner should write down.
6. `trivial_card_pending`. R31 requires landing one trivial card. Phactory never writes the tracker, so
   Phactory cannot file this card. The operator or manager agent must file it, and Phactory must tell them
   exactly what to file. What the card changes is project specific, so Phactory cannot choose it either.
   Default: onboard asks "which file may the trivial card append one line to" as an intake question, then
   says "file a card labelled X with this good block" and waits for it to appear.
7. `trivial_card_in_flight`. The trivial card runs the whole of Part 1.1, including `awaiting_approval`,
   unless auto merge is on. Onboarding is therefore not a single sitting. It is a workflow with the same
   long wait as any card.
8. `onboarded`. Trivial card reached `scored`. The project file is marked verified. Event: eval (A5).

Branch worth naming: the project is already fed by the old runner or the lanes script (Cratekit,
support-workbench). Onboarding it into Phactory while the old system still runs is the exact failure the
carried rules call the worst one, two systems landing the same card. See gap I5.

### 1.10 F3 halt and resume

* Halt classes the contract implies: `merged_card_not_moved`, `commit_mismatch` (merged commit differs
  from the tested one), and, from the runner's rule that an unreadable input is a blocking skip,
  `tracker_unreadable` and `git_unreadable`. The runner kept halt classes as a closed set in one file; the
  default is to do the same.
* Who resumes: the operator (A1), after repairing by hand. The contract says "the workflow resumes at
  verify" but names no trigger. Options: Phactory polls the tracker until the card moves (then the
  operator does nothing but move the card), the operator calls a verb (`land` or a `resume` argument on
  it), or the engine exposes a signal. See gap I1.
* From which state: `halted` back to `verifying`, never earlier. The code is already on main. A halted card
  is not re dispatched; re dispatching would build the same card a second time on top of its own landing.
* The branch with no repair: `commit_mismatch`. The operator cannot make the merged commit equal the tested
  one after the fact. The only honest outcomes are accept with a recorded reason and identity (R8), or
  revert on main and treat the card as abandoned. The contract has no accept path, so a `commit_mismatch`
  halt is stuck forever. Default: an explicit override on the land verb that records who, why, and the two
  shas, and moves the card to `landed` with the override visible in the score record.

### 1.11 F4 swapping a binding mid flight

* The operator edits the project file. Cards already in `building` keep the session they have. Cards in
  `waiting_for_slot` and later dispatches pick up the new binding. Each task record carries the binding it
  used (R15), so the before and after comparison in F4 falls out of the records.
* Unspecified: whether each step reads the binding when it runs or the whole workflow snapshots it at
  dispatch. The judge role matters here: a card built under judge A and scored after a swap to judge B
  should record B, since that is what ran. Default: read at the step, record at the step.
* Unspecified: a binding naming an account that is not in the operator file, or a model string the
  provider rejects. The project file is edited by hand, so there is no edit time validation. Default:
  dispatch validates the binding first and halts the card with class `binding_invalid` before any builder
  cost is spent. This is a fifth halt class.

### 1.12 The in flight files fact (R10)

How Phactory knows which files another build is editing before that build has pushed anything. There are
exactly two sources and the contract uses one of them without saying so.

* Declared paths. R24's deterministic score checks "diff inside declared paths," so a card declares the
  paths it will touch. The union of declared paths across every card from `dispatched` to `merged` is the
  pre push in flight files fact. This is the only pre push source possible, since a sandbox or laptop
  worktree is invisible to Phactory until a push.
* Actual diff. Once a card reaches `returned`, its real diff is known and replaces its declared paths in
  the fact. From then until `merged`, the fact is exact.
* A card with no declared paths contributes nothing until it pushes. The lanes script had the same blind
  spot and lived with it.

Default: the fact is declared paths until push, actual diff after, scoped to cards between `dispatched` and
`merged`. Where declared paths live on the card is gap C5.

### 1.13 Cap and lease (R5): the N plus first card

* The N plus first eligible card enters `waiting_for_slot`. It is dispatched when a slot frees, in the
  order the tracker adapter's ready read returns cards (the runner used the adapter's `ready` order; the
  lanes script said "take it after #N"). Default: tracker order, which the project controls by ordering its
  board.
* What occupies a slot is not defined. "Builds in flight" could mean `dispatched` to `returned` (a builder
  session is running, which is what costs money and what an account's concurrency limit counts) or
  `dispatched` to `merged` (what the in flight files fact counts). With the second meaning, a card waiting
  days for approval holds a slot, and a project with cap 2 and a slow approver builds nothing.
* Default: the cap counts builder sessions, `dispatched` to `returned`. The in flight files fact counts
  `dispatched` to `merged`. These are two sets and should be two names in the plan.
* The lifted lease. The carried rule is "one lease per repository, two runs never merge into the same
  repository at once." The host's queue now serialises merges, and Phactory never merges, so the lease as
  written guards nothing. What still needs serialising per repository is the push and the pull request open
  (one pusher per repository, from the lanes rules), and what needs counting per project is the cap. Default:
  the lease becomes two engine concurrency keys, one per project for the cap and one per repository for the
  push and open step, and the runner's heartbeat and TTL semantics are not lifted, because the engine
  owns liveness. If the planner wants the lease file lifted as the contract's Dependencies say, it should
  say what it guards.
* Accounts are pooled across projects (R13 to R15), so two projects each under their own cap can together
  exceed one subscription account's concurrency. No per account limit exists in the contract. Default for
  the first version: none, and the failure surfaces as a provider error in `building` and becomes a halt
  under gap I3.

## Part 2. Gaps

Ordered by severity. Each names what is missing, why it matters, a default drawn from the carried rules or
the runner, and one question with stakes.

### Critical

**C1. Who pushes the branch, and with what credential.**
What is missing: R17 says "Phactory pushes each returned branch and opens one pull request." F1's steps say
"the builder pushes a branch and opens a pull request titled with the card key." R10 says the builder
"returns a branch at a commit." These cannot all be true. The physical constraint decides it: a hosted
builder's sandbox is destroyed when the session ends, so the commit has to leave the sandbox before then,
and the only party inside the sandbox is the builder. Either the builder pushes, or Phactory pulls from a
live sandbox before letting it end, or the builder pushes to a staging remote Phactory owns.
Why it matters: it decides which credential enters every sandbox (a customer repo write token in a Managed
Agents session and in a subscription CLI sandbox), what "one pusher per repository" means, how R22's action
log reaches Phactory, and whether R16 holds. It also shapes the Managed Agents spike, which should test the
push path, not only the event log.
Default: the builder pushes its own branch under a credential scoped to that branch namespace and nothing
else (a deploy key or fine grained token limited to `refs/heads/phactory/*`), Phactory confirms the commit
exists on the remote, Phactory opens the pull request and holds the only credential that can do so. The
queue is the only writer to main. R17's prose becomes "Phactory opens one pull request per returned
branch."
Question: does the builder push its branch, or does Phactory extract it? Stakes: credential scope in every
sandbox and the shape of the Managed Agents adapter.

**C2. Card ownership, terminal states, and the re dispatch trigger.**
What is missing: Phactory cannot mark the tracker, and the runner's state file is gone by decision. Nothing
in the contract says how Phactory remembers it already owns a card, or what makes an owned card eligible
again. Every branch in Part 1 that ends short of `landed` (`returned_empty`, `gate_red`, `abandoned`,
`halted`) leaves a card that still looks ready to a watch cycle. Separately, the contract does not say
whether a labelled card dispatches itself on a watch cycle or waits for an explicit `dispatch` call; the
carried rule "route at filing, nobody routes" and the verb list imply both exist.
Why it matters: without an ownership record, every failed card is rebuilt every cycle at full cost, or
dispatched twice at once, which is the carried rules' worst failure. Without a defined re dispatch trigger,
every failed card is stuck forever.
Default: one workflow per (project, card key) as the engine's idempotency key, which every candidate engine
supports. Watch dispatches a ready card only if no workflow exists for its key. Terminal states are
`scored`, `abandoned`, `returned_empty`, `gate_red` (until closed), and `halted`. Re dispatch happens only
through the explicit `dispatch` verb with a reason, recorded under R8, which starts a new workflow with a
new branch name; a second workflow can never start while a first is non terminal.
Question: is dispatch automatic on label or explicit by verb, and what exactly reopens a card? Stakes: the
cost of every failure and the double landing rule.

**C3. Approval comes before the queue gate on both hosts, and the Bitbucket enqueue has a hand on it.**
What is missing: the flowchart runs the queue gate first and asks for approval after. On GitHub a pull
request enters the queue only after branch protection (including required approvals) is satisfied, and
the merge result is tested in the queue afterwards. On Bitbucket merge checks (approvals) must pass before
"Add to merge queue" is available, and then the queue tests. In both, the operator approves a pull request
whose merge result has not yet been tested, and `gate_red` arrives after approval. On Bitbucket, someone
has to press "Add to merge queue," and the contract does not say who.
Why it matters: the needs you bucket shows "approve" on a pull request that has only passed head CI, then
may show the same card again as red. The operator's expectation from the contract (approve a green merge
result) is not what either host offers. And if nobody presses enqueue on Bitbucket, every Bitbucket card
stalls at `awaiting_approval` forever with approvals present.
Default: the needs you entry says "approve, the queue will test the merge after." Head CI on the pull
request is treated as the first signal and shown. On Bitbucket, Phactory enqueues by API once approvals are
present (spike two covers whether the API exists; if not, the operator presses it and the needs you entry
says so). Phactory's deterministic checks run as a required check so they gate before approval where the
host allows.
Question: does the operator approve before or after the merge result is tested, and who enqueues on
Bitbucket? Stakes: the shape of the approval wait step the engine spike is built around.

**C4. What a slot is, what the in flight set is, and what the lifted lease guards.**
What is missing: Part 1.13. "Builds in flight" has two readings with opposite operational consequences.
The lease the Dependencies say will be lifted guards a merge Phactory no longer performs.
Why it matters: with the wide reading a slow approver starves builds; with the narrow reading the in flight
files fact must be defined separately or it drops cards the moment they return. The lease question decides
whether a runner module is lifted for no purpose.
Default: cap counts `dispatched` to `returned`; in flight files counts `dispatched` to `merged`; the lease
becomes two engine concurrency keys (per project cap, per repository push and open) and the runner's
heartbeat lease is not lifted.
Question: does a card waiting for approval hold a build slot? Stakes: whether cap is a cost control or a
conflict control, and whether `state.py` is lifted at all.

**C5. The card contract.**
What is missing: the contract relies on fields a card carries without naming them anywhere: the lane label
(R9), the definition of good (F1 trigger, R24), declared paths (R24 and, by Part 1.12, R10), the named
test (R24, AE7). It does not say where these live per tracker (a Jira custom field or description block, a
GitHub issue body section), who parses them (the read only tracker adapter), or what happens when one is
absent. The deferred question about a pre filing check is downstream of this and cannot be answered until
the schema exists.
Why it matters: the in flight files fact, the deterministic score, the judged score, and the handoff's
done line all read from this schema. Project agnosticism means the schema has to be the same on every
tracker, or the adapter has to map it, and the manager agent on a phone has to be able to produce it in a
conversation.
Default: the ideation note's `good:` block, extended to a fenced block in the card body with `good`,
`paths`, and `test` keys, parsed by the adapter's `read`. Absent `paths` means the in flight contribution
is empty until push and the paths check records "not declared" rather than fail. Absent `good` means the
judged score records "no rubric," not zero. Absent `test` means that deterministic item records "none
named."
Question: what exactly must a card carry, in what form, and what does each score do when a field is
absent? Stakes: the adapter's read contract and whether a phone filed card can be built at all.

### Important

**I1. Resume mechanics and the halt with no repair.**
Missing: the trigger that moves `halted` back to `verifying`, and any path out of `commit_mismatch`.
Default: `land` verb re runs verify for a named card (the runner's "run again, the runner re verifies");
watch also re verifies halted cards each cycle so a card the operator moved by hand clears itself; an
explicit override argument on `land` records who, why, and both shas for `commit_mismatch`. Halt classes
are a closed set in one file, as the runner kept them.
Question: can the operator accept a mismatch with a recorded reason, or is revert the only exit? Stakes: a
halt the page can never clear.

**I2. How host events reach Phactory.**
Missing: whether Phactory learns of merges, closes, and gate results by webhook or by polling. "Remote
builders never poll" is a rule about builders, not the control plane, but the hosted and unattended decision
means a webhook endpoint is a public surface with its own auth, and polling two hosts for every open card
has a cost and a lag. The deferred question about the laptop builder receiving a handoff without polling
covers the opposite direction only.
Default: engine wait for event fed by host webhooks, with a poll on a cadence as the fallback for a missed
delivery. Verify runs on the merge event, not on a timer.
Question: webhook, poll, or both? Stakes: the engine choice (which ones take external signals cleanly) and
how long `verifying` lags `merged`.

**I3. Builder failure classes and time budget.**
Missing: a session that never ends, a provider error at start, a rate limit on a pooled account, a session
that ends with a branch whose commit is not on the declared base. The runner's grounding does not show a
per task timeout either, so there is no default to lift.
Default: a time budget per role in the project file; expiry moves the card to `halted` with class
`builder_expired` and whatever trace exists; a provider error at start moves to `returned_empty` with the
error; a branch not descending from the handoff's base is `returned_empty` with the reason.
Question: what is the ceiling on a build, and is it per project? Stakes: unbounded spend on a hung hosted
session while nobody watches.

**I4. R22's action log on the branch pollutes R24's diff.**
Missing: if the builder commits its action log to the pull request branch, the log is in the diff the judge
reads, trips "diff inside declared paths," and lands on the customer's main.
Default: the log is committed on a separate ref the builder also pushes (`refs/phactory/log/<key>`), or
in a path the project file names and the deterministic check excludes, and the judge receives the diff
minus that path. The first is cleaner and needs the branch scoped credential from C1 to cover two refs.
Question: where does the fallback action log live so it never reaches main or the judge? Stakes: AE5 and
every subscription CLI build, since the CLI's transcript is the common case for R22.

**I5. Onboarding is a workflow with waits, and the trivial card has a hand on it.**
Missing: Part 1.9 items 2, 6, 7 and the already owned project branch. Phactory cannot file the trivial card
(it never writes the tracker), cannot choose what it changes (project agnostic), and the card waits for a
human approval unless auto merge is on, so onboarding spans at least two sittings. Re entry needs a saved
partial file, which the contract does not require. Onboarding a project the runner or lanes script still
feeds sets up the double landing failure.
Default: save after every answer; ask for the trivial card's target file as an intake question; emit the
exact card to file and wait for it; onboard asks "is any other system dispatching this board" and refuses
to finish while the answer is yes; the properties that cannot be tested are attestations recorded with
identity.
Question: who files the trivial card, what does it change, and may onboarding finish before it lands?
Stakes: AE6, AE8, R31, and whether the first cold onboarding can complete from a phone at all.

**I6. Project file fields the flows need that R2 does not list.**
Missing from R2 but read by the flows: the ready condition (which status plus label makes a card
eligible; the runner used labels for GitHub and JQL for Jira), the landed status verify compares against,
the closing reference form per host, time budgets per role, the trivial card's target, the path convention
for R22's log, and the project's declared paths convention if it differs from the card default.
Default: add them to R2 and to the intake sequence; each is a one question choice.
Question: which of these are per project configuration and which are fixed by the adapter? Stakes: R35, since
a field the adapter hard codes is the first place a never seen project fails.

**I7. Mid build label removal and card closure.**
Missing: Part 1.8. Default: re read the card at every Phactory caused transition and stop at the boundary;
no cancellation in the first version. Question: finish or cancel a build whose card was closed? Stakes:
builder cost versus a lost branch, and a provider cancel API per adapter.

### Minor

**M1. Binding validation on swap (F4).** Missing: hand edited bindings fail only at dispatch. Default:
validate at dispatch, halt class `binding_invalid`, binding read and recorded per step. Question: does a
swap take effect at the step or at dispatch? Stakes: which judge a post swap score records.

**M2. Scores on partial cards.** Covered under C5's default; stated separately so the eval unit does not
invent "zero." Question: is an absent rubric a null or a fail? Stakes: whether swap comparisons in F4 are
skewed by cards that never had a good block.

**M3. Page bucket mapping.** Missing: which states show under being built, waiting to land, needs you.
Default: Part 1.1's mapping, with every branch state carrying a human action under needs you and
`abandoned` shown once then dropped. Question: does `abandoned` ever leave the page without a tracker
change? Stakes: a needs you list that only grows.

**M4. Two lane labels on one card, or a lane label on a card in a done status.** Default: two labels is
not ready and shown under needs you; done status is not ready and silent. Question: none worth the plan's
time; name it in the adapter's ready rule.

## Part 3. Recommended next steps

Answer in the plan, because they change units, adapters, or the engine spike:

1. C1, who pushes. Fold the answer into the Managed Agents spike so it tests the push path with a branch
   scoped credential, not only the event log.
2. C2, ownership and re dispatch. Write the terminal state list and the idempotency key into the control
   plane unit; it is the first thing the engine spike should prove.
3. C3, approval before queue and the Bitbucket enqueue hand. Rewrite the flowchart and the approval wait
   step; fold the enqueue API into spike two.
4. C4, slot and in flight set, and whether the lease is lifted at all. Two named sets in the plan.
5. C5, the card contract. One short schema section the adapter unit, the eval unit, and the handoff unit
   all cite.
6. I2, host events. Decide webhook plus fallback poll before choosing the engine, since the engines differ
   in how they take external signals.
7. I5 and I6, onboarding as a workflow and the missing R2 fields. These decide whether R35 can pass.

Defer to implementation, with the default above written into the unit that owns it:

* I1 resume trigger and override argument (land verb unit).
* I3 time budgets and failure classes (builder adapter unit; values are configuration).
* I4 action log location (trace unit, after C1 settles credential scope).
* I7 mid build label removal (watch unit).
* M1 to M4.
