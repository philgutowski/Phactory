---
title: A landing check cannot assume the merged commit equals the commit the gate tested
date: 2026-10-15
category: architecture-patterns
module: verify landed
problem_type: architecture_pattern
component: service_layer
severity: high
applies_when:
  - "Verifying that a merged pull request is the code a gate tested and a person approved"
  - "Landing goes through GitHub or Bitbucket merge queues, or through auto merge with squash or rebase"
  - "A rule from a local merge flow, such as land the exact verified commit, is being carried into a host merge path"
tags: [verify-landed, merge-queue, squash-merge, tree-equality, landing-relation]
---

# A landing check cannot assume the merged commit equals the commit the gate tested

## Context

Phactory's first version plan carried the sentence "the merged commit equals the commit the gate tested" through the brainstorm (R20), a three review deepening pass, and the first four reviewers of a five persona document review. It reads as obviously right because it is the invariant of the local merge flow the earlier systems used: join in a scratch worktree, gate there, fast forward to the exact verified commit, never re merge (`docs/ideation/2026-10-14-software-factory.md`, the lanes rules). On a host merge path that invariant does not hold, and a verify built on it would have halted every normal landing as a commit mismatch.

The adversarial and feasibility reviewers caught it by tracing a squash merge and a queue merge instead of reading the sentence. The fix landed in PR #1.

## Guidance

State the relation between the commit a person approved and the commit that reached main per landing path and merge method, never as one equality:

- **Merge queue (GitHub, Bitbucket Cloud).** The host tests a commit it creates (GitHub's `merge_group.head_sha`, Bitbucket's merge result commit) and lands that commit. The check is two parts: the landed tip equals the tested commit, and the tested commit's recorded pull request head equals the head Phactory confirmed when it opened the pull request. Record the tested commit from the queue's checks requested event; do not infer it.
- **Auto merge without a queue, under "require branches to be up to date".** The landed commit is a merge, a squash, or a rebase of the confirmed head onto the current base. A squash or rebase lands a commit with one parent, so "the confirmed head is the second parent" is false for them. Compare trees: the landed tip's tree equals the confirmed commit's tree. That holds for all three merge methods because the up to date rule makes the pull request head's parent the base tip.
- **Required checks must exist on the queue's commit too.** A check Phactory posts only on the pull request head never appears on the temporary queue commit, so a queue that requires it waits forever.
- **A commit message naming the card key is evidence, not a gate.** A GitHub merge commit message names the pull request number, not the card, so gating on it halts landings that are correct.

## Why This Matters

The wrong relation fails closed, which is why it survives: every normal landing parks under needs you until the operator overrides, the failure looks like a configuration problem, and the sentence itself looks like the safety property. A fix that merely loosens the equality (accept any commit) would drop the property the check exists for, that the code on main is the code that was approved and tested.

## When to Apply

- Writing or reviewing any verify, landing, or audit step that compares a pre merge commit with a post merge commit.
- Moving a rule proven in a local merge flow onto a host merge path.
- Adding a required check that must pass on a merge queue.

## Examples

Squash merge, auto merge path: the pull request head `X` has parent base tip `B`. Squash produces `S` with the single parent `B`. `S != X` and `X` is not a parent of `S`, but `tree(S) == tree(X)`, so tree equality holds and the second parent check does not.

Queue path: the queue builds `T` from base tip plus the pull request, tests `T`, and lands `T`. The builder's head `X` is not `T`. Verify checks `landed == T` and that the queue's recorded pull request head for `T` is `X`.

## Related

- Plan: `docs/plans/2026-10-14-1730-feat-phactory-first-version-plan.md`, R20, KTD7, U10 (landing relation), U2 (spike that records the relation per merge method per repository).
- Resolved review: `docs/research/2026-10-14-plan/doc-review-resolved.md`, item 1.
- Local merge version of the same invariant: `docs/ideation/2026-10-14-software-factory.md`, "Verify in a scratch join and land the exact verified commit".
- The relation is unproved on real repositories until U2 captures `merge_group.head_sha` against the base tip, and the merge commit trees, per method.
