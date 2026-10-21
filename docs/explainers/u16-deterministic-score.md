# U16 explainer: the deterministic score and check_card

This card fills the first step of the `post_landed` seam U14 left empty. Once a build is `landed`, the card workflow runs that seam, and the step this card adds scores the landed change against what its card declared. The same module also answers a question asked before a card is filed: would Slipway dispatch this draft as written?

## Words used below

- **Score.** A record of how a landed card measures against what it declared, one row per build and kind in the `scores` table (U7).
- **Deterministic score.** The score computed from facts alone, with no model and no judgement. U17 adds the other kind, the judged score, where a model reads the card's definition of good and the diff.
- **Item.** One of the three things the deterministic score checks: `gate`, `paths`, and `runs`.
- **Absence.** An item with nothing to check: the card declared no paths, named no test, or an input could not be read. An absence is recorded as itself and never as a fail (KTD5).
- **Gate.** The project's own lint and test command, run by the host's CI or by a runner instance, never by Slipway (KTD18).
- **Gate log.** The text the gate printed while it ran, as the host or the runner serves it.
- **Seam.** A named point in the card workflow where an ordered list of steps runs (U14).
- **Draft.** A card someone is writing and has not filed, or has filed but nobody has claimed.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/evals/deterministic.py` | `score` and `check_card`, both pure: they read the text and lists they are handed, and start no process. Also the gate log reader and the paths matcher both share. |
| `src/slipway/evals/steps.py` | `score_deterministic`, the `post_landed` step: reads the card, the diff, the landed tree, and the gate, calls `score`, and writes the scores row. Its one line `SLIPWAY_STEPS` is what `workflow/post_landed.py` registers. |
| `src/slipway/records/store.py` | `record_score`, the write to `scores`, unique on build and kind, so a step that runs twice replaces its first row. |
| `src/slipway/workflow/worker.py` | `PostgresRecords.record_score`, so the real worker's records offer the write. |
| `tests/unit/test_deterministic.py` | Every scenario on the card, on fixtures and a throwaway git repository. |
| `tests/integration/test_records.py` | The scores write against the real Postgres. |
| `tests/unit/test_card_workflow.py` | One line: `post_landed.load()` now finds `score_deterministic`, where U14's test said nothing was registered. |

## The three items

```
gate    the gate's conclusion           pass | fail | no conclusion | not read
paths   diff inside declared paths      pass | fail (files listed) | not declared | not read
runs    the named test ran and passed   pass | fail (why) | none named | not read
```

**gate** folds the gate's own word for how it ended into pass or fail: `success` passes, `failure`, `timed_out`, `cancelled` and their kin fail, and anything else (neutral, skipped, still running) records `no conclusion`.

**paths** compares every file the landed change touches with the card's `paths`. An entry covers a file when it is the file, a directory above it (with or without a trailing slash), or a glob that matches it (a glob is a path pattern in which `*` and `?` stand for any characters and `[...]` for any one of a set). Any file no entry covers fails the item, and the record lists those files beside the declared paths.

**runs** reads the gate log for the test the card names (AE7). The test can be a file (`tests/test_page.py`) or a test id (`tests/test_page.py::test_renders`). It fails when:

- the landed tree does not hold the named file, when the tree was read;
- the log holds no report line for it ("not in the gate's log");
- a report line says it failed;
- every report line says it was skipped.

The log reader knows the report lines of pytest (verbose, the `-rA` summary including its file and line form for skips, the failure summary, and the default progress line), Jest and Vitest, and `go test`. It takes off GitHub Actions' per line timestamps and colour escapes first. A project whose gate prints no test ids (pytest with `-q` alone prints dots) fails the runs item on every card that names a test, which is the honest answer: Slipway cannot see that the test ran.

A passing runs item records `passed_reports`, the number of report lines that said the test passed. It counts lines, not tests: a file's progress line and each of its tests' summary lines count once each.

**The total** counts what passed and what failed and names what was absent. Its verdict is `fail` when any item failed, `pass` when all three passed, and `partial` when nothing failed but something was absent. A card with no block and a green gate is `partial`, never `fail`.

## Where the step's inputs come from

`score_deterministic` only reads:

- **The card**, from the tracker, parsed by `cards.contract.parse`, the parser the ready rule uses. A card the tracker cannot read scores `paths` and `runs` as `not read`.
- **The diff**, from Slipway's own bare cache through `gitread`: the files the confirmed commit changes since the point where it meets the landing branch just before the merge (the merged commit's first parent). A branch that merged `main` in partway is scored on its own changes, not on the other cards it brought along.
- **The landed tree**, the files of the merged commit, from the same cache.
- **The gate**, from the host adapter's `gate_run(commit)` when the adapter has one, for the tested commit on a queue path and the confirmed commit otherwise. It returns the conclusion and the log.

A bare cache has no checkout, so no project hook, filter, or attribute runs while the step reads it (KTD18). The test `test_no_code_path_in_the_module_invokes_a_subprocess_or_reads_the_gate_command` walks `deterministic.py` and holds it to that.

## check_card

`check_card(draft, project, base_tree, in_flight, other_lanes)` reads a draft the way dispatch would and changes nothing. It returns `would_dispatch`, or the list of what is missing:

- **The lane label.** No lane label returns the allowed labels; two lane labels, or another lane's label, are named.
- **The block.** Parsed by the same parser, so a block that fails here fails the ready rule too. A block naming another lane is named.
- **The named test.** Its file must be in the base tree, or inside the card's own paths when the card is the one that writes it.
- **The paths.** Each in flight build whose files the draft's paths overlap is named with its state and the shared files. Files are the build's pushed diff once it has one, else its declared paths, the same rule the handoff uses.

Absent fields go in `notes`, not `missing`, because KTD5 lets a card dispatch without them.

## Where this departs from the card, and why

- **No host reads the gate log yet.** The card's runs item reads "the gate's log read from the host (or the runner's read API)". No host adapter has that read, the host adapters are not in this card's paths, and the project file does not name which check is the gate. So the step asks the host adapter for `gate_run(commit)` and records `not read` for `gate` and `runs` when it has none. Until a later card adds it to the GitHub and Bitbucket adapters (and U23 adds the runner's read), a landed build scores `partial`, never `fail`, on those two items. `score` itself, which the scenarios test, takes the gate and its log as inputs and needs nothing more.
- **`score` takes the block and the tree.** The plan writes `score(build, diff, gate_log, project)`. The items read the card's block, not the project file, and the landed tree lets AE7's "the test does not exist" fail without a log, so the call is `score(build, diff, gate, block, tree)`.
- **A named test may be new.** The card asks that the named test file exist in the base tree. A card that writes its own test (this one, for instance) names a file the base does not have yet, so a file inside the card's paths passes too.
- **The scores write.** The card's paths have no record write, but the score must reach U7's table. `store.record_score` and `PostgresRecords.record_score` are the smallest pieces that do it; the step calls the write only when the records offer it, so the fakes U14's tests use stay unchanged.
