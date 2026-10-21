# The build loop: how Slipway builds itself in the cloud

Slipway is a service for handing cards to coding agents and landing their work. Until its own workflow (U14) and watch (U25) exist, something else has to hand Slipway's cards to agents and land them. This page is that something. It is the plan's Build Process section made mechanical: a ready rule, a handoff, a dispatcher, a gate, and a model review that is the review. Everything here lives in `factory/`, `.claude/skills/dispatch/`, and `.github/workflows/review.yml`.

## Words used below

- **Card.** One GitHub issue on this repository, titled `U<n>: ...` for a plan unit or `U<n> follow up: ...` for a follow up, carrying the lane label (`cloud` or `operator`) and the fenced `slipway` block with `good`, `paths`, and `test`.
- **Conductor.** The session that runs the dispatch procedure: reads the board, decides which cards are ready, posts the handoff, creates the branch, and starts a builder session. Today it is a Claude Code session, scheduled or a person's.
- **Builder.** The Claude Code cloud session that builds one card from its handoff, opens the pull request, and watches it until it merges.
- **Handoff.** The one message the builder starts from: base commit, branch, rules, files other cards hold, and the done line.
- **In flight.** A card is in flight from the moment its branch exists until its pull request merges. Its declared `paths` are held: no other card may be dispatched on them.
- **Gate.** The `ci` check: ruff, pytest with the integration suite required, and the import rule test.
- **Review check.** The `review` check: a model reads the diff against the card and the brief and blocks or passes.
- **Print mode.** Claude Code run as a command (`claude -p`) with a prompt on standard input and a JSON answer on standard output, no terminal, no session.

## One card, start to finish

```
conductor                        builder session                    GitHub
=========                        ===============                    ======
read issues, pulls, branches
ready rule picks a card
render handoff
post handoff on the issue
create slipway/u<n>-<slug>   ..................................>  board: In progress
start session(handoff)   ....>  checkout the branch
                                build, run the gate locally
                                push commits
                                open pull request, Closes #n  ...>  board: In review
                                enable auto merge                   ci runs the gate
                                                                    review reads the diff
                                fix blocking findings, push   ...>  both checks run again
                                                                    auto merge, squash
                                                                    issue closes, board: Done
```

## The ready rule, `factory/handoff.py`

`ready(issues, pulls, branches)` takes the three REST listings as plain JSON and returns a verdict for every open cloud card. A card is ready when its block parses, every unit on its Dependencies line is closed, no branch exists under its name and no open pull request closes it, and none of its declared paths overlaps a path an in flight card declares. Each failed condition is a reason in the verdict, so the conductor's report says why a card waits.

Two details are worth knowing:

- **A branch is the dispatched mark.** The conductor creates the branch before starting the session, so a second conductor running a minute later sees the card as dispatched. This is the same move Slipway makes at dispatch (KTD27), and it is what moves the board to In progress.
- **An operator spike whose findings are recorded but whose issue stays open** is passed by name with `--assume-done U2`. The rule never guesses that a spike is done. Today two are passed, each with its own `--assume-done`: U2, whose merge method findings U9 needs while steps 4 to 6 stay open, and U4, whose Hatchet findings (`docs/spikes/hatchet.md`) U14 needs while step 1, sizing on the VPS, stays open.

`render` writes the handoff in the plan's shape, five numbered items then the card body. It differs from Slipway's own eight item handoff (`src/slipway/builders/handoff.py`) in three ways: the builder opens its own pull request, because there is no Slipway to open it yet; there is no credential item, because the cloud session holds its own; and there is no side reference item, because the session cannot push outside its branch.

## The dispatcher, `.claude/skills/dispatch/SKILL.md`

The conductor is a procedure, not a program, because the step that starts a cloud session is a Claude Code tool (`create_session`), reachable from a session and not from a shell. The skill tells a session exactly what to run and in what order, with two caps: at most three cards in flight and at most two new dispatches per run. The caps are there for one reason. Each in flight card holds its declared files, and each pull request that lands moves `main` under the others, so more in flight means more held files and more rebases, not faster landing.

A scheduled task runs the skill on a cadence. It fires into a standing conductor session rather than a fresh one, because a fresh session started by a scheduled task has neither the `create_session` tool nor GitHub access to the repository; the first live run on 2026-10-20 stopped at step 0 for that reason. Pausing the task pauses the factory; the branches, pull requests, and sessions already started carry on.

Two more things the first live runs taught. The ruleset requires a branch to be up to date with `main`, so each landing puts every other open pull request behind; the builder merges `main` into its branch and pushes, and the conductor updates the branch of any idle builder's pull request, at the cost of one more review run each time. And the ready rule checks a card only against cards already in flight, so the conductor checks the two cards it picks in one run against each other.

## Asking the operator

The operator is one person, and a factory that stops to ask is a factory that waits. So every session follows the rule in `CLAUDE.md`: decide what you can and write the decision down, keep working on everything that does not need the operator, and when only the operator can supply something (a credential, a device, a paid account, a setting behind the operator's login), ask for that one thing in a single step, as multiple choice with a recommendation first when the answer is one of a few options.

## The review check, `.github/workflows/review.yml` and `factory/review.py`

Auto merge waits for the required status checks and for nothing else. `ci` proves the gate is green; it says nothing about whether the change does what the card asked. The review check is the second required check and the answer to that question, and it is the whole review: no person approves a pull request before it lands.

The workflow is the same two step shape as `ci.yml`, one checkout and one run step. The run step installs the Claude Code CLI and runs `factory/review.py`, which:

1. Reads the pull request, finds the card it closes, and reads the card's block.
2. Takes the diff and the list of touched files, and settles the facts a script settles better than a model: files outside the card's declared paths, files another in flight card declares (in flight by the ready rule's own test, `handoff.dispatched`, so a bare branch under a card's name holds its files before its pull request exists), added lines or commit messages with an em dash or en dash, and added lines with a bare double hyphen for the model to judge as flag or punctuation.
3. Writes one prompt: `factory/review-brief.md`, then the card, the facts, then the diff (inline up to 400 KB, otherwise in a file the model reads in parts).
4. Runs `claude -p` with the reviewer model, read only tools (`Read`, `Glob`, `Grep`, and `git diff`, `git log`, `git show`), a JSON schema for the answer, and a dollar cap.
5. Posts the verdict as a pull request review (a comment, never an approval: approving is the operator's) and exits 1 when any finding is blocking.

A review that could not run is red, never green. No key, a provider error, or an answer outside the schema all fail the check with the reason in the review body, because a check that passes without having reviewed anything is no check.

### The brief

`factory/review-brief.md` is the reviewer's written definition of a good review, the same idea as a card's `good`. It names the ten things that block (the card's `good` not met, a defect with a traced path, the first law, KTD18, tests reaching the network or weakened, a secret, dash punctuation, a file another card holds, no explainer or undefined jargon, no `Closes #n`) and says everything else is advisory and gets one line. A change to the brief changes what lands, so `factory/` is a CODEOWNERS path and the operator reads every change to it.

### What CODEOWNERS still does

The ruleset does not require a code owner review; the operator decided on 2026-10-20 that a model review plus a green gate is the merge gate on this repository. `CODEOWNERS` stays, because GitHub still asks the named owner to review a change under those paths, which is the right notification: a change to the gate, the deploy files, or a credential path is one the operator reads first once it lands, with the model's verdict and findings already on the pull request.

## Which model builds and which reviews

The reviewer is **Claude Fable 5.1** at effort high. The builder is **Claude Opus 5.5** by default, and Fable 5.1 for the units whose correctness rests on concurrency, authorization, or a credential (U9, U14, U25, U18, U19, U22).

The reasoning:

- **The reviewer should be at least as strong as the builder and should not share its context.** A model reviewing its own session confirms its own assumptions. The review check runs in a fresh process with only the diff, the card, and the brief, under the strongest model available, so the second reading is genuinely a second reading.
- **The builder spends the tokens.** A build runs for an hour over many turns; a review reads one diff. Putting the stronger, costlier model on the smaller job buys the most judgment per dollar. At list prices a review of a typical card costs under three dollars and a build runs to tens of dollars, so moving every build to Fable would roughly double the bill for a gain the review already captures on ordinary cards.
- **The hard cards get the stronger builder anyway**, because a defect in the workflow's claim before act guard or in the token verifier is the kind a review finds late and a builder avoids early.

Three things would change this choice, and each is a one line change: `SLIPWAY_REVIEW_MODEL` moves the reviewer, the model list in the dispatch skill moves the builders, and `SLIPWAY_REVIEW_EFFORT` moves the reviewer's depth.

## Setup, once, by the operator

1. Add the repository secret `CLAUDE_CODE_OAUTH_TOKEN`, a subscription token printed by `claude setup-token` on a machine signed in to the operator's Claude account, so the reviewer runs on the subscription. `ANTHROPIC_API_KEY` is the alternative for API billing. Until one exists, the review check is red on every pull request, including the one that adds it.
2. Add `review` to the ruleset's required status checks beside `ci` (Settings, Rules, `main`, Require status checks to pass), and turn off "Require review from Code Owners" on the same ruleset. From that moment auto merge waits for the model review on every pull request and for no person.
3. Optionally set the variables `SLIPWAY_REVIEW_MODEL`, `SLIPWAY_REVIEW_EFFORT`, `SLIPWAY_REVIEW_BUDGET`, and `SLIPWAY_REVIEW_RUNS_ON`.

The Admin role bypasses the ruleset, as U2 recorded, so the operator's own merge button skips both checks. Nothing here changes that.

## Where this differs from the plan

- **The builder opens its own pull request.** The plan's handoff shape says Slipway opens it; the cloud card handoff in the Build Process section says the session does, and this page follows the Build Process section.
- **A model review is a required check.** The plan has the gate and the KTD25 human review only. The review check adds a reading of every change against its card before auto merge, which the plan's Build Process assumed a person would do when needed and nobody had time to do on every card.
- **No person approves before the merge.** KTD25 has the operator approve a change on the credential and gate paths before it lands. The operator replaced that with the review check on 2026-10-20: a model review of every change, with `CODEOWNERS` kept as the request to read those paths first after they land.
- **`factory/` is a CODEOWNERS path.** KTD25 does not list it, because it did not exist; it is the review gate's own code, so it joins `.github/`.
- **The review job stays on a hosted runner by default.** `ci.yml` moves to an operator runner by variable; the review needs Node and a route to the Claude API, so it has its own variable and moves only when the instance has both.
