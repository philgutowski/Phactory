# U26 explainer: the first project's lane

The first project Slipway runs is not shaped like Slipway's own repository. Its own watcher session lands every branch, its laptop builder shares one checkout with several other sessions, and its branches and pull request titles follow its own naming. This card adds the project file fields and the code paths that read them, so that project runs through Slipway with no code naming it (R1). Every value below is set in the project's file; the code carries only the defaults, which keep today's behaviour.

## Words used below

- **Lander.** Whatever puts a pull request's branch on the base branch. On GitHub's merge queue or auto merge it is the host; on this path it is something of the project's own, such as a watcher session that merges the branch into main and pushes.
- **Base tip.** The commit the base branch (usually `main`) points at.
- **Tree.** The snapshot of every file in a commit. Two commits with the same tree hold the same files, whatever their history.
- **Template.** A text with placeholders in braces, such as `{key}-{slug}`, that a value fills in.
- **Slug.** The card title in lower case words joined by hyphens, such as `fix-export` for "Fix export".
- **Worktree.** A second working folder of one git repository. It has its own checked out branch and its own files, and it shares the repository's commits with the main folder.
- **Shared checkout.** The clone on the laptop that the operator and other sessions already work in. It must stay on its own branch with its own uncommitted files.
- **Namespace.** The fixed text a branch name starts with, such as `slipway/`, that a machine identity's push rule holds the builder to (KTD6).

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/config/project.py` | Adds `external_lander` to the landing paths, and three optional fields: `branch_template`, `pull_request_title`, and `checkout`. Validates the templates at load, renders them (`Project.branch_for`, `Project.title_for`), and gives `branch_namespace`, the literal text in front of a template's first placeholder. |
| `src/slipway/verify.py` | Adds the relation for `external_lander`: the base tip's tree equals the tree Slipway confirmed. |
| `src/slipway/workflow/card.py` | Names the branch and the pull request from the project's templates, arms no auto merge on the external lander path, and ends a build whose card branch already exists when the template has no build id. |
| `src/slipway/builders/cli.py` | Adds the laptop builder's worktree checkout: a worktree of the shared checkout in place of a clone. |
| `tests/fixtures/config/lander.toml` | A project file shaped like the first project's, every value invented. |

## The external lander

`landing_path = "external_lander"` says that something other than Slipway and other than the host's queue lands the branch. The host then marks the pull request merged because its commits reached the base branch. Slipway still never merges, and on this path it arms nothing either: `open_pull_request` skips `enqueue` even when the file says `auto_merge = true`, because arming the host's auto merge would make the host a second lander.

No host setting reveals an outside lander, so the landing probe never finds this value live. It comes only from the project file, which `landing_from` and `pinned_landing` (in `hosts/base.py`) already put in place of whatever the probe reads. On Bitbucket the probe needs both `landing_path` and `merge_method` pinned, so a Bitbucket project on this path still names the lander's merge method.

Verify checks this path by the relation of the auto merge path, applied to the base tip: once the host says merged, the tip of the base branch must carry exactly the tree Slipway confirmed. A host that marks a pull request merged by an outside push may give no merge commit, so verify falls back to the tip for `merged` when the host gave none. A lander that changed the card's files on the way, or a tip that already carries another change, is `commit_mismatch`, the same halt every other path uses.

## Branch names and titles

`branch_template` fills three placeholders:

| Placeholder | Value |
|:-|:-|
| `{key}` | the card key, `XY-12` or `13`. Required, because the board automation and verify find the card by it. |
| `{slug}` | the card title as a slug, at most forty characters, cut at a word. `card` when the title is empty. |
| `{build}` | the build id. |

The default is `slipway/{key}-{build}`, exactly the name every build has had so far. The plan's text called the default `slipway/{key}-{slug}`, but the code already named branches by build id, so the default keeps the code's shape and the slug is there for a project that wants it. A project like the first one sets `{key}-{slug}`, which renders `XY-12-fix-export` for card XY-12 titled "Fix export".

At load the template is refused when it lacks `{key}`, uses any other placeholder or a format spec, or renders a name git would refuse. The check renders the template with sample values and applies git's branch name rules (no space, no two dots, no component starting with a dot, no `.lock` ending, no leading slash or hyphen, and the rest of `git check-ref-format`). A slug holds only lower case letters, digits, and hyphens, so a template that passes with the sample passes with any card.

A template without `{build}` gives every build of one card the same branch. When the branch already exists, the workflow cannot tell whether it is an earlier build's or this build's own from a step run twice, so it ends the build `returned_empty` with the reason "branch ... already exists at ...; delete it to build this card again". The default template carries the build id and keeps the old behaviour of building on.

`pull_request_title` fills `{key}` and `{title}` and must carry `{title}`. The default `{title}` is today's title; the host adapter still puts the tracker's title token in front when the title does not already carry it, so a template that starts with the key does not get the key twice.

`branch_namespace` reads the literal text in front of the template's first placeholder: `slipway/` for the default, empty for `{key}-{slug}`. Onboard (U19) is the one caller: when it pins a machine identity to a namespace it reads this in place of a fixed `slipway/`, and refuses an empty one. The first project's builder pushes with the operator's own credentials, not a machine identity, so its empty namespace has nothing to be held to.

## The worktree checkout

`checkout = "worktree"` in the project file, and a `LaptopBuilder` made with `checkout="worktree"` and `shared=<path of the shared checkout>`, run each session in its own worktree instead of a fresh clone:

1. Before the session, the worker notes whether the shared checkout already has a local branch of the card's name, fetches the card's branch into the shared checkout (the fetch writes only objects and `FETCH_HEAD`), and adds `workspace/repo` as a worktree at the base commit, detached.
2. The session script (`WORKTREE_SCRIPT`) marks the two folders safe for its own git, sets the remote and the branch's upstream through git's environment config (`GIT_CONFIG_COUNT` and its keys), which only this session reads, and creates the card's branch with `git switch --create`. A plain `git push` then reaches the card's branch. Nothing is written to the shared checkout's configuration.
3. On release, the session user removes the workspace, the worker runs `git worktree prune` so the shared checkout forgets the worktree, and deletes the local branch the session made.

If the shared checkout already had a local branch of that name, it is someone else's: `git switch --create` fails, the session exits with nothing pushed, the build ends `returned_empty`, and release leaves the branch alone. The shared checkout's own branch, files, and status are never touched. The session user needs write access to the shared checkout's `.git` folder, which the laptop's install gives through the worker's group.

`clone` stays the default, and a container always clones.

## The ready status

`ready_status` may be any status, a backlog one included: the lane label alone marks which cards are Slipway's (`lane_label`). The first project keeps its Slipway cards in Backlog, because its own feeder builds every Ready card. Both are fields since U6; the fixture shows the shape. The onboard question that says so is U19's to ask, since onboard does not exist yet.

## Left for later

- **The live smoke.** One real card of the first project built in a worktree on the laptop and landed by its watcher is the operator's run. The pull request says so.
- **Onboard.** U19 reads `branch_namespace` and asks the ready status question above.
- **The laptop worker's wiring.** The worker that builds `LaptopBuilder` passes `checkout` from the project file and the shared checkout's path from the laptop's own install.
