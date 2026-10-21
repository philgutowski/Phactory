# U28 explainer: onboarding reads the host and the tracker

U19 built onboarding with five reads it could not yet make, so a live onboarding asked the operator to attest them: that the repository is reachable, that the board exists, that the host requires a check before merge, that the machine identity is held to Slipway's branch namespace, and that the board moves on Slipway's branch and pull request. This card teaches the host and tracker adapters those reads, and teaches `LiveProbe` to call them. The operator now picks boards, labels, and statuses from live lists, and the external gate and durable state checks pass or fail on what the host and tracker say. Where a host cannot answer at all, the attestation U19 built is still the fallback.

## Words used below

- **Ruleset.** GitHub's per repository set of branch rules (forbid pushes, require a check) with a bypass list of actors the rules do not apply to.
- **Branch protection.** GitHub's older, per branch version of the same idea. A repository may use either, or both.
- **Branch restriction.** Bitbucket's per pattern branch rule. Two kinds matter here: a merge check (`require_passing_builds_to_merge`, a pull request may not merge until that many builds pass) and a branch permission (`push`, only the listed users and groups may push).
- **Machine identity.** The credential builders push branches with (KTD6). The host must hold it to the namespace, `refs/heads/slipway/**` for the default branch template.
- **Namespace.** The literal text in front of the branch template's first placeholder (U26), `slipway/` by default.
- **Scratch card.** A throwaway card Slipway files on the board for the board movement check and closes afterwards. Its key is in the scratch branch's name and the pull request, exactly as a real card's would be.
- **Scratch base.** A branch Slipway creates in the namespace for the scratch pull request to target, so the check never opens a pull request against the real base branch.
- **Sweep.** Removing every scratch pull request, branch, and card, whether this run's or a crashed run's.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/hosts/base.py` | Adds `Reading` (one onboarding read: `ok` is True, False, or None when the host cannot answer), `Client.patch` and `Client.delete`, and the eight new names in `INTERFACE`: three reads and five scratch writes. |
| `src/slipway/hosts/github.py` | `read_repository`, `read_required_checks` (rulesets, then branch protection), `read_restriction` (the active rulesets and whether the asking identity can bypass them), and the scratch writes over the REST API. |
| `src/slipway/hosts/bitbucket.py` | The same reads over branch restrictions and the account the token acts as, and the scratch writes (a pull request is declined, Bitbucket's word for closed without merging). |
| `src/slipway/trackers/base.py` | Adds `boards`, `labels`, `statuses`, and `read_board` to `INTERFACE`, the scratch interface, and the scratch card's title. |
| `src/slipway/trackers/github.py` | The listings through `gh project list`, `gh label list`, `gh project field-list`, and `gh project view`. `GitHubScratch` files the scratch card as an issue and closes it as not planned. |
| `src/slipway/trackers/jira.py` | The listings through the project search, label, project statuses, and project reads. `JiraScratch` files the scratch card in the board's first standard issue type and closes it through a transition to a done status. |
| `src/slipway/onboard/checks.py` | `LiveProbe` now calls the reads, builds a tracker for listings from the draft, and runs the board movement check (`_ScratchRun`). |
| `tests/unit/test_onboard_reads.py` | The card's scenarios on captured fixtures and in memory fakes. |
| `tests/fixtures/host/`, `tests/fixtures/tracker/` | The API answers those scenarios read, written from each host's API reference and named in the fixture README. |

## How a read becomes a check

`checks.py` already had the checks and their next steps; U28 changes only what the probe answers. Each probe method asks one adapter read and turns the answer into a `Finding`:

- An answer is `Finding(True, detail)` or `Finding(False, detail)`. False stops the check with the host's own words and the one next step U19 wrote (a ruleset to add, the board workflow to copy).
- `ProbeUndetermined` from the host, or a `Reading` whose `ok` is None, becomes `Finding(None, detail)`, and onboard asks the attestation exactly as U19 built it.
- A failure the operator can fix (an unset credential variable, a repository the token cannot see) is `Finding(False, reason)`.
- An adapter with no such method at all answers None too. That keeps a fake, or an adapter built before this card, on the attestation path rather than crashing.

## The external gate on each host

**GitHub, the required check.** The adapter reads the rules GitHub computes for the default branch from every active ruleset, then the branch's classic protection. A required status check in either is enough. A 404 saying the branch is not protected is an answer (nothing required). A 403 or 404 on both, which is what a private repository on a plan without rulesets returns, is `ProbeUndetermined`.

**GitHub, the restriction.** The adapter lists the branch rulesets (including the organization's) and reads each active one. A ruleset holds the identity when it includes every branch (`~ALL` or `refs/heads/**`), excludes nothing outside the namespace (an excluded `~DEFAULT_BRANCH` or `release/**` would be a branch the identity could push to), and forbids both creation and update. Whether the identity can bypass it is GitHub's answer, not ours: a ruleset read carries `current_user_can_bypass` for the credential that asked, and the adapter asks with the machine identity's own credential. `never` holds it; anything else fails, naming the ruleset. Where GitHub leaves the field out, an empty bypass list still proves it, and anything else is None. This is the shape of the namespace ruleset in `docs/spikes/merge-paths-click-sheet.md`.

**Bitbucket, the required check.** A merge check whose glob matches the main branch, with a count of at least one, reads as "1 passing build" (Bitbucket does not name the builds).

**Bitbucket, the restriction.** A push permission must match the main branch, and the account the token acts as (`GET /user`) must not be among its users. Where Bitbucket will not say who the token acts as, a permission that lists no one proves it, and anything else is None. Bitbucket's globs cannot exclude a prefix, so this read holds the main branch, the one the gate protects, rather than every branch outside the namespace. Reading branch restrictions needs the token's repository admin scope; without it, both reads are None.

## The board movement check

`LiveProbe.board_moves` proves KTD27 on the real board. In order:

1. **Sweep.** Ask the scratch class for open cards titled with the scratch title, close every open pull request into the scratch base and delete its head, delete each leftover card's scratch branch and the scratch base, and close the cards. Only branches under the namespace are ever deleted, so a real card's branch is never touched.
2. **File the scratch card.** It carries no lane label, so watch never dispatches it.
3. **Create the scratch base at the base branch's parent commit,** under a name with no card key, so creating it moves nothing on the board.
4. **Create the scratch branch at the base branch's head,** named by the project's own branch template for the scratch card. The pull request between the two branches then has exactly one commit, and Slipway writes no commit and runs nothing (KTD18).
5. **Read the board** until the card shows the in progress status or the wait ends (three minutes, read every five seconds).
6. **Open the pull request** from the scratch branch into the scratch base, with the tracker's closing reference, and read the board until the card shows the in review status or the wait ends.
7. **Sweep again**, success or failure alike.

A board that moved both times passes durable state. A board that did not fails with the board workflow template (GitHub Projects) or the two Jira automation rules as the next step (AE13). A run that crashed between steps leaves things the next run's first sweep removes. A board seen moving is remembered for the rest of the process, so a recheck of another property does not open a second scratch pull request.

## Why the scratch card is not on the tracker adapter

R34 says tracker adapters are read only, and the U8 tests forbid a filing method on them. The board movement check needs exactly one write, a card it files and closes. So each tracker module has a separate `Scratch` class with exactly three methods (`open`, `close`, `leftovers`), and only the board movement check builds one. The adapter stays read only; the write is visible, small, and named.

## Listings before the project file exists

The board, label, and status questions come before the project file is written, so `LiveProbe` builds the tracker for a listing from the answers so far (`checks.Draft`) and resolves the tracker credential straight from the operator file and the environment. A listing that cannot be read offers nothing, and the question still takes a typed answer, as U19 built it.

## What is not done here

- The live part of the card's verification, one onboarding of a test repository on each host that asks none of the five attestations, is the operator's: it needs a GitHub App installation, a Bitbucket repository access token, and boards on both trackers. The pull request lists the steps.
- Two facts only a live run can settle are written down as assumptions: that GitHub reports `current_user_can_bypass` to an App installation token, and that a Bitbucket repository access token answers `GET /user` with the account its branch permissions name. Where either does not hold, the read answers None and the attestation is asked, so neither can pass a project that should fail.
- The shipped board workflow (`templates/slipway-board.yml`) finds a card from a `slipway/u<n>-` branch, while the default branch template renders `slipway/<key>-<build>`. The board movement check uses the project's own template, so it reports truthfully whether the two agree for that project.
- The U19 explainer's note that `LiveProbe` reads only the tracker and the landing path is now out of date; this file supersedes it.
