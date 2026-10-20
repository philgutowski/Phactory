# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Landing a card

### Baseline
The commit on the base branch that a build started from. A landing is only counted once the merged commit comes after it.

### Confirmed commit
The head of the card's branch as Slipway recorded it when it opened the pull request. It is not necessarily the code that ends up on the base branch, because a host can rewrite it while merging.

### Merged commit
The commit the host's merge left on the base branch. Whether it counts as the confirmed commit landing depends on the landing path, never on the two being equal.

### Landing path
The way a host brings a pull request into the base branch, such as through a merge queue, through auto merge, or by a plain merge. Each path has its own relation between the confirmed commit and the merged commit, and verifying a landing means checking the relation of the path that was used.

### Tested commit
Under a merge queue, the commit the queue built and ran its checks on. It is recorded alongside the pull request head the queue tested, so the chain from confirmed commit to merged commit can be checked link by link.

### Held commit
A confirmed commit pinned in Slipway's local copy of the repository while its branch still exists on the host. Without the pin, a squash or rebase landing followed by branch deletion leaves nothing on the host that names the confirmed commit, and a host that serves only commits some ref names can refuse to hand it over later.
*Avoid:* Cached head, saved head
