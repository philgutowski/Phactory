# U9 explainer: host adapters and webhooks

A host is where a project's repository lives: GitHub or Bitbucket Cloud. Slipway needs seven things from a host: create the card's branch, confirm the commit the builder pushed, open the pull request so the tracker's automation finds the card, post its own check on any commit, put the pull request on the host's landing path where an API exists, read the pull request's live state, and find out how the host will land it. It also needs to accept the host's webhooks safely. This card builds the code that does all of that for both hosts, under the network guard, from documented payload shapes.

## Words used below

- **Host.** The service holding the repository: GitHub or Bitbucket Cloud.
- **Adapter.** A class that hides one host behind a shared set of methods, so the rest of Slipway never knows which host it is talking to.
- **Host credential.** The token the adapter sends with every request. On GitHub a GitHub App installation token (a short lived token minted for one installation of an App on a repository) or a token the operator holds; on Bitbucket a repository access token (a credential Bitbucket issues for one repository with named scopes). The project file names the operator file entry; the operator file names the environment variable; the variable holds the value (R3).
- **Confirmed commit.** The branch head Slipway saw on the host when it opened the pull request (KTD6). Every later relation starts here.
- **Landing path.** The mechanism the host uses to put a pull request's work on the base branch: `merge_queue` (GitHub's queue), `auto_merge_strict` (GitHub auto merge with "require branches to be up to date"), `bitbucket_queue`, or `plain_merge` (auto merge on with no required up to date check, named by U10).
- **Merge method.** How the host builds the commit that lands: `merge` (a merge commit with two parents), `squash` (one new commit holding all changes), or `rebase` (the branch's commits replayed on top of the base).
- **Tested commit.** On a queue path, the commit the host itself creates and runs CI against (GitHub's `merge_group.head_sha`, Bitbucket's merge result commit) (KTD7).
- **Ruleset.** GitHub's per repository set of rules that restrict what can happen to a branch. The probe reads the rules GitHub computes for the default branch from every active ruleset.
- **Check.** A status the host shows on a commit and on the pull request. Under a GitHub App it is a check run (GitHub's richer check with a title and summary); under any other credential it is a commit status; on Bitbucket it is a build status. A ruleset's required status check matches either by name.
- **Webhook.** An HTTP POST the host sends to Slipway's endpoint when an event happens. The JSON body is the payload.
- **HMAC.** A keyed hash. The host hashes the raw body under the webhook secret and sends the result in a header; only a sender holding the secret can produce it. Slipway recomputes it and compares in constant time, so the time taken says nothing about how many characters matched.
- **Delivery id.** The id the host gives one delivery (GitHub `X-GitHub-Delivery`, Bitbucket `X-Request-UUID`). A retry carries the same id, which is what dedupe keys on.
- **Normalised event.** The small closed vocabulary Slipway turns any host event into: `pr.merged`, `pr.closed`, `pr.synchronize`, `pr.dequeued`, `queue.requested`, `check.completed`.
- **Injection.** Passing the HTTP opener into the adapter's constructor instead of building it inside, so a test can pass a fake.

## What each file does

| File | What it does |
|---|---|
| `src/slipway/hosts/base.py` | The interface, the result types (`Branch`, `PullRequest`, `CheckPost`, `Landing`, `Credential`), the errors (`HostError`, `BranchConflict`, `ProbeUndetermined`), the shared JSON `Client` over urllib, the credential resolver, and the helpers that put the closing reference in a title or body. |
| `src/slipway/hosts/github.py` | `GitHubHost`: REST for branches, pull requests, checks, and the probe; GraphQL for enqueue and auto merge. |
| `src/slipway/hosts/bitbucket.py` | `BitbucketHost`: REST 2.0 for branches, pull requests, and build statuses; `enqueue` returns `not_supported`; the probe returns pinned values or refuses. |
| `src/slipway/hosts/webhooks.py` | `verify`, the one entry for a delivery: cap, resolve the repository, check the signature, parse. `Event`, `Dropped`, `WebhookRefused`, `DeliveryLog`, and the two parsers. |
| `tests/unit/test_hosts.py` | Every scenario the card names, over `FakeApi`, a stand in for urllib's opener that routes a method and URL pattern to a status and a fixture. |
| `tests/fixtures/host/*.json` | Documented payload and API shapes, hand written, every id invented. The README beside them says why they are not recorded deliveries. |
| `docs/explainers/u9-host-adapters-webhooks.md` | This file. |

## The adapter interface

Every adapter implements exactly these methods, and the test asserts nothing else is public, so a `merge` method can never appear (R18, Slipway never merges):

| Method | GitHub | Bitbucket |
|---|---|---|
| `create_branch(name, sha)` | `POST git/refs`; on "already exists", reads the branch and returns it when it sits at `sha`, else `BranchConflict` | `POST refs/branches`, the same rule |
| `commit_on_remote(branch, sha)` | `GET git/ref/heads/<branch>`, true when the head is exactly `sha` | `GET refs/branches/<branch>` |
| `open_pull_request(branch, title, reference)` | `POST pulls` with the body line first in the body; on "already exists", lists open pull requests by head and returns the one on the branch | `POST pullrequests` with the title token in front of the title |
| `post_check(sha, name, state, url, summary)` | `POST check-runs` under an App, `POST statuses/<sha>` under a token | `POST commit/<sha>/statuses/build`; refuses without a url, which Bitbucket requires |
| `enqueue(pr, landing)` | `enqueuePullRequest` on the queue path, `enablePullRequestAutoMerge` with the probed method otherwise | `not_supported`, no request |
| `pull_request(pr)` | `GET pulls/<n>` and `GET pulls/<n>/reviews` | `GET pullrequests/<id>` |
| `probe_landing(project)` | pinned values, else the repository and `GET rules/branches/<default>` | pinned values, else `ProbeUndetermined` |

`create_branch` is idempotent on purpose. Dispatch creates the card's branch at the base commit (KTD27), and a retried step must find its own branch and go on. A branch at a different commit means two dispatches crossed, and that is refused with both commits in the error.

`open_pull_request` takes the tracker's `ClosingReference` (U8) and never a string. The GitHub tracker gives a body line, `Closes #12`, and the adapter writes it as the first line of the body, where GitHub's own automation reads it. The Jira tracker gives a title token, `EX-12`, and the adapter writes it in front of the title, where Jira's development integration reads it. A title that already carries the token is left alone.

`pull_request` normalises the host's words to three states, `open`, `closed`, and `merged`, with `merged` as a separate flag and the merge commit filled only once merged. GitHub's approvals are the reviewers whose latest decisive review is an approval: a later request for changes or a dismissal withdraws one, and a plain comment decides nothing. Bitbucket's are the participants marked approved.

## The probe

The landing path and merge method are a live read, never a table in code (KTD9), and the project file may pin either or both (U6). The GitHub probe:

1. Returns the pinned pair at once, without a request, when the project file sets both.
2. Reads the repository for its default branch, whether auto merge is allowed, and which merge methods are allowed.
3. Reads the rules GitHub computes for the default branch. A `merge_queue` rule makes the path `merge_queue` with the rule's own merge method. Without one, a `required_status_checks` rule with the strict (up to date) policy and at least one named check, on a repository that allows auto merge, makes the path `auto_merge_strict`, which is exactly what `docs/spikes/merge-paths.md` measured after `ci` became required. Anything less is `plain_merge`, so verify (U10) uses the weaker relation instead of waiting for a merge that will never be queued.
4. Off a queue, the method is the one Slipway will pass when it arms auto merge: the only method the repository allows, or the first of squash, merge, rebase among those allowed. Squash comes first because it lands one commit per card and the landed hash is the pull request's `merge_commit_sha`, as the spike recommends for Slipway's own project file.
5. Any value the project file pins replaces the probed one, and the `Landing` records which source each value came from.

Bitbucket Cloud exposes no API for its merge queue or the queue's strategy (`docs/research/2026-10-14-plan/framework-docs-apis.md`, Part C2), so the Bitbucket probe has nothing to read. It returns the pinned pair, and with either key missing it raises `ProbeUndetermined` naming the keys to pin, rather than guess.

## Enqueue on each host

KTD7 says Slipway enqueues with `enqueuePullRequest` once reviews and checks pass, or lets auto merge do it. The GitHub adapter does both from one method: on the queue path it enqueues at the pull request's current head, so a head that moved meanwhile is refused by the host; on either auto merge path it arms auto merge with the probed merge method, at the same expected head. The caller passes the `Landing` it already probed, or the adapter probes once itself. Bitbucket has no endpoint for its queue, so `enqueue` returns `not_supported` without a request, and the repository's own "automatically queue once builds pass" setting, or the click that the needs you entry names, does the work.

## Webhooks, in order

`verify(host, headers, body, secrets_for)` does these in this order, and nothing later runs when an earlier step refuses:

1. **Cap.** A body over one million bytes is refused as `body_over_cap` before it is decoded.
2. **Repository.** The payload names it under `repository.full_name` on both hosts. `secrets_for(repository)` answers with that repository's current secret and, inside a rotation window, the previous one, from the operator file's `webhook_secrets` (U6). None or an empty answer is `unknown_repository`, refused before any secret is chosen, so a stranger's payload never reaches the signature check and cannot learn from its timing.
3. **Signature.** GitHub sends `X-Hub-Signature-256`, Bitbucket `X-Hub-Signature`, each `sha256=` and the hex HMAC SHA-256 of the raw body. Every listed secret is compared in constant time, so the time taken does not say which secret matched either. No header is `no_signature`, no match is `bad_signature`.
4. **Delivery id.** Missing is `no_delivery_id`, because a row without one cannot dedupe.
5. **Parse.** The host's event header and the payload's action map onto the six kinds. An event Slipway does not track is returned as `Dropped` with its reason, not refused: a `ping` or a `labeled` action is a valid delivery with nothing to do, and the endpoint should answer it with success so the host does not retry. When the caller passes `opened_by_slipway`, a pull request event for a number Slipway did not open is dropped the same way.

`Event.normalised()` is the mapping `ingest_host_event` (U7) stores: `host`, `delivery_id`, `repository`, `pr_number`, `kind`, `merge_commit`, `head_commit`, then the tested commit, the tested head, the check name and state, and the event's own details. `DeliveryLog` is the in process half of dedupe, a bounded memory of recent delivery ids the endpoint may consult before the database; the durable half is the table's unique key on host and delivery id.

Where each kind's fields come from:

| Kind | GitHub | Bitbucket |
|---|---|---|
| `pr.merged` | `pull_request` closed with `merged` true: `merge_commit_sha`, `head.sha`, `merged_by` | `pullrequest:fulfilled`: `merge_commit.hash`, `source.commit.hash`, `closed_by` |
| `pr.closed` | `pull_request` closed with `merged` false | `pullrequest:rejected` |
| `pr.synchronize` | `pull_request` synchronize: the new head is `after` | `pullrequest:updated`: `source.commit.hash`, which Bitbucket also sends for an edit that moved nothing, so the caller compares it with the confirmed commit |
| `pr.dequeued` | `pull_request` dequeued, with its `reason` | none |
| `queue.requested` | `merge_group` checks requested: the tested commit is `merge_group.head_sha`; the pull request number and the group's pull request head come from `head_ref`, which GitHub names `refs/heads/gh-readonly-queue/<base>/pr-<number>-<head sha>` | a commit status `INPROGRESS` on a `bitbucket-merge-queue-*` ref: the tested commit is `commit_status.commit.hash`; the payload names no pull request, so the caller resolves it through the adapter |
| `check.completed` | `check_run` completed (name, conclusion, `head_sha`, first linked pull request) and `status` in a final state (context, state, sha) | a commit status in a final state (`SUCCESSFUL`, `FAILED`, `STOPPED`) |

Neither host signs a timestamp, so a replay cannot be refused at the signature. That is why a host event is a wake up and never evidence (KTD10): every transition it causes re reads the pull request through the adapter first, and a replayed merge event finds the same merged state and the same row.

## How each adapter is injected so tests never reach the network

Both adapters take an `opener` with urllib's `open(request, timeout)` shape and build their own only when none is passed. The tests pass `FakeApi`, which records every request's method, URL, JSON body, and headers, and answers by routing a method and URL pattern to a status and a fixture; a status of 400 or more comes back as urllib's `HTTPError`, the way the real opener delivers it, so the adapters' handling of a 404 or a 422 is tested on the same path they run in production. One test builds an adapter with no fake and proves the network guard (`tests/_nonet.py`) catches it. The `Client` turns any failure to reach the host into a `HostError` whose text is scrubbed of the token (KTD24), and a test proves the token is absent from the message.

## Where this differs from the card

- **Fixtures are documented shapes, not U2 recordings.** U2's steps 4 to 6 are still open, so no delivery was captured. Each fixture follows the host's published shape and the README beside them says so. When U2 records a live delivery, the recorded file replaces its twin under the same name.
- **The live smoke is the operator's.** No test repository was reachable from this card's session, so the pull request body says the smoke is left to the operator, as the conductor's note asked.
- **The Bitbucket probe cannot read the queue.** The card asks for the merge method from the live queue setting; Bitbucket Cloud publishes no API for it, so the Bitbucket probe is pinned or undetermined, never a guess.
- **`enqueue` arms auto merge on GitHub's non queue paths.** The card names `enqueue` as the queue entry only; KTD7 says Slipway enqueues or lets auto merge do it, and one method doing either from the probed `Landing` keeps the workflow (U14) free of a host specific branch. The return value says which happened.
- **`plain_merge` is a probe answer.** The project file's `LandingPath` has three members; the probe can also answer `plain_merge`, the fourth path verify (U10) knows, when a repository has auto merge but no required up to date check. The constant comes from `slipway.verify`, so the two modules cannot disagree on the name.
- **The GitHub App token is read, not minted.** The adapter sends whatever the named environment variable holds. Minting an installation token from an App's private key needs RSA signing, which the standard library does not provide; that mint belongs with the credential refresh the builder adapters own (KTD6, U11 and U12), and this card does not add a dependency for it.
