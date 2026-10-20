---
title: A git test origin on a plain path cannot refuse a fetch of an unadvertised commit
date: 2026-10-20
category: test-failures
module: gitread and verify tests
problem_type: test_failure
component: testing_framework
symptoms:
  - "A test that fetches a deleted branch's head by bare commit id passes against any remote, including one that would refuse it"
  - "Setting uploadpack.allowTipSHA1InWant, allowReachableSHA1InWant and allowAnySHA1InWant to false on the test origin changes nothing"
  - "Squash and rebase landings were green in tests and would have halted as git_unreadable on a host that serves only advertised commits"
root_cause: incomplete_setup
resolution_type: test_fix
severity: medium
tags: [git, test-origin, file-url, protocol-version, uploadpack, unadvertised-object, squash-merge]
---

# A git test origin on a plain path cannot refuse a fetch of an unadvertised commit

Two terms first. An **advertised** commit is one some ref on the remote points at, so the remote lists it when git asks what it has. An **object store** is the folder inside a repository where git keeps every commit and file it has ever stored, whether or not any branch still reaches it.

## Problem

Slipway verifies a landed pull request by reading the commit it confirmed (the card's head when the pull request opened) from a local bare cache of the project's repository. With squash or rebase landings and delete branch on merge, the confirmed head is no ancestor of main and no branch points at it, so it is unadvertised. Some hosts refuse a fetch of an unadvertised id. The code path that handled this had a test that passed, and the test could not have failed.

## Symptoms

- The deleted-branch test in `tests/unit/test_verify.py` was green with no fetch by id ever attempted.
- Turning off the three `uploadpack.allow*SHA1InWant` settings on the test origin (the switches that let a server hand out a commit by id) made no difference to the result.
- The gap was found while building #66, not by a failing test. On a real host it would have shown as every squash landing halting with `git_unreadable`.

## What Didn't Work

- **Cloning the origin by its directory path.** `git clone /path/to/origin.git` uses git's local transport, which copies the origin's whole object store. Unreachable commits come along. The cache already held the deleted branch's head, so no fetch by id was needed or tried. Reproduced on git 2.39.3: after deleting the branch on a bare origin, a clone by path still had the commit (`cat-file -t` printed `commit`), and a clone through a `file://` URL did not (`could not get object info`).
- **Setting the three `uploadpack.allow*SHA1InWant` options to false and nothing else.** Git protocol version 2, the default in current git, does not enforce them. With all three false, a by-id fetch over `file://` still succeeded on 2.39.3. The origin looked locked down and served anything.

## Solution

Two changes to the test setup, both in `tests/unit/test_verify.py` and shipped in #66 (closes #52):

1. **Reach the origin through a `file://` URL**, not a bare path. The test's `Landing` class passes `self.repo.origin.as_uri()` as the remote (`tests/unit/test_verify.py:9-14` explains why). Git then runs the same upload pack exchange it runs against a host, and sends only what the origin serves. Upload pack is the server side program that answers a fetch.
2. **Make the origin refuse, and pin the client to protocol version 0**, in `refuse_unadvertised_ids` (`tests/unit/test_verify.py:34-48`): set the three `uploadpack` options to false in the origin's config, then set `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=protocol.version`, `GIT_CONFIG_VALUE_0=0` in the environment, so every git call in the test uses version 0. A by-id fetch then fails with `Server does not allow request for unadvertised object`, and the test matches the substring `unadvertised` of that message (`tests/unit/test_verify.py:311`).

With a refusing origin in place the test could finally prove the production fix: `gitread.hold` (`src/slipway/gitread.py:205`) fetches and pins the confirmed head under `refs/slipway/held/<branch>` at confirm time, while the branch still exists and the commit is advertised. A fresh cache without that hold fails with `unadvertised`; a cache that held the head verifies the card as landed.

## Why This Works

A fetch by id only reaches the server when the client lacks the object. The path clone removed that precondition, because the local transport copies objects wholesale. The `file://` URL restores it. Separately, the refusal settings are a version 0 feature: in version 2 the client asks for objects by id and the server does not apply those three checks. Pinning version 0 puts the checks back in play. Both were confirmed by hand on git 2.39.3 with a throwaway repository: path clone had the commit, `file://` clone did not, a default-protocol by-id fetch succeeded against a three-way-false origin, and the same fetch with `-c protocol.version=0` printed the unadvertised-object error.

## Prevention

- **A test of "the remote refuses X" must show it can fail.** Keep the pair in the suite: one case that proves the unheld cache raises `unadvertised`, next to the case that passes with the hold. If the refusing setup silently stops working, the first case turns green-to-red and says so (`tests/unit/test_verify.py:302-316`).
- **Test remotes are `file://` URLs by default** when the code under test fetches, clones, or checks reachability. A bare path is fine only for tests that do not care how objects travel. The network guard in `tests/_nonet.py` refuses command arguments that start with `http://`, `https://`, `ssh://`, `git://` or `git@` (and a few network programs), so `file://` passes; keep it that way, since refusing it would push tests back to bare paths. The phase 1 explainer (`docs/explainers/phase-1-scaffold.md`) describes the guard.
- **Pin `protocol.version=0` through git's environment config** (`GIT_CONFIG_COUNT` and friends), not a repository config file, so the pin reaches every git subprocess the code under test starts.
- **Treat the Bitbucket behaviour as unmeasured.** Whether Bitbucket keeps a pull request ref after the branch is deleted, and whether it refuses unadvertised ids, is still open in the merge path spike (#5). `hold` avoids the question by pinning at confirm time; do not drop it on the strength of a GitHub-only result.

## Related

- `docs/explainers/u10-verify-halts.md`, lines 37 to 48: what `hold`, `refresh` with prune, and the pull request refs do, written beside the code.
- `docs/solutions/architecture-patterns/landing-check-relation-per-merge-path.md`: why the confirmed head matters at all (the squash path compares the landed tree to the confirmed commit's tree), which is what makes it worth keeping fetchable.
- `docs/spikes/merge-paths.md`: the repository settings (delete branch on merge on, squash as the landing method) that create the unadvertised-head condition.
- #52 (the card), #66 (the fix), #13 (the original U10 card), #5 (open: Bitbucket side).
