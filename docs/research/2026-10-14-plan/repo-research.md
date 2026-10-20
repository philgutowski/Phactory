# Repo research: lifting adapters, verify, and the lease from native-relay into Slipway

Scope: patterns. Source repo `/Users/pgutowski/Documents/PhilAI/native-relay`, HEAD `6e15774`, clean. All paths below are relative to that root. Read only; nothing changed.

## Headline findings (what changes the plan)

1. **Nothing lifts as a bare file.** Every one of the five pieces imports `relay.contracts`, and the adapters, `verify.py`, and the lease all take a `manifest` object with a specific dataclass shape (`manifest.tracker.*`, `manifest.project.*`, `manifest.shipping_mode`, `manifest.shipping_push`). The lift is "copy the file, then replace the manifest object with a small config dataclass and inline about ten constants". Budget that as rewriting the constructors and the top 30 lines of `verify.verify`, not as a copy.
2. **The GitHub adapter shells out to `gh`, never REST.** Every read is `gh issue view`, `gh issue list`, `gh project item-list`, `gh repo view`, and `gh api graphql`. There is no token, no `GITHUB_TOKEN`, no `urllib`. A hosted Slipway has to either install and authenticate `gh` in its runtime or rewrite the transport. The injectable `run` callable is the seam that makes a rewrite cheap.
3. **The Jira adapter is REST with Basic auth from two env vars**, names configurable through the manifest (`tracker.token_env`, default `JIRA_API_TOKEN`; `tracker.email_env`, default `JIRA_EMAIL`), site from `tracker.site`. Standard library `urllib` only. This one lifts almost cleanly.
4. **`verify.verify` cannot be expressed as (repo, baseline, head, task id, tracker status) alone without dropping checks**, but the three checks Slipway needs can. It reads six to eight things from git and two from the tracker, and half the checks (`head_equals_remote`, `mirror_equals_head`, `pr_open`, `ci_green`, `on_default`, `tree_clean`) encode native-relay's local-merge shipping model, which Slipway does not have. What survives is `new_commit_since_baseline`, `hand_landing`, `card_terminal`, `closing_reference`, and `_finish`'s landed/partial_landing rule.
5. **The lease is not a separable piece of `state.py`.** It is methods on `StateStore`, whose every write goes through `_mutate`, which reads and rewrites the whole run state JSON under `flock`. The repo lease alone (`_acquire_repo_lock`, `_read_repo_lock`, `_write_repo_lock`, `heartbeat`'s second half, `_is_live`, `_age`) is about 80 lines and is self contained once you give it `repo_lock_path`, `pid`, `hostname`, `now`, `ttl_seconds`. The manifest lease is entangled with task records (`_mark_crashed` touches `classify.scan_self_kill`) and should not be lifted.
6. **Tests lift, with two helper files.** The adapter tests need nothing but fixtures and the in file fakes; verify tests need `tests/_repo.py` (temp git repo builder) plus `commit_on_branch` from `tests/test_gitwrite.py`; state tests need nothing. All import a `manifest` fixture TOML, so they inherit finding 1.
7. **One pointer is wrong.** The adapters expose eleven public methods, not nine: `INTERFACE` is nine and `filing.ADAPTER_METHODS` adds `filing_instructions` and `filing_allowed_tools`. The test at `tests/test_adapters.py:161` asserts exactly `INTERFACE | ADAPTER_METHODS`. Slipway drops eight of the eleven.
8. **No packaging at all.** No `pyproject.toml`, no `requirements.txt`, no lint or type tool configured. Standard library only, Python 3.11+ (`tomllib`). Test runner is `unittest`, command below. Slipway can choose its own packaging without a migration.

## 1. Dependency list per file

### `skills/relay/scripts/relay/adapters/__init__.py` (241 lines)

Imports: `re`, `from .. import contracts`.

Uses from `contracts`: only `contracts.UNKNOWN_RETURN` (`"its todo status"`, `contracts.py:567`) inside `unknown_baseline_move`, a closeout sentence Slipway drops.

Lift verdict: lift the first 90 lines (constants, `ConfigurationError`, `reference_hit`, `skipped`, `INTERFACE`, `NETWORK_TIMEOUT_SECONDS`, `SHA_TOKEN_RE`) as is, with no imports from the runner. Drop `unknown_baseline_move`, `status(adapter, ..., cache)`, `board_lag`, `item_confirmed_terminal`, `task_tracker_steps` (reads `manifest.tracker.adapter`, `manifest.execution.mode`, `manifest.tracker.file`, `manifest.tracker.in_review_status`), and rewrite `build(manifest, ...)` to take Slipway's config.

Small and liftable: everything above the `status()` helper. Entangled: `task_tracker_steps` and `build`.

### `skills/relay/scripts/relay/adapters/github.py` (764 lines)

Imports: `json`, `hashlib`, `shlex`, `subprocess`, and from the package `NETWORK_TIMEOUT_SECONDS, OUTCOME_HALTED, OUTCOME_LANDED, reference_hit, skipped, unknown_baseline_move`. No `contracts`, no `gitread`, no `manifest` import. Manifest coupling is in the constructor only:

```
skills/relay/scripts/relay/adapters/github.py:92-97
    def __init__(self, manifest, run=None):
        self._owner = manifest.tracker.owner
        self._project_number = manifest.tracker.project_number
        self._status_field = manifest.tracker.status_field
        self._in_review = manifest.tracker.in_review_status
        self._run = run or make_run(manifest.project.repo)
```

Five fields. `manifest.project.repo` is only the `cwd` for `gh`. Replace with a five field dataclass and the file lifts.

What to drop: `closeout_instructions` (lines 526-573), `filing_allowed_tools`, `filing_instructions` (575-613), `write_tool_patterns`, `closeout_allowed_tools`, `_board_lag`, `_item_confirmed_terminal`, and the whole triple coordinator section from line 616 to the end (`read_triple_snapshot`, `card_state`, `collision_evidence`, `capture_task_delta`, `capture_closeout_delta`, plus the private `_graphql`, `_repository_identity`, `_project_identity`, `_project_item_for_issue`, `_bounded_comments`, `_triple_card`). That removes roughly 450 of 764 lines. What remains is about 200 lines: transport (`make_run`, `_gh`), `_board`, `_items`, `_issue`, `_project_status`, `_comments`, and the reads `candidates`, `ready`, `read`, `status`, `comments_since`, `closing_reference`.

### `skills/relay/scripts/relay/adapters/jira.py` (493 lines)

Imports: `base64`, `hashlib`, `json`, `urllib.error`, `urllib.parse`, `urllib.request`, and from the package `ConfigurationError, NETWORK_TIMEOUT_SECONDS, OUTCOME_HALTED, OUTCOME_LANDED, reference_hit, skipped, unknown_baseline_move`. `os` imported locally in `__init__`. No `contracts`, no `gitread`.

Manifest coupling, constructor only:

```
skills/relay/scripts/relay/adapters/jira.py:112-121
    def __init__(self, manifest, opener=None, env=None):
        ...
        tracker = manifest.tracker
        self._site = (tracker.site or "").rstrip("/")
        self._project_key = tracker.project_key
        self._done = tuple(str(name).lower() for name in tracker.done_statuses)
        self._in_review = tracker.in_review_status
        token = env.get(tracker.token_env)
        email = env.get(tracker.email_env)
```

Seven fields. Drop `closeout_instructions`, `filing_*`, `write_tool_patterns`, `closeout_allowed_tools`, every `GROK_*`, `CLOSEOUT_TOOLS`, `FILING_*` constant (lines 32-81), the triple write methods (`_all_comments`, `_transition`, `_authorize_triple_writes`, `_triple_*`, lines 180-246) and `read_triple_snapshot` (446-493). Keep `_adf_text` (Atlassian Document Format flattener, needed for `description`), `_request`, `_get`, `_issue`, `_comments`, `candidates`, `ready`, `read`, `status`, `comments_since`, `closing_reference`. About 170 lines survive.

### `skills/relay/scripts/relay/verify.py` (370 lines)

Imports: `re`, `time`, `dataclasses`, `datetime`, and `from . import contracts, gitread, gitwrite, manifest as manifest_module`.

Uses:

- `contracts.HALT_LANDED` (`"landed"`), `contracts.HALT_PARTIAL_LANDING` (`"partial_landing"`), `contracts.HALT_TRACKER_WRITE_DENIED` (`"tracker_write_denied"`), `contracts.STATUS_HALTED`, `contracts.STATUS_LANDED`. Five string constants; inline them.
- `gitread`: `GitError`, `status_porcelain`, `current_branch`, `rev_parse`, `fetch`, `default_branch`, `log_oneline`, `log_messages`. `gitread.py` (270 lines) imports only `os` and `subprocess`; its `run()` is `git -C <repo> <args>` via `subprocess.run` with `stdin=DEVNULL` and a 120 second timeout. The eight functions verify needs total about 60 lines. Lift as a trimmed `gitread.py`.
- `gitwrite.task_branch_for` (6 lines, `prefix + task_id`), `gitwrite.mirror_target` (pure string function), `gitwrite.abort_dangling_merge` (used only by `startup_reverify`). `gitwrite.py` is 1210 lines and imports `contracts, gitread`; do not lift it. Copy `task_branch_for` if the PR probe path survives, otherwise drop both.
- `manifest_module.pushes(manifest)` (`manifest.shipping_push is not False`). Drop with the `head_equals_remote` check.
- `manifest.project.repo`, `manifest.project.default_branch`, `manifest.project.branch_prefix`, `manifest.project.mirror`, `manifest.shipping_mode`.
- `record` dict keys: `id`, `baseline_sha`, `landing_ref`, `findings`, `status`.
- `store.records()`, `store.upsert(...)` in `startup_reverify` only.

Verdict: `hand_landing` and `_task_pattern` (lines 275-295) lift as is given `gitread.log_messages`. `Verdict`, `_check`, `_skip`, `_finish` lift with the two halt strings inlined. `verify()` itself needs a rewrite of its signature (section 4). `startup_reverify` is runner specific, drop.

### `skills/relay/scripts/relay/state.py`, lease portion (file is 701 lines)

Imports: `fcntl`, `hashlib`, `json`, `os`, `socket`, `time`, `dataclasses`, `datetime`, and `from . import classify, contracts, gitread`.

Uses in the lease path:

- `contracts.LEASE_TTL_SECONDS = 600` (`contracts.py:775`) as the default TTL; `contracts.LEASE_HEARTBEAT_SECONDS = 60` (`contracts.py:774`) is the cadence the run loop uses, not referenced in `state.py` itself.
- `gitread.repo_identity(repo)` (`gitread.py:237-247`) to key the repo lock on the main working tree (`git rev-parse --git-common-dir`), so two worktrees of one repo share a lease. About 12 lines plus `common_dir` and `run`. Liftable.
- `classify.scan_self_kill` inside `_mark_crashed`, called from `acquire()` when a stale manifest lease is reclaimed. `classify.py` imports `backends, contracts, summary`. This is the entanglement: the manifest lease reclaim marks task records and reads task logs. Not liftable, and Slipway does not need it.
- `contracts.STATE_SCHEMA_VERSION`, `STATUS_*`, `IN_FLIGHT_STATUSES`, `TERMINAL_STATUSES`, `HALT_RUNNER_CRASHED`, `RUN_CRASHED` in the record and terminal code around the lease. Not needed for the repo lease.

The smallest self contained piece is in section 5.

## 2. Credentials and configuration

### GitHub

- Transport: `gh` CLI, shelled out through `subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)` (`github.py:81-88`). Module docstring: "Reads go through `gh`, which already holds the operator's authentication, so Relay carries no secret of its own here." No REST call, no token, no env var read anywhere in the file. GraphQL is also through `gh api graphql` (`github.py:250`), and only in the triple section Slipway drops.
- Commands used by the kept reads: `gh issue view <n> --json id,title,body,state,comments`; `gh issue list --state open --limit 1000 --json number,title,labels,body`; `gh project item-list <project_number> --owner <owner> --format json --limit 500`.
- Config keys (manifest `[tracker]` table, `manifest.py:65-80`): `owner`, `project_number`, `status_field` (the board column name that counts as terminal, optional), `in_review_status`. Plus `[project] repo` as the `cwd`. The `ready` read takes a runtime `source` dict with key `labels` (a list), which native-relay reads from a feeder sidecar `[ready]` table, not from the manifest.
- Hosted implication: Slipway's runtime needs `gh` installed and authenticated (`gh auth login` or `GH_TOKEN` in the environment, which `gh` itself honours). Alternatively rewrite `_gh` to hit REST with a token; the `run` seam means `_issue`, `_board`, and `ready` are the only three call sites.

### Jira

- Transport: `urllib.request` directly, `https://<site><path>`, headers `Authorization: Basic base64(email:token)`, `Accept: application/json`, `Content-Type: application/json`, 30 second timeout (`jira.py:137-161`). Module docstring: "Credentials are read from the environment once, at construction, and never leave this object."
- Env vars: the names are configurable. Defaults from `manifest.py:309-312`: `token_env` defaults to `"JIRA_API_TOKEN"`, `email_env` defaults to `"JIRA_EMAIL"`. Missing either raises `ConfigurationError` at construction (`jira.py:123-128`), before any request.
- Config keys: `site` (host only, no scheme, e.g. `example.atlassian.net`), `project_key`, `done_statuses` (list, compared lowercase), `in_review_status`, `token_env`, `email_env`.
- Endpoints: `GET /rest/api/3/issue/{key}?fields=summary,description,status,comment,project` and `GET /rest/api/3/search/jql?jql=...&fields=...&maxResults=100` with `nextPageToken` paging, 20 page cap. The `ready` read takes a runtime `source` dict with key `jql`.

### Markdown adapter

Not in scope. Noting only that it reads a tracker file at the remote default branch via `gitread.show`, so it is the one adapter coupled to git.

## 3. Mapping the three Slipway reads onto the interface

| Slipway need | Adapter method | Shape returned |
|---|---|---|
| Read a card (title, body, labels, status) | `read(task_id)` | `{"id", "title", "description", "status"}`; GitHub `status` is the issue `state` (`OPEN`/`CLOSED`), Jira is the status name. **No labels.** Labels appear only in `ready()`'s items. Add `labels` to `read` on both adapters (GitHub: add `labels` to `ISSUE_FIELDS`; Jira: add `labels` to `ISSUE_FIELDS` and read `fields.labels`). |
| List cards by label or query | `ready(source)` | `([{"id", "title", "description", "labels"}], reason)`. GitHub filters `gh issue list` by `source["labels"]` (all must match); Jira runs `source["jql"]`. Returns `(list, reason)`, never raises; empty with a reason distinguishes "unreadable" from "nothing ready" (`adapters/__init__.py` docstring). |
| Read current status | `status(task_id)` | `{"status", "terminal", "reference", "skipped"}`. `terminal` is the adapter's own judgement: GitHub, `state == CLOSED` or board column equals `status_field`; Jira, status name in `done_statuses`. `skipped` is a reason string when the read failed. |

`candidates()` is a fourth read (all not done cards on the project or board) Slipway may want for a dashboard but did not ask for.

What the other six assume about the runner, and why Slipway drops them:

- `comments_since(id, baseline)`: assumes the runner recorded a `baseline_comment_id` before launching the Task process so it can read what the process wrote. Slipway does not launch a process that writes comments.
- `closing_reference(id, ref)`: assumes a Closeout process commented the landing sha or PR URL on the card. Slipway's own verify would use this if the builder or Slipway itself comments the PR URL. Keep it; it is nine lines and `reference_hit` is shared.
- `write_tool_patterns()`: tool name prefixes (`mcp__atlassian__`, `gh issue`, `gh project item-edit`) the runner's classifier uses to turn a denied tool call in a Claude transcript into a `tracker_write_denied` halt. Pure runner concern.
- `closeout_allowed_tools(backend)`: the tool allowlist for the Closeout process, with Grok spellings. Runner concern.
- `closeout_instructions(outcome, return_to, backend, baseline_unknown)`: prose for the Closeout brief telling a model how to move the card with `gh` or Atlassian MCP. Runner concern.
- `filing_instructions`, `filing_allowed_tools`: the browser test loop's Filing process. Runner concern.

The deliberate design rule behind all six, from `adapters/__init__.py:7-10`: "No method here writes... every write goes through a Task or Closeout process instead, so a defect in the runner can never move a card." If Slipway wants to move cards itself (for example label a card `slipway:dispatched`), that is a new write surface the current adapters do not have, except the Jira triple REST writes Slipway is not lifting.

## 4. `verify.verify` in detail

Signature (`verify.py:117-118`):

```
def verify(manifest, record, adapter, scope=SCOPE_FULL, pr_probe=None, do_fetch=False,
           env=None, now=time.time):
```

Inputs actually consumed:

- From `manifest`: `project.repo`, `project.default_branch` (falls back to `gitread.default_branch(repo)`, i.e. `refs/remotes/origin/HEAD`, then `"main"`), `project.branch_prefix`, `project.mirror`, `shipping_mode` (`"local_merge"` or `"pr_terminal"`), `shipping_push`.
- From `record`: `id`, `baseline_sha`, `landing_ref`, `findings` (only to copy `tracker_write_denied` findings into evidence).
- From `adapter`: `status(task_id)` and `closing_reference(task_id, landing_ref)`.
- `pr_probe(branch)` returning `{"url", "number", "state", "ci"}`, built by the caller from `gitwrite.find_pr` and `gitwrite.poll_ci` (both shell out to `gh pr list --head <branch> --json url,number` and `gh pr checks <branch>`).

Git reads, in order, each through `_safe` so a `GitError` becomes a blocking skip:

1. `gitread.fetch(repo)` only if `do_fetch and pushes`.
2. `gitread.status_porcelain(repo)` for `tree_clean`.
3. `gitread.current_branch(repo)` for `on_default`.
4. `gitread.rev_parse(repo, default)` and `rev_parse(repo, "origin/" + default)` for `head_equals_remote`.
5. `gitread.log_oneline(repo, baseline, default)` for `new_commit_since_baseline`.
6. `gitread.rev_parse(repo, "<remote>/<destination>")` for `mirror_equals_head`.
7. `gitread.log_messages(repo, baseline_sha, local_sha)` inside `hand_landing` when the record has no `landing_ref`.

Tracker reads: `adapter.status(task_id)` for `card_terminal`; `adapter.closing_reference(task_id, landing_ref)` for `closing_reference`, skipped when `hand_landing` already found a commit naming the task.

Return: a `Verdict` dataclass (`verify.py:54-78`) with `scope`, `checks` (dict of check name to `{"result": pass|fail|skipped, "evidence": {...}, "blocking": bool}`), `landed`, `halt_class`, `at` (ISO UTC), `evidence` (`landing_ref`, `baseline_sha`, `findings`). `as_dict()` is what the state file stores.

Halt classes it can set (`_finish`, lines 317-344): `landed` when no fails, no blocking skips, and both tracker checks pass; `partial_landing` when only tracker checks failed and nothing was blocking; otherwise `None` and the caller names the class. A blocking skip never yields a class, by design: "the runner did not read enough to say either way".

Coupling verdict: `verify()` is tightly bound to the manifest's shipping model, not to the record. `record` is a plain dict with four keys. Rewriting as a function of `(repo, baseline_sha, head_sha, task_id, tracker_status)` works for the subset Slipway needs:

- `new_commit_since_baseline`: `log_oneline(repo, baseline_sha, head_sha)` non empty.
- `closing_reference`: `hand_landing(repo, baseline_sha, head_sha, task_id)` finds a commit naming the task between baseline and head, or the adapter finds a comment naming `landing_ref`. For a PR merged through a native merge queue, the merge commit's message normally carries the PR number, so `_task_pattern` matching `#<n>` for numeric ids (`verify.py:275-280`) does the job for GitHub when the card id equals the issue number and the PR body says `Closes #<n>`; for Jira keys the pattern is the key as a whole word.
- `card_terminal`: `tracker_status["terminal"]` and not `tracker_status["skipped"]`.
- `_finish` rule as is.

What does not fit the five tuple and should be dropped or re homed: `tree_clean` and `on_default` (assume the verifier runs inside the working checkout it merged into; Slipway verifies a remote), `head_equals_remote` (the remote is the landing in Slipway, so compare against `origin/<default>` after a fetch, or ask the host API), `mirror_equals_head` (no mirror), `pr_open` and `ci_green` (a merged PR is `MERGED`, not `OPEN`; native-relay's `pr_terminal` mode is refused by `manifest.validate` as unimplemented, `manifest.py:40-46`, so these two checks have never run live).

Recommendation for the plan: lift `Verdict`, `_check`, `_skip`, `_finish`, `_task_pattern`, `hand_landing`, and the four checks above into a new `verify.py` with the signature Slipway wants, and lift a trimmed `gitread.py` (`run`, `GitError`, `rev_parse`, `fetch`, `log_oneline`, `log_messages`, `default_branch`, `common_dir`, `repo_identity`). Treat it as a derived file with the origin named in its docstring, not a copy.

## 5. The lease

Two leases, one class. Slipway wants the repo lease.

Inputs (`StateStore.__init__`, `state.py:94-118`): `manifest_path`, `repo_path`, `home`, `now` (injectable clock), `pid`, `hostname`, `ttl_seconds` (default `contracts.LEASE_TTL_SECONDS = 600`), `observer`.

Where it stores: `~/.relay/repos/<sha256(repo_identity)>.lock` as JSON, or `$XDG_STATE_HOME/relay/repos/...` (`state.py:78-82, 114`). Payload written by `_write_repo_lock`: `{"holder_pid", "hostname", "manifest", "acquired_at", "heartbeat_at", "ttl_seconds"}` via temp file and `os.replace`. The physical serialisation is a separate `flock` on `<lock path>.flock` (`state.py:355-357`), taken `LOCK_EX` for the read-modify-write and released by closing the fd.

Liveness (`_is_live`, `state.py:283-291`): `now() - heartbeat_at < ttl_seconds`; an unparseable stamp counts as live "so a hand edited file is never stomped". Heartbeat cadence is the caller's job, `LEASE_HEARTBEAT_SECONDS = 60`. `heartbeat()` (`418-443`) re-stamps and returns `False` if the lease is no longer this holder's and manifest's, which the run loop treats as a halt.

Ownership identity: `holder_pid + hostname` for "mine", and `manifest` path for "same run". A second manifest naming the same repo gets `AcquireResult(LOCKED, other_manifest=...)` (`352-371`); the test at `tests/test_state.py:72` covers it.

Smallest self contained piece: `_read_repo_lock`, `_write_repo_lock`, `_acquire_repo_lock`, the repo half of `heartbeat`, the repo half of `release` (`os.remove` when holder and manifest match), `_is_live`, `_age`, `_holder`, `_is_mine`, `_iso`, `_epoch`, `sha256_of`, `base_dir`, plus `AcquireResult` and the three code strings. About 100 lines, no `contracts` beyond the TTL integer, no `classify`. Needs `gitread.repo_identity` if the worktree sharing rule matters to Slipway (it does if builders work in worktrees of one clone).

Hosted implications: `fcntl.flock` is POSIX only and process local to one machine (`README.md:277-281`: "The lease uses `fcntl`, so Windows is out until that changes"). A hosted Slipway with more than one process or host cannot use this as its only lock; the file plus heartbeat pattern is still the right shape but the store would need to be shared (database row, or a git ref the way native-relay's triple mode does with `refs/relay/claims/*` in `contracts.py:784-785`, which is explicitly "not lifted" territory). The plan should decide whether the lease is per machine (lift as is) or per deployment (lift the shape, swap the store).

## 6. Tests

Runner: `unittest`, discovery from the repo root. Helper `tests/_paths.py` puts `skills/relay/scripts` on `sys.path` and imports `tests/_nonet.py`, which monkeypatches `urllib.request.OpenerDirector.open` and `subprocess.Popen.__init__` to raise `NetworkReached` on any real opener call or any launch of `gh`, `curl`, `wget`, `ssh`. Worth lifting as is: it is the enforcement behind "no test touches a network".

| Piece | Test file | How it stubs |
|---|---|---|
| `adapters/__init__.py` contract | `tests/test_adapters.py` `SharedContract` (147-257), `ReturnTo` (259-311) | Builds all three adapters from `tests/fixtures/manifests/complete.toml` with string replacements, `tests/test_adapters.py:107-144` |
| `github.py` | `tests/test_adapters.py` `GitHub` (802-1004), `ReadySource` (464-527), `TripleGitHubSnapshot` (1006-1116, drop) | `DispatchRun` (71-98): a `run(args, timeout)` callable that routes `gh project ...` to `tests/fixtures/tracker/github_project_items.json` and `gh issue view <n>` to `github_issue_open.json` / `github_issue_closed.json`; `_JsonOpener`/`_Proc` for `gh issue list` |
| `jira.py` | `tests/test_adapters.py` `Jira` (529-800) | `FakeOpener` (38-69): routes a URL substring to `jira_issue_open.json`, `jira_issue_done.json`, `jira_search.json`; asserts Basic auth header and the 30 second timeout at 576 |
| `verify.py` | `tests/test_verify.py` (525 lines, 11 classes) | Real git against a temp repo from `tests/_repo.make_repo` (bare origin, `origin/HEAD` set); tracker through `tests/_fakes.FakeAdapter`; `commit_on_branch` imported from `tests/test_gitwrite.py:20` |
| lease | `tests/test_state.py` `Acquire` (45-204), `Heartbeat` (206-290), `Layout` (655-680) | `FakeClock`, temp `HOME`, real `fcntl`; no git, the repo is an empty dir so `repo_identity` falls back to the path |

Fixtures are six small JSON files (6 to 25 lines each) under `tests/fixtures/tracker/` and one manifest TOML under `tests/fixtures/manifests/`. Everything else under `tests/fixtures/` (transcripts, backends, stdout) belongs to the classifier and is not needed.

Can they lift? Yes for the adapter and lease tests, with the manifest fixture replaced by Slipway's config construction (every test builds a manifest through `mf.load`, so that is one helper to rewrite). `test_verify.py` lifts the `FullScope`, `LandedByHand`, `GitReadFailures` classes; `CodeScope`, `Mirror`, `PrTerminalMode`, `NoPush`, `StartupReverify`, `AgainstTheRealMarkdownAdapter` test checks Slipway drops. `tests/_repo.py` and `tests/_fakes.py` (`FakeAdapter`, `FakeRun`) lift whole.

## 7. Toolchain

- Python: 3.11 or later required (`README.md:279`, `tomllib`). The machine runs 3.14.6. Slipway need not keep `tomllib` since it has no manifest.
- Packaging: none. No `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements*.txt`, `tox.ini`, `pytest.ini`, `conftest.py`. The package is `skills/relay/scripts/relay/` and is reached by `sys.path` insertion in `tests/_paths.py`, because "a Claude Code plugin ships its scripts next to the skill that calls them, not on the Python path".
- Dependencies: standard library only. Confirmed for all five files and `gitread.py`.
- Test command, exact (`CLAUDE.md:56`, `README.md:452`): `python3 -m unittest discover -s tests` from the repo root. Full suite is about fourteen minutes, 2359 tests as of 2026-09-28. Single module from `tests/`: `python3 -m unittest test_adapters`.
- Lint and type check: none configured. The only mention of `ruff` or `pytest` in the repo is a note about an external project's battery (`docs/plans/2026-09-07-native-mode-plan.md:22`). Slipway is free to adopt `pyproject.toml` with `pytest` and `ruff` without migrating anything.

## 8. Hard coded values

A grep of the five files plus `gitread.py` and `contracts.py` for `/Users/`, `pgutowski`, `philgutowski`, `integrel`, `Cratekit`, `IW-`, `atlassian.net`, `github.com/`, hostnames returned nothing. Specifics worth knowing:

- `state.py:82`: `~/.relay` as the state root, overridable by `XDG_STATE_HOME`. Slipway will want its own name.
- `jira.py`: `JIRA_API_TOKEN` and `JIRA_EMAIL` appear only in prose for Grok closeouts (`jira.py:391, 441`); the real defaults are in `manifest.py:309-312`.
- `jira.py:37-77`: Atlassian MCP tool names (`mcp__atlassian__getJiraIssue` and so on). Not account specific, but Claude and Grok tooling specific; dropped with closeout.
- Comments reference project history (`#62`, "The first Cratekit run", `verify.py:253`; `IW-` appears as a branch prefix example in `tests/test_verify.py:62`). Comments only, no behaviour.
- `tests/_repo.py:30-31`: git identity `Relay Test <relay@example.invalid>`. Fine.
- `contracts.py:784-785`: `refs/relay/claims` and `refs/relay/integration`, the triple mode ref namespace. Not lifted.

## Contradictions with the scout's pointers

- "nine methods": `INTERFACE` has nine, but both adapters expose eleven public methods and the suite asserts eleven (`tests/test_adapters.py:161-169`, `filing.ADAPTER_METHODS` at `skills/relay/scripts/relay/filing.py:34`).
- "GitHubAdapter": correct name, but the pointer's framing of it as a tracker adapter undersells that half the file (lines 616-764 plus the private GraphQL methods) is a triple coordinator collision detector with its own module level functions.
- "verify.py (verify(), hand_landing())": correct, and `startup_reverify` is a third public function that reaches into the state store. Not for lifting.
- "state.py (repo lease, _is_live, heartbeat, fcntl)": correct names, but the repo lease is not a module level thing; it is methods on `StateStore` and `acquire()` takes the manifest lease first and rolls it back if the repo lease is held (`state.py:343-350`). The lift has to cut the class, not import it.
- `CONCEPTS.md` sections "Verify-landed" (716), "Landed" (728), "Lease" (60) exist as named. "Lease" is under "The loop", not under "Outcomes".
