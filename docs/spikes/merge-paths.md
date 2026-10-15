# U2 spike: merge paths and machine identities on both hosts

Status: steps 1 and 3 recorded with the required check re probe, steps 4 to 6 open. Started 2026-10-15.
Unit: U2 in `docs/plans/2026-10-14-1730-feat-phactory-first-version-plan.md`.
Source of host facts: `docs/research/2026-10-14-plan/framework-docs-apis.md` Part C.
Consumer: U9 (host adapters) reads this report for the landing path and merge method its probe must find, and U10 (verify landed) reads it for the commit relation.

Cells marked **to record** are measurements the spike still has to take. Everything else is settled as of 2026-10-15.

## Terms used below

- Landing path: the mechanism the host uses to put a pull request's work on `main`. U9 names three: `merge_queue` (GitHub), `auto_merge_strict` (GitHub auto merge with "require branches to be up to date"), `bitbucket_queue`.
- Merge method: how the host builds the commit that lands, as a merge commit (two parents), a squash (one new commit holding all changes), or a rebase (the branch's commits replayed on top of `main`).
- Tree: git's snapshot of every file at a commit, identified by its own hash. Two commits with different hashes can carry the same tree, which is what makes the tree the thing verify compares.
- Confirmed commit: the branch head Phactory confirmed on the remote when it opened the pull request (KTD6).
- Tested commit: on a queue path, the commit the host itself creates and runs CI against (GitHub `merge_group.head_sha`, Bitbucket's merge result commit) (KTD7).
- Machine identity: the credential the builder pushes under. On GitHub a GitHub App installation token (short lived token minted for one installation of an App on a repository). On Bitbucket Cloud a repository access token.
- Ruleset: GitHub's per repository rule set that restricts what can happen to branches, with a bypass list of actors the rules do not apply to.
- Registration token: the fine grained personal access token (a token limited to named repositories and named permissions) that holds Administration write on one repository so a runner instance can register a just in time runner, one that registers, runs one job, and exits (KTD20).
- Webhook: an HTTP POST the host sends to a URL when an event happens. The JSON body is the payload; a saved payload is a fixture U9's tests replay.

## Settled on 2026-10-15, before any probe ran

- `philgutowski/Phactory` is public and owned by a user account. "Require merge queue" is not offered in its rulesets. This matches Part C1: the queue is on GitHub Enterprise Cloud and on public repositories owned by organizations. Landing path is therefore `auto_merge_strict`.
- Repository settings: `allow_auto_merge` on, `allow_update_branch` on, `delete_branch_on_merge` on. All three merge methods allowed.
- Ruleset `main` (id 25166140) is active with rules `deletion`, `non_fast_forward`, `pull_request` (code owner review required, otherwise 0 approving reviews, repository Admin role bypasses always). No required status check yet, on purpose: `ci.yml` does not exist until U5 lands, and a required check that nothing produces would block every merge.
- Fork pull request workflow approval policy is set to all external contributors (KTD25 baseline).
- `Lementknight` is a user account, not an organization. Phillip has push but not admin on `Lementknight/zig-actions-runner`, so U2 steps 1 and 4 cannot run there under his account. Step 3 also needs admin (auto merge is a repository setting). Decision: U2 runs on Phactory only; the runner repository is recorded as **owner must confirm**.

## Repository 1: `philgutowski/Phactory` (GitHub, user owned, public)

| Item | Finding |
|---|---|
| Landing path | Settled: `auto_merge_strict`. Queue not offered. U9's `probe_landing` must return `auto_merge_strict` for this repository from the live ruleset. |
| Merge methods to probe | Done 2026-10-15, all three probed with real spike content, see Step 3 results below. Phactory's own project file should pin `squash`: one landed commit per card, same tree as the head, and the landed hash is the pull request's `merge_commit_sha`. |
| Commit relation verify checks | Proved 2026-10-15 for all three methods: `tree_of(main tip after merge) == tree_of(confirmed commit)`, and `pull_request.merge_commit_sha` equals the `main` tip in every case, including squash and rebase where the research was silent. Never compare the landed hash with the confirmed hash on this path; only the merge commit method keeps the confirmed commit as a parent. Hashes in Step 3 results. |
| Machine identity excluded from `main` | **To record** (step 4): a push to `main` with the App installation token is refused by the second ruleset; a branch create, update, and delete under `refs/heads/phactory/**` with the same token succeeds. Also record whether a RepositoryRole bypass entry for Write treats the App as a Write role member (if it does, the App is not excluded and the bypass list must drop Write). |
| Registration token scope held | **To record** (step 4, last sentence): a fine grained token with Administration write only, limited to this repository, succeeds at just in time runner registration, and the same token is refused on a content write (proof the scope is as narrow as KTD20 says). |
| Tracker automation delays | **To record** (step 6): seconds from the `pull_request` closed delivery timestamp to the linked issue closing, and from that close to the Projects item reaching Done. Part C5: "Item closed" and "Pull request merged" are on by default; the merged workflow moves the pull request item, not the issue; the issue moves only through a closing keyword into the default branch and then "Item closed". No built in workflow fires on branch created or pull request opened, so for GitHub Projects only the done step is native and KTD27's Actions template owns the other two. Out of U2's scope to build; record that the gap exists. |
| Webhooks to capture here | `create` (step 4 branch creation), `pull_request` synchronize (second push), `pull_request` closed with `merged: true`. Step 3 ran before any webhook receiver existed, so the closed payload is captured on a later pull request once the U4 host or a tunnel is up. `merge_group` cannot be captured on this repository because it has no queue; see the fixture list. |

## Repository 2: `Lementknight/zig-actions-runner` (GitHub, user owned, owner must confirm)

| Item | Finding |
|---|---|
| Landing path | **Owner must confirm.** Expected `auto_merge_strict` by the same Part C1 plan rule as Phactory (user owned, not an organization), but only an admin can open the rulesets page to confirm "Require merge queue" is absent, and only an admin can turn `allow_auto_merge` and `allow_update_branch` on. |
| Merge methods to probe | **Owner must confirm** which methods the repository allows. Phillip can read `allowed merge methods` from the repository API without admin; record the read, not a probe. |
| Commit relation verify checks | Same as Phactory once the landing path is confirmed: tree equality between `main` tip and confirmed commit under auto merge. If the owner later moves the repository under an organization with a queue, the relation changes to the queue form (landed tip equals tested commit, tested commit's pull request head equals confirmed commit) and this row is rewritten. |
| Machine identity excluded from `main` | **Owner must confirm.** Phactory's involvement here is the filing agent only (KTD20), under a GitHub App with Issues read and write and no content permission, so no branch namespace ruleset is needed for Phactory's identity on this repository. Record that as the reason the cell stays empty if the owner declines. |
| Registration token scope held | Not applicable here. The runner instance registers on the repositories whose jobs it runs, not on the runner's source repository. |
| Tracker automation delays | Not applicable for U2. The runner repository's issues are filed by Phactory's agent; no board automation is in scope. |

## Repository 3: Bitbucket Cloud, `<workspace>/<repository>` (placeholder, Standard workspace)

| Item | Finding |
|---|---|
| Landing path | Expected `bitbucket_queue` from Part C2: merge queues are on Standard and Premium workspaces, Pipelines must be enabled, not on forks, GA 2026-06-30. **To record:** the workspace plan as shown in the UI, that Pipelines is enabled, and that the queue setting is visible on this repository. If any of the three fails, the fallback is a plain approve and merge with no queue, and verify falls back to the tree relation; record which. |
| Merge methods to probe | Part C2: Merge commit, Squash, Rebase + merge. **To record:** which strategy the queue is configured with (one per queue; the strategy is a queue setting, so probing all three means reconfiguring the queue between test pull requests). |
| Commit relation verify checks | KTD7 queue form: (a) `main` tip after merge equals the queue build's commit (the merge result commit the queue tested), and (b) that tested commit's tree equals the confirmed commit's tree, and (c) `pullrequest:fulfilled` `merge_commit.hash` equals the `main` tip. Part C2 does not say in so many words that the tested SHA is the SHA that lands, so (a) is the measurement that decides whether verify can use hash equality here or must drop to the tree relation. **To record:** the queue build commit hash, `merge_commit.hash`, the `main` tip, and both tree hashes, per strategy probed. |
| Machine identity excluded from `main` | **To record** (step 5): branch permissions on `main` exclude the repository access token so a push to `main` under it is refused while a push under `phactory/*` succeeds. Risk row in the plan: if Bitbucket branch permissions cannot name a repository access token as excluded, record it and the project runs in the degraded mode the risk table names. Also record that the queue's temporary branches `bitbucket-merge-queue-*` are not blocked by the same branch restrictions (Part C2 requires this or the queue never builds). |
| Registration token scope held | Not applicable. The Zig runner is GitHub Actions only. |
| Tracker automation delays | **To record** (step 6): seconds from branch created to In Progress, pull request created to In Review, `pullrequest:fulfilled` to Done, read from the Jira issue history against the webhook delivery times. Part C4: the merged trigger exists under DevOps triggers and needs a connected cloud SCM; keying is the upper case issue key in the pull request title or source branch name. |
| Enqueue with no click | **To record** (step 5): after approval with builds green, does the pull request enter the queue on its own under "automatically queued once builds/checks pass", or does it still need the "Add to merge queue" button. Part C2 says there is no REST endpoint to enqueue (BCLOUD-24176 is gathering interest), so if a click is needed, KTD7's needs you entry names that click. |
| Webhooks to capture here | `pullrequest:fulfilled` (Part C3: carries `merge_commit.hash`, `destination.commit.hash`, `source.commit.hash`, `state` MERGED). The build status event for the queue's commit: Part C3 says no merge queue specific events exist, and Part C does not name the commit status event; its name is not in the research, verify in the webhook subscription UI and record the exact event key. |

## Step 3 results, Phactory, 2026-10-15

Three pull requests, each carrying real spike content, merged through GitHub under one method each. "Confirmed" is the branch head Phactory would have confirmed; "landed" is the `main` tip after the merge.

| Method | PR | Confirmed head | Confirmed tree | Landed tip | Landed tree | Parents of landed tip | `merge_commit_sha` equals landed tip | Tree equal |
|---|---|---|---|---|---|---|---|---|
| merge commit | #29 | `98356e8` | `4a15e3e` | `0855ca3` | `4a15e3e` | `d22b491`, `98356e8` | yes | yes |
| squash | #30 | `431936e` | `9901187` | `c5c264f` | `9901187` | `0855ca3` | yes | yes |
| rebase | #31 | `08b4372` | `4d89003` | `8f36ec3` | `4d89003` | `c5c264f` | yes | yes |

Two findings beyond the table:

1. Auto merge is not a waiting state until something blocks the pull request. With no required status check on the ruleset, `gh pr merge --auto` on #29 merged at once and left no auto merge event on the timeline, and on #30 GitHub refused `enablePullRequestAutoMerge` with "Pull request is in clean status", so #30 was merged through the host directly. "Require branches to be up to date" is likewise a property of the required status check rule, not a repository setting, so it is not enforced until `ci` becomes required after U5. Consequence for verify and for U9's probe: the landing path on this repository is `auto_merge_strict` only once a required check exists; before that it is a plain merge. U5's acceptance must re run one probe after the check is required.
2. The code owner review rule is inert until a `CODEOWNERS` file exists (U5), so the KTD25 review gate is not yet live either.

### Re probe after `ci` became the required check, 2026-10-15

Once U5 landed, the `main` ruleset gained `required_status_checks` with context `ci` and the strict (up to date) policy. Observed on the next two cloud cards:

- #35 (U7) and #34 (U6) were both green and both based on the same `main` tip. #35 merged first by squash. Within twenty seconds #34 reported `BEHIND`, so "require branches to be up to date" is now enforced. `gh pr update-branch 34` merged `main` into the branch and #34 moved to `BLOCKED` until `ci` ran again on the new head.
- Auto merge could be enabled on #34 while `BLOCKED`, and it is now a waiting state: GitHub merges when `ci` passes, with no further click. Before the required check existed, enabling auto merge on a green pull request was refused as "clean status" (Step 3 results, finding 1).
- U9's probe should therefore read `auto_merge_strict` from the live ruleset only when a `required_status_checks` rule with the strict policy is present; a repository with auto merge on but no required check is a plain merge path, and verify must say so rather than wait for a merge that will never be queued.
- KTD25 on a solo repository: #34 touched `CODEOWNERS` paths, and GitHub still showed it `CLEAN` for the operator, because the operator is the ruleset's Admin bypass actor and a pull request author cannot approve their own pull request. The review is the operator's merge decision, taken after reading the diff, not a GitHub approval event. Record this where U9 and the status page describe "waits for review".
- Measured 2026-10-15: auto merge does not use bypass. #43 (docs only) auto merged about two and a half minutes after its check went green. #34 (`CODEOWNERS` paths), green at 15:12:58Z with auto merge enabled since 15:08:38Z, had not merged at 15:17:41Z and was merged by hand under the Admin bypass. So on this repository a pull request that touches a KTD25 path is always a hand merge, and a pull request that does not is hands free once `ci` passes. U9's `probe_landing` and the status page should state "waits for the operator's merge" rather than "waits for review" for these paths.
- Measured 2026-10-15: with strict checks and no queue, two green pull requests do not land hands free. The second drops to `BEHIND` when the first merges, GitHub's auto merge does not update the branch, and `gh pr update-branch` plus a fresh `ci` run is needed before it lands (#34 twice, after #35 and after #43). KTD7's needs you entry for the GitHub auto merge path is therefore "update branch", about once per concurrent sibling; a merge queue removes it, and this repository cannot have one.

## Notes against the unit text

- U2 step 2 (queue probe on GitHub) has no repository to run on. Neither GitHub repository offers a queue under its current ownership, so the `merge_queue` landing path and its `merge_group` relation stay unverified by this spike. U9's queue scenario is built from the octokit example payload instead of a live capture.
- U2 step 1 names two repositories; one is admin blocked. Recorded above as owner must confirm.
- U2 step 6 names three Jira triggers; Part C4 confirms only "Pull request merged" from the primary docs page, and names "five DevOps triggers" across Bitbucket, GitHub, GitLab from an Atlassian community article. Branch created and pull request created are to be confirmed in the Jira automation UI.
- U2 step 6 says the Projects "Item closed" workflow fires on merge. Part C5 says it fires on the issue closing, which a merge into the default branch causes through a closing keyword. Verify must therefore read the issue's closed state, not the merge event, as the Projects signal. Whether a queue merge counts is moot on Phactory (no queue).

## Fixtures (U9 inputs, from the unit's Files line)

| Fixture | Planned source | Captured |
|---|---|---|
| `tests/fixtures/host/github_pr_closed.json` | Step 3 on Phactory: `pull_request` delivery with `action: closed`, `merged: true`, after the merge commit method run. | captured: no |
| `tests/fixtures/host/github_pr_synchronize.json` | Step 4 on Phactory: `pull_request` delivery with `action: synchronize` after the second push to the App created branch. | captured: no |
| `tests/fixtures/host/github_merge_group.json` | No queue on either GitHub repository. Source is the GitHub owned octokit example `merge_group/checks_requested.payload.json` named in Part C1, saved verbatim and marked as an example, not a live capture. | captured: no |
| `tests/fixtures/host/github_create.json` | Step 4 on Phactory: `create` delivery when the App token creates `refs/heads/phactory/u2-test`. | captured: no |
| `tests/fixtures/host/bitbucket_fulfilled.json` | Step 5 on the Bitbucket repository: `pullrequest:fulfilled` delivery after the queue merges the test pull request. | captured: no |

Each fixture is saved with its delivery id and the signature header kept, since U9's verify test replays the HMAC check against it. Secrets used during capture are rotated before the fixture is committed.
