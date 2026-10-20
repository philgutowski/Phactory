# U2 click sheet: steps 4, 5, 6 by hand

For Phillip. Each step says what to do, what to copy down, and where the research backs it. Where Part C of `docs/research/2026-10-14-plan/framework-docs-apis.md` says nothing about a screen, the line reads "not in the research, verify in the UI" and names the outcome to look for instead of a menu path. Record every measurement in `docs/spikes/merge-paths.md` in the cell marked to record.

Rule for the whole sheet: on Slipway, Claude holds a standing go (2026-10-15) and runs every API write listed here without asking. Phillip's hands are needed only where a credential is created or a passkey is confirmed, and on hosts and accounts that are not his.

Terms on first use: a GitHub App is an identity GitHub issues to a program, separate from any user, that is installed on chosen repositories and acts there with only the permissions it declares. An installation token is a one hour token minted for one such installation. A fine grained personal access token is a user token limited to named repositories and named permissions. A ruleset is GitHub's per repository rule set over branches with a bypass list of actors it does not apply to. A webhook is an HTTP POST the host sends to a URL when something happens; its JSON body is the payload.

## Step 4: GitHub App on Slipway, namespace ruleset, registration token

Goal (U2 step 4): install a test GitHub App on `philgutowski/Slipway`, mint an installation token, prove a ruleset refuses that identity on `main` and allows it under `refs/heads/slipway/**`, capture the `create` and `pull_request` synchronize payloads, and prove a fine grained token with Administration write alone registers a just in time runner.

### 4.1 Create the App

1. Sign in to GitHub as `philgutowski`. Open your account's developer settings and start a new GitHub App owned by your user account. Not in the research, verify in the UI.
2. Name it so it is obviously a test identity, for example `slipway-dev-pgutowski`. The name must be unique across GitHub. Not in the research, verify in the UI.
3. Homepage URL: `https://github.com/philgutowski/Slipway`.
4. Webhook: leave Active on. For the URL, pick one of two options and write down which. Option A, a temporary public receiver you control (a tunnel to a local listener, or a hosted request catcher) so raw payloads can be saved to files. Option B, a placeholder URL that returns 404; the App's own delivery history page still stores each payload and lets you read and redeliver it, which is enough to copy the JSON by hand. Not in the research, verify in the UI that the delivery history shows the full body. Set a webhook secret and store it under the name the operator file will use (R3); never paste it into the repo.
5. Repository permissions. The plan does not list them in one place; this list is derived from U9's adapter interface (create branch, confirm commit, open pull request, post check, read pull request) and KTD6. Set exactly:
   - Contents: Read and write (create and delete refs, read commits and trees).
   - Pull requests: Read and write (open pull requests, read state and approvals).
   - Checks: Read and write (post the `slipway/landing` check on any commit).
   - Metadata: Read (GitHub adds this on its own).
   - Everything else: No access. In particular no Administration and no Workflows. Administration stays on the separate registration token (KTD20). Note for later: a builder branch that edits `.github/workflows/` will be refused by GitHub without Workflows write; out of U2's scope, record it as an open item.
6. Subscribe to events: `Create`, `Pull request`, `Check run`, `Check suite`. Also tick `Merge group` if offered, for completeness; it will never fire on this repository. The event list GitHub offers depends on the permissions above; not in the research, verify in the UI which appear.
7. Where can this App be installed: only on this account.
8. Create the App. On the App's settings page, copy the App ID and the Client ID into your notes.

### 4.2 Private key and installation

9. On the same settings page, generate a private key. GitHub downloads a `.pem` file once and never shows it again. Move it to the path the operator file will name, mode 600, outside any repository. Not in the research, verify in the UI.
10. Install the App on your account and choose only `philgutowski/Slipway`. After installing, the browser lands on the installation's page; the installation id is the number at the end of that page's URL. Not in the research, verify in the UI. Cross check: Claude can read it on your go with `GET /app/installations`, which needs a JWT (a short lived signed token built from the App ID and the private key) rather than your user token.
11. Hand Claude the App ID, installation id, and the key path. Claude mints an installation token (`POST /app/installations/{installation_id}/access_tokens`) on your go and keeps it in memory only. Endpoint from GitHub's REST docs, not from Part C; verify the response carries `token` and `expires_at` about one hour out.

### 4.3 The namespace ruleset (Claude runs on your go)

12. Before this runs, confirm you are comfortable that a second active ruleset applies to every identity on the repository except the bypass list. Rulesets restrict everyone not listed as a bypass actor; there is no way to target one App negatively. So the humans go on the bypass list and the App does not. The body Claude will send to `POST /repos/philgutowski/Slipway/rulesets`:

```json
{
  "name": "slipway-namespace",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["~ALL"],
      "exclude": ["refs/heads/slipway/**"]
    }
  },
  "rules": [
    { "type": "creation" },
    { "type": "update" },
    { "type": "deletion" }
  ],
  "bypass_actors": [
    { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always" },
    { "actor_id": 4, "actor_type": "RepositoryRole", "bypass_mode": "always" }
  ]
}
```

    Reading it: every branch except those under `slipway/` is closed to creation, update, and deletion, for everyone except repository Admin (role id 5) and Write (role id 4). The existing ruleset `main` (id 25166140) stays as is and keeps `deletion`, `non_fast_forward`, and the pull request rule on `main`. The role ids are the values GitHub's rulesets API documents for RepositoryRole; they are not in Part C, so if the API rejects them Claude lists the repository's roles first and corrects the ids before retrying.

13. The question the spike answers here: does GitHub treat an App installation as a Write role member for bypass purposes? If the test in 4.4 shows the App pushing to `main` with this body in place, drop the `actor_id: 4` entry and run 4.4 again. On a solo repository the only human with push is you, and you are Admin, so dropping Write costs nothing.

### 4.4 Prove the exclusion and capture the payloads

14. With the installation token, Claude runs these in order, each on your go, and you watch the webhook deliveries:
    - Create `refs/heads/slipway/u2-test` at the current `main` tip (`POST /repos/philgutowski/Slipway/git/refs`). Expect success. Save the `create` delivery as `tests/fixtures/host/github_create.json`.
    - Push a one file commit to that branch under the token. Open a pull request from it to `main`. Push a second commit. Save the `pull_request` delivery with `action: synchronize` as `tests/fixtures/host/github_pr_synchronize.json`. Record both head hashes.
    - Try to update `refs/heads/main` to that branch's head with the token (`PATCH /repos/philgutowski/Slipway/git/refs/heads/main`). Expect a refusal naming the ruleset. Record the HTTP status and the message verbatim.
    - Try to create `refs/heads/not-slipway/u2-test` with the token. Expect refusal. Record it.
    - Delete `refs/heads/slipway/u2-test` with the token after the pull request is closed. Expect success.
15. Close the test pull request without merging. Delete the test branch if the delete above was skipped.

### 4.5 Registration token scope

16. Create a fine grained personal access token on your account, resource owner `philgutowski`, repository access limited to `Slipway` only, repository permissions Administration: Read and write, nothing else, expiry as short as the UI allows. Not in the research, verify in the UI. Store it under the operator file's registration token name.
17. Claude runs, on your go, `POST /repos/philgutowski/Slipway/actions/runners/generate-jitconfig` with a name under the instance prefix and the labels the instance will use. Endpoint from GitHub's REST docs for self hosted runners, not from Part C; verify the response carries `encoded_jit_config`. Record success as "registration token scope held".
18. Negative check with the same token: attempt to create a ref (`POST /repos/philgutowski/Slipway/git/refs`). Expect a refusal, which proves Administration write alone does not grant content write. Record it. Discard the runner registration (it was never started, so it expires on its own; verify in the UI that no runner is listed).

## Step 5: Bitbucket Cloud, repository access token, branch permissions, merge queue, webhooks

Goal (U2 step 5): on a repository in a Standard workspace, create a repository access token, prove branch permissions on `main` exclude it, turn the merge queue on with automatic queueing after builds pass, approve a test pull request, record whether it enqueues with no click, capture `pullrequest:fulfilled` and the build status event for the queue commit, and compare `merge_commit.hash` with the queue build's commit.

### 5.1 Preconditions (Part C2)

1. Pick the repository and write its `workspace/repository` into `merge-paths.md` in place of the placeholder. It must not be a fork.
2. Confirm the workspace plan is Standard or Premium. Part C2 names the plans; where the plan shows in the UI is not in the research, verify in the UI (workspace settings, plan or billing area).
3. Confirm Pipelines is enabled on the repository. Part C2 requires it for the queue.
4. Confirm a `bitbucket-pipelines.yml` exists on `main` with at least one step that runs on pull requests and one `merge-queues:` entry. Part C2: under `pipelines:` add `merge-queues:` with a key that must match the queue name in the UI, for example `mergequeue-main`. If the file lacks the entry, add it on a branch first and merge it by hand before the test, so the test pull request is not also the pipeline change.

### 5.2 Repository access token

5. Create a repository access token on the repository with scopes for repository read and write and pull request read and write. The scope names and the screen are not in the research, verify in the UI; record the exact scopes you ticked. Store the token under the operator file's host credential name.
6. Note the token's display name; branch permissions refer to users and groups, and the spike must learn whether a repository access token appears as a selectable identity there.

### 5.3 Branch permissions on `main`

7. Open the repository's branch permissions and add or edit the rule for `main`. Set write access so that only you (and any human maintainers) can push, and merge via pull request only. Where the identity picker is and whether the access token is offered as a name are not in the research, verify in the UI. Record one of three outcomes: the token can be named as excluded, the token is excluded because the write list is an allow list it is not on, or the token cannot be excluded (plan risk row: degraded mode, record it).
8. Add a second rule for the pattern `slipway/*` that permits the token to push. If the token cannot be named at all, record that `slipway/*` is left unrestricted and the exclusion rests on `main` alone.
9. Part C2 requirement: the queue's temporary branches `bitbucket-merge-queue-*` must not be blocked by branch restrictions. Check your rules do not match that pattern. Record the check.

### 5.4 Merge queue

10. Turn the merge queue on for `main` and set the queue name to match the `merge-queues:` key from 5.1 (`mergequeue-main` in the example). Part C2 names the feature and the key matching rule; the settings screen's location is not in the research, verify in the UI.
11. Set the merge strategy. Part C2 lists Merge commit, Squash, Rebase + merge. Start with Merge commit and record it; the strategy is a queue setting, so a second probe under Squash means changing it here between test pull requests.
12. Turn on "automatically queued once builds/checks pass" (Part C2 wording; the toggle label in the UI may differ slightly, verify in the UI and record the exact label).

### 5.5 Webhook subscription

13. Add a repository webhook pointing at the same receiver you used in step 4 (or a placeholder; Bitbucket keeps a request log per webhook that shows the body, not in the research, verify in the UI). Subscribe to `Pull request: Merged` (the event key is `pullrequest:fulfilled`, Part C3) and to the commit status events (the names are not in the research, verify in the UI; they are the events a Pipelines build posts as it starts and finishes). Set a secret and store it under the operator file's webhook secret name for this repository.

### 5.6 The test pull request

14. Create a branch `slipway/u2-test` from `main` using the repository access token (Claude runs it on your go, or you do it with the token in a local clone). Record whether the push succeeded (this is the positive half of the exclusion test).
15. With the same token, try to push a trivial commit to `main`. Expect refusal. Record the message verbatim (negative half).
16. Open a pull request from `slipway/u2-test` to `main` with the Jira key in the title (upper case, Part C4), for example `IW-000 U2 merge path probe`. Use a real key on a throwaway Jira issue so step 6 can run on the same pull request.
17. Wait for the pull request build to go green. Approve the pull request as yourself.
18. Watch the pull request page. Record one of: it enters the queue on its own within a minute, or it shows an "Add to merge queue" button that still needs a click (Part C2 names the button). This is the "enqueue with no click" cell.
19. When the queue build starts, open the Pipelines page for that build and record the commit hash it ran against (the merge result commit, Part C2). Record its tree hash afterwards by fetching the commit.
20. When the queue merges, record the `main` tip hash. From the webhook log, save the `pullrequest:fulfilled` body as `tests/fixtures/host/bitbucket_fulfilled.json` and copy `merge_commit.hash`, `destination.commit.hash`, `source.commit.hash` from it (Part C3 names the fields). Save the commit status body for the queue commit beside it for U9's `queue.requested` mapping. Compare: queue build commit, `merge_commit.hash`, `main` tip. Equal means verify can use hash equality on this host; unequal means verify drops to the tree relation and the report says so.
21. Repeat 11 to 20 under Squash if time allows; record it as a second row. Rebase + merge is optional for U2.

## Step 6: Jira rules and the GitHub Projects workflow

Goal (U2 step 6): confirm Jira rules for branch created, pull request created, and pull request merged move a card through In Progress, In Review, Done, confirm the Projects "Item closed" workflow fires when a pull request merges, and record the delay of each.

### 6.1 Jira preconditions (Part C4)

1. The Jira site must have the SCM connected as a cloud integration (Bitbucket Cloud linked to the site, or the GitHub for Jira app for GitHub). Part C4: the DevOps triggers need a connected cloud SCM and do not support self hosted tools. Confirm the development panel on a test issue shows branches and pull requests; where the integration page lives is not in the research, verify in the UI.
2. The target project's workflow must contain statuses named In Progress, In Review, and Done, with transitions reachable from To Do in that order. Not in the research, verify in the project's workflow editor and record the exact status names, because the automation action names them.
3. Create one throwaway issue and note its key. Keys must be upper case in branch names and titles (Part C4).

### 6.2 Three automation rules

4. Open project automation and create rule one. Trigger: the DevOps trigger for a branch created. Part C4 confirms the trigger family under "DevOps triggers" and names only "Pull request merged" from the primary docs; the Atlassian community article names five triggers across Bitbucket, GitHub, GitLab. If a branch created trigger is not offered, record that and fall back to the pull request created trigger moving the card to In Progress. Action: transition issue to In Progress. Save and turn on.
5. Rule two. Trigger: pull request created. Action: transition issue to In Review. Save and turn on.
6. Rule three. Trigger: pull request merged (Part C4: "The flow executes when a pull request is merged", smart value `{{pullRequest}}`). Action: transition issue to Done. Save and turn on.
7. For each rule, record the rule's audit log location; Part C4 (third party, unverified) says a pull request with no matching key leaves the rule with nothing to act on and the audit log is where that shows.

### 6.3 Measure the Jira delays

8. Run the step 5 pull request flow (or a second one) with the throwaway key in the branch name and title. After each host event, open the issue's history and record the transition timestamp against the host's event time (branch creation time from the host, pull request created time, `pullrequest:fulfilled` delivery time from the webhook log). Write three delays in seconds into the Bitbucket repository's tracker delays cell.
9. If a transition did not fire, read that rule's audit log and record the reason verbatim. Common causes to check first: key missing or lower case, status name mismatch, integration not linked. All three are inferences from Part C4's keying rule, not measured facts; record what you see.

### 6.4 GitHub Projects "Item closed"

10. Open the GitHub Project that tracks Slipway's cards (Project 11 on `philgutowski`). Its workflows page is at `/users/philgutowski/projects/11/workflows`. Measured 2026-10-15: on a freshly created user project only "Auto add sub issues" was on; "Item closed" and "Pull request merged" were off, contrary to Part C5's "on by default". "Item closed" was turned on by hand, set to Status Done, after the Status field's default Todo option was replaced (a workflow whose value points at a removed option refuses to turn on until a value is picked). There is no API to flip these toggles; the GraphQL `workflows` connection reads them only.
11. Part C5: "Pull request merged" moves the pull request item, not the issue. The issue reaches Done only when it is closed, which a merged pull request causes when its body carries a closing keyword (`Closes #N`) and it merges into the default branch. Confirm Slipway's test pull request from step 3 carries `Closes #N` for a throwaway issue that is on the project board.
12. After the step 3 merge, record: time of the `pull_request` closed delivery, time the issue shows closed in its timeline, time the project item shows Done. Two delays go into Slipway's tracker delays cell.
13. Record for KTD27 that no built in Projects workflow fires on branch created or pull request opened (Part C5 states there is none), so In Progress and In Review on GitHub boards come from the Actions template in a later unit.

## What to hand back to Claude

- The filled cells in `merge-paths.md`, with every "to record" either filled or changed to "owner must confirm" or "not available on this host" with one line of reason.
- The five fixture files, or for each one not captured, the reason.
- The secrets you created, by operator file name only, never by value, plus a note of which ones to rotate before anything is committed.
