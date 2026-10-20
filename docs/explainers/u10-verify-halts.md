# U10 explainer: verify landed and the halt classes

After the host says a pull request merged, someone has to check that what reached `main` is the code that was approved and tested, and that the card moved to done. This card builds that check, the read only git calls it stands on, and the closed list of reasons a card can stop short. Nothing here writes to a host, a tracker, or the records store. The card workflow (U14) calls verify and records what it says.

## Words used below

- **Landing.** The moment a pull request's work reaches the base branch, usually `main`.
- **Landing path.** The mechanism the host uses to land: a merge queue, or auto merge with the branch kept up to date, or a plain merge.
- **Merge queue.** A host feature that builds its own commit from the base tip plus the pull request, runs CI on that commit, and lands it.
- **Merge method.** How the host builds the landed commit: a merge commit (two parents), a squash (one new commit holding all changes), or a rebase (each branch commit replayed on top of the base).
- **Tree.** Git's snapshot of every file at a commit, with its own hash. Two commits with different hashes can carry the same tree.
- **Baseline.** The commit on `main` the build started from.
- **Confirmed commit.** The branch head Slipway confirmed on the remote when it opened the pull request (KTD6).
- **Tested commit.** On a queue path, the commit the queue built and ran CI against, recorded with the pull request head the queue reported for it (KTD7).
- **Merged commit.** The commit the host says it landed (`merge_commit_sha` on GitHub).
- **Bare cache.** A git repository with no working checkout, only the object store. Slipway keeps one per project under `$SLIPWAY_HOME/cache/git/`.
- **Advertised.** A commit some ref on the remote points at, so the remote lists it when git asks what it has. Some hosts serve only advertised commits and refuse a request for any other id.
- **Held ref.** A ref Slipway writes in its own cache, under `refs/slipway/held/<branch>`, pointing at a confirmed head so that commit stays in the cache after the host deletes the branch.
- **Prune.** On a fetch, deleting a local branch ref whose branch no longer exists on the remote.
- **Halt.** A stop a person must look at, with a class from a closed set and the evidence that explains it.
- **Partial landing.** The commit landed but the card did not move to done.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/gitread.py` | Git calls that only read the project's remote: `run`, `GitError`, `rev_parse`, `fetch`, `log_oneline`, `log_messages`, `default_branch` (rewritten from native-relay's names), and the new `tree_of`, `is_ancestor`, `merge_tree`, `first_parent`, `cache_path`, `refresh`, `hold`, `release`. The only writes are to Slipway's own cache. |
| `src/slipway/verify.py` | `verify(...)` returns a `Verdict`: the outcome, the checks, the evidence, and a `Halt` when one applies. Also `hand_landing` and `key_pattern`. |
| `src/slipway/halts.py` | The seven halt classes, each with its kind, the evidence keys it requires, and its needs you line. `Halt` refuses a class outside the set and missing evidence when it is built. |
| `tests/unit/test_verify.py` | Every landing path and outcome on real git in temporary repositories, plus the `gitread` calls. |
| `tests/unit/test_halts.py` | The set is exactly seven, each one is produced and renders its line, and the kinds split as KTD11 says. |

## How verify reads git without running project code

The cache is filled at two moments.

**At confirm time**, while the branch still exists on the remote, the workflow calls `gitread.hold(cache, remote, branch, confirmed)`. It refreshes the cache, which brings the branch in with every other, so the confirmed head arrives as an advertised commit that any host serves. It then writes the held ref `refs/slipway/held/<branch>` pointing at that commit. Holding the same branch again moves the ref to the new head.

**At verify time** the workflow calls `gitread.refresh(cache, remote, commits=(confirmed,), refs=pull_refs)`. The first time it clones bare; after that it fetches every branch with prune, so a `slipway/*` branch the host deleted on merge leaves the cache too and branch refs do not pile up. Prune touches only `refs/heads/`, so the held ref survives, and garbage collection never drops a commit a ref points at. A confirmed head the cache still lacks is looked for in two more places, in order:

1. `refs`, the ref names a host keeps for the pull request after its branch is gone (`refs/pull/<n>/head` on GitHub). The ref layout belongs to the host, so a host adapter supplies it and `gitread` names none (R1). These are fetched for their objects only and stored under no local ref.
2. The commit's bare id. This is what used to be the only route, and it is why squash and rebase landings were fragile: with delete branch on merge, nothing advertises the confirmed head any more, and a host that refuses unadvertised ids fails the fetch, which verify reported as `git_unreadable`.

When the card is finished, the workflow calls `gitread.release(cache, branch)` to drop the held ref.

Because the cache is bare, no checkout ever happens, so no filter, hook, or attribute from the project can run (KTD18). Every git call also points hooks at nothing and turns off credential prompts. A `GitError` message is passed through `redact.scrub`, because a remote URL can carry a token.

Verify then reads only that cache. In tests the remote is always on disk; the network guard refuses an `https` remote, and a test proves it. The tests reach the origin through a `file://` URL rather than a bare path, because cloning a bare path copies the origin's whole object store, unreachable commits included, which would hide a refused fetch. A refusing origin is built by setting git's three `uploadpack.allow*SHA1InWant` settings to false and pinning the client to git protocol version 0, since protocol version 2 does not enforce those settings. Under that origin a fresh cache cannot get a squash landed card's confirmed head, and a cache that held it at confirm time verifies the card as landed.

## The landing relation per path

The relation between the confirmed commit and the merged commit depends on the landing path, never on commit equality alone (KTD7, `docs/spikes/merge-paths.md`, `docs/solutions/architecture-patterns/landing-check-relation-per-merge-path.md`).

| Landing path | Relation verify checks | Why |
|:-|:-|:-|
| `merge_queue`, `bitbucket_queue` | merged equals tested, and the tested commit's recorded pull request head equals confirmed | The queue lands the commit it tested, which it built itself. The head it recorded ties that commit back to what was approved. |
| `auto_merge_strict` | tree of merged equals tree of confirmed | Measured on this repository under merge commit, squash, and rebase: with the branch kept up to date, every method lands the confirmed tree. The landed hash differs from the confirmed hash under squash and rebase, so hashes are never compared. |
| `plain_merge` | tree of merged equals the tree git builds by merging confirmed into merged's first parent | Auto merge on with no required up to date check. The branch may be behind when it lands, so the landed tree also holds whatever reached `main` meanwhile. `git merge-tree` computes that merge in the object store alone. When the branch was up to date this reduces to the strict relation. |

Each relation is recorded under its own check name. A queue path records two checks, `merged_equals_tested` and `tested_head_equals_confirmed`; when the first fails the second is recorded as a skip. The other two paths record one, `confirmed_landed`. No name appears twice in a verdict, so a page that reads a check by name can never show a pass beside a halted verdict, which it could when both queue checks shared the name `confirmed_landed` and the first one passed.

On every path verify also checks `new_commit_since_baseline`: the merged commit is on the branch tip's history and comes after the baseline.

`hand_landing` looks for a commit between baseline and merged whose message names the card (`#13` on GitHub, `ABC-12` on Jira). It is recorded in evidence and never gates, because a host merge commit names the pull request number, not the card.

## How partial landing is detected

Partial landing is the commit checks passing while the card does not. Verify does not read the tracker; the caller passes the `TrackerStatus` it read through the tracker adapter (U8). If the commits landed and `status.terminal` is false, the outcome is `partial_landing` with halt `merged_card_not_moved`, carrying the card key, the merged commit, and the status the card is in. This is AE2.

The host's merged event usually arrives before the tracker's own rule moves the card, so the workflow re reads the tracker every 30 seconds for 10 minutes before it records this halt. The two numbers live in `halts.py` as `NOT_MOVED_RECHECK_SECONDS` and `NOT_MOVED_RECHECK_WINDOW_SECONDS`; the loop itself belongs to the workflow.

## The verify decision

```
hold the confirmed head at confirm time (workflow)
        |
refresh the bare cache (workflow)
        |
read branch tip, trees, ancestry   on GitError ->  halted, git_unreadable
        |
new_commit_since_baseline, then the path's relation checks
  (queue: merged_equals_tested, tested_head_equals_confirmed; else confirmed_landed)
        |
  any failed?   yes ->  halted, commit_mismatch (relation, expected, found)
        |no
tracker status read?
  skipped ->   blocked, no class (the caller names tracker_unreadable)
        |
card terminal?
  no  ->   partial_landing, merged_card_not_moved
  yes ->   landed
```

A commit mismatch wins over an unreadable tracker, because the commits alone already prove the landing is wrong.

## The closed halt set

A new kind of outcome becomes a finding on a record, never an eighth class (KTD11).

| Class | Kind | One line |
|:-|:-|:-|
| `merged_card_not_moved` | verify halt | The commit landed but the tracker still shows the card short of done. |
| `commit_mismatch` | verify halt | The landed commit does not relate to the confirmed one by the landing path's relation. Only the operator's land override passes it. |
| `tracker_unreadable` | verify halt | The tracker could not be read, so done cannot be decided. |
| `git_unreadable` | verify halt | The repository could not be fetched or read. |
| `builder_failed` | build outcome | The builder did not produce a usable branch: a provider error at start or a wrong base. |
| `builder_expired` | build outcome | The build ran past its role's time budget (KTD22). |
| `binding_invalid` | build outcome | The role's account binding in the project or operator file is not usable. |

A verify halt is a wait: the workflow sits in `halted`, posts a failing landing check with the needs you line, and waits for `card.resume` raced with an hourly re verify (`REVERIFY_SECONDS`). A build outcome is a reason on `returned_empty`, an ownership terminal state the dispatch verb can reopen.

## How it connects

- `verify` imports `gitread`, `halts`, `trackers.base` (for `TrackerStatus`), and `config.project` (for `LandingPath`). `gitread` imports `redact`. `halts` imports only the standard library. None of them imports `service`, `workflow`, or a surface (KTD23).
- U14 calls `gitread.hold` when it confirms the branch head, passes the host adapter's pull request refs to `gitread.refresh` before each verify, and calls `gitread.release` once the card reaches a terminal state.
- U9's `probe_landing` decides the landing path; U14 passes it in with the confirmed and tested commits from the build row, and the tracker status from the U8 adapter.
- U14 turns the verdict into a transition: `landed` to `landed`; any halt, or `blocked` with the tracker status's `tracker_unreadable`, to `halted` with the needs you line on the `slipway/landing` check.

## Follow up: one list of landing paths (#51)

An **enum** is a fixed set of named values defined once in code; Python's `StrEnum` makes each value also a plain string, so `LandingPath.PLAIN_MERGE == "plain_merge"` holds.

Verify first kept its own landing path strings beside the `LandingPath` enum in `src/slipway/config/project.py`, and only verify knew `plain_merge`. A rename on either side would have made verify refuse every call. Now:

- `LandingPath` has a fourth member, `PLAIN_MERGE`, so a project file may also pin `landing_path = "plain_merge"`.
- `verify.py` names no landing path of its own. It imports `LandingPath` and keeps one table, `RELATIONS`, mapping each member to the relation it is checked by: `tested_commit` for the two queues, `tree_equals_confirmed` for `auto_merge_strict`, and `tree_equals_confirmed_merged_into_parent` for `plain_merge`. `verify` refuses any path missing from the table and picks its checks from the relation, not from the path's name.
- `tests/unit/test_verify.py` fails when the table and the enum disagree, so adding a member to `LandingPath` without a relation is caught by the gate, not by the first card that lands that way. A second test fails if verify grows a landing path string again.
- `src/slipway/hosts/base.py` used to import its landing path names from verify. It now derives `MERGE_QUEUE`, `AUTO_MERGE_STRICT`, `BITBUCKET_QUEUE`, `PLAIN_MERGE`, and `LANDING_PATHS` from `LandingPath`, keeping the names `hosts/github.py` and the host tests already use.
