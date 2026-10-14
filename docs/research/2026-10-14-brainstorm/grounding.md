# Grounding dossier: Phactory v1 (verbatim pointers, no interpretation)
Written 2026-10-14 by the grounding scout (relayed; scout ran read-only).

## 1. Note (Phactory)
Path: /Users/pgutowski/Documents/PhilAI/Phactory/docs/ideation/2026-10-14-software-factory.md
- "Rules learned" (from the runner): "Never trust the agent's own report. The agent's closing claim is a claim. Landing is decided from git and the tracker, read by the tool itself."
- "From the lanes": "Verify in a scratch join and land the exact verified commit. Merge onto main in a throwaway worktree with its own environment, run the gate there, fast forward main to that commit. Never re merge."
- "Decisions settled": "From the runner: the tracker adapters, verify landed, the lease. From the lanes script: the land path (...), the handoff shape, and the status read."
- "Acceptance targets": "onboard... three kinds of project: GitHub with a GitHub project board, Bitbucket with Jira, and one it has never seen, and land one scored card on each."
- "Open": "Which trackers and hosts the first version must speak: at least GitHub and Jira, and the first Bitbucket repository is already in view."

## 2. Runner: native-relay
Root: /Users/pgutowski/Documents/PhilAI/native-relay

Identity and shape
- README.md:1-9: "Run a list of pre-defined tasks through a native plan, build, review, verify, record pipeline, one fresh headless process per task... The runner then runs the project gate, merges, pushes unless the manifest turns pushing off, verifies the landing itself, and launches a short Closeout process."
- README.md:26-39, "What a project needs before Relay can run": independent tasks; durable state; "An external gate that refuses broken changes"; "Relay never defines that; the manifest names only the one gate command the runner itself runs before the merge."
- README.md:50-51: Manifest "Names the tracker adapter, the task list, the shipping mode, any mirror rule, the disallow patterns, and the docs root the closeout may write a learning under."
- README.md:53-55: "It holds no project knowledge and never writes to a tracker."

Tracker adapters
- skills/relay/scripts/relay/adapters/__init__.py:1-22 (docstring): "An adapter is the whole of what Relay knows about a tracker... No method here writes... the adapter's public surface is exactly the nine methods below."
- Interface block: candidates(), ready(source), read(id), status(id), comments_since(id, baseline), closing_reference(id, ref), write_tool_patterns(), closeout_allowed_tools(backend=None), closeout_instructions(outcome, ...).
- adapters/__init__.py:73 reference_hit, :108 status, :225 build.
- Files: adapters/github.py (764 lines, class GitHubAdapter at :91), adapters/jira.py (493 lines, class JiraAdapter at :111), adapters/markdown.py (224 lines).
- Bitbucket: grep for "bitbucket" in runner .py files returned no matches.
- adapters/__init__.py docstring: "`ready` ... labels for GitHub, a JQL query for Jira, and nothing at all for markdown."

Verify landed
- skills/relay/scripts/relay/verify.py:117 `def verify(manifest, record, adapter, scope=..., ...)`. Docstring: "Never raises for a tracker or git read that fails; an unreadable input becomes a blocking skip so the caller sees `not landed`, not a crash."
- verify.py:283 `def hand_landing(repo, baseline_sha, head_sha, task_id)`: "The newest commit between the baseline and the head whose message names the task... This is how a task landed by hand between runs is recognised."
- CONCEPTS.md:716-720 (Verify-landed): "It never reads the Task process's exit code, printed result, or claims as evidence, because a headless run has every incentive to report success."
- CONCEPTS.md:728-731 (Landed): "code that merged while the card stayed put is a partial landing, not a landing, and it halts the run."
- CONCEPTS.md:11-12: "The Runner decides a Task's outcome by Verify-landed, which consults git and the Tracker through a Tracker adapter, never the Task process itself."

Lease
- CONCEPTS.md:60-62: "There are two: one over the Manifest... and one over the target repository, so two different Manifests naming the same repository cannot merge into it at the same time."
- CONCEPTS.md:70-71: "A Lease is renewed on a heartbeat rather than held for the length of the work, so it expires on its own if a Runner dies."
- skills/relay/scripts/relay/state.py:1-10 (docstring): "one JSON file per manifest, a lease... A second, repo level lease..."
- state.py:108-109: "Key the repo lease on the main working tree, not this checkout's path, so a worktree of the same repository cannot take a second repo lease."
- state.py:274-291 `_is_live`: "A lease is live while its heartbeat is younger than its TTL."
- README.md:277-281 (Platform): "The lease uses `fcntl`, so Windows is out until that changes."

Halt rules
- README.md:408-411: "The run halts rather than continuing past an outcome it cannot confirm... Repair by hand, then run again: the runner re-verifies what halted and resumes at the first task that did not land."
- CONCEPTS.md:356-359: "Halt class ... Every class carries the evidence its Cause line needs."
- CLAUDE.md (repo): "Halt classes are a closed set in `contracts.py` (KTD6). A new outcome is a finding attached to a record, not a new class."

Land path (runner)
- skills/relay/scripts/relay/gitwrite.py:970 `def local_merge_tail(...)`: "The fixed local merge sequence of R50, from the task process's exit to a pushed default branch."
- gitwrite.py:894 `def run_gate(repo, command, log_path, ...)`: "Never a shell string (R9); a nonzero exit strands the branch."
- Sequence in local_merge_tail (approx. lines 1000-1063): checkout(repo, branch); claude_dir_backstop; `gate = run_gate(repo, gate_command, ...)` in the repo checkout; checkout(repo, default_branch); merge_no_ff(repo, branch, ...); push(repo, ["origin", default_branch], ...).
- So the runner merges in the main checkout. No scratch join, no fast forward. The gate runs on the task branch in the repo.

Trace and records per task
- launch.py:178: `os.path.join(home, ".claude", "projects", "*", session_id + ".jsonl")`, the CLI's session transcript.
- state.py:78-83 `base_dir`: `XDG_STATE_HOME/relay` or `~/.relay`.
- run.py:444: `cfg.store.path("logs", item.task.id + ".stdout.log")`.
- run.py:476: `cfg.store.path("digests", item.task.id + ".json")`, written by classify.write_digest.
- run.py:521: `cfg.store.path("gate", item.task.id + ".log")`.
- CONCEPTS.md:280-283 (Digest): "The record the Runner builds by reading a Task process's transcript once, holding the Envelope alongside what the Runner itself observed..."
- classify.py:1-12 (docstring): "The transcript is the jsonl file the CLI writes, one JSON object per line... The last `assistant` line with a text block is the final message, where the return envelope lives."
- CONCEPTS.md:333-337 (Learning judgment): "judging whether a Task produced a learning worth keeping and writing it if so, as one markdown file under the Manifest's docs root."
- No separate action log: tool calls come only from the transcript (classify.py joins tool_use to tool_result).

Evals and grading
- Search for eval, grader, grading, score in skills/ and tests/ .py files: no eval or grading code.
- Only hits: skills/relay/scripts/relay/pair.py:8 ("The rubric in the skill is judgment, not a table"); skills/relay/references/backend-rubric.md (backend assignment, not grading).
- CONCEPTS.md:333-340 is the closest: the Closeout judges "whether a Task produced a learning". Not a score.

Hard-coded identity
- Search for phactory|cratekit|philgutowski|electricpassage|/Users/ in non-test runner .py files: no matches.
- CLAUDE.md (repo) names GitHub project "Native Relay (project 6 on philgutowski)" and "Compound Relay (project 4)". Repo-level docs, not code.

## 3. Lanes script: board-lanes
Paths: /Users/pgutowski/Documents/PhilAI/agent-config/skills/board-lanes/SKILL.md and scripts/lanes.py

Verbs and identity
- lanes.py:1-20 (docstring): "Hand cards to a board's lanes, land what the lanes build, and launch the feeder, from one file." Verbs: handoff, land, launch, progress, queue, land-queued, up, status.
- SKILL.md:14: "Everything that was hand work when this was first set up (Cratekit, 2026-10-09)..."
- SKILL.md:27: "The first instance is `~/.relay/manifests/cratekit-board.lanes.toml`; copy it for the next board."

Land path
- lanes.py:485 `def land_one(cfg, issue, branch=None, push=False, dry_run=False, expect_tip=None)`: "Join, gate, fast forward and optionally push one lane branch... `expect_tip` is the tip the watcher verified."
- lanes.py:520 `git(repo, "worktree", "add", "--detach", join, main, quiet=False)`
- lanes.py:533-534 (approx.) `merged = git(join, "merge", "--no-ff", "-F", fh.name, ref, ...)`
- lanes.py:537 `for cmd in board["gate"]:` then `out = sh(cmd, cwd=join, check=False)`
- lanes.py:551 `git(repo, "merge", "--ff-only", sha, quiet=False)`
- lanes.py:552 (approx.) `git(repo, "worktree", "remove", "--force", join, quiet=False)`
- lanes.py:577 (approx.) `git(where, "push", remote, "%s:refs/heads/%s" % (sha, main), quiet=False)`
- lanes.py:550 (refusal): "a relay run started while the gate ran; the verified join %s waits in %s."
- SKILL.md:69-71: "joins it onto main in a scratch worktree under the board's worktree root with its own environment, runs the gate and the pre merge checks in the join, then fast forwards main to that exact commit."

Handoff shape
- lanes.py:364 `def cmd_handoff(cfg, args)`. Body built at approx. lines 386-393: heading "**%s**, %s.%s", date, "Take it after #N."; "- Base: `%s` on `%s`%s." with "local only, same machine" alternative; "- Branch: `%s`."; "- Rules: %s." % lane["rules"]; "- Files another build is editing: %s."; "- Done: %s." % lane["done"]
- lanes.py:373-374: "a remote builder branches from the remote, so push first or pick a pushed base."
- SKILL.md:63-64: "Adds the lane's label, posts the handoff comment in the fixed shape (base, branch, rules, ...)."

Status read
- lanes.py:308 `def cmd_status(cfg, args)`: reads feeder_status(board), git log -1 main, rev-list ahead/behind against remote/main, project_items(board) via gh project item-list, open_cards(board, lane label) per lane, git ls-remote --heads, gh pr list. Read only.
- SKILL.md:58: "**`status`**, read only."

Hard-coded to one board or machine
- lanes.py:79: `["gh", ..., "--repo", board["github"], ...]` (repo comes from config).
- lanes.py:229: `needle = "relay_cli.py run %s" % board["manifest"]`
- lanes.py:183 (comment): "2026-10-09 20:18, the Grok agents, the watcher and the operator share one user."
- lanes.py:411: "On Cratekit, 2026-10-09, `python3` was 3.14 while the primary venv ran 3.12."
- lanes.py:525: "a repo whose pre-commit scan refuses a home directory or an account name (Cratekit's R1)..."
- lanes.py:556: "the way METHOD's merge step does by hand."
- lanes.py:790-791: `subprocess.run(["pbcopy"], input=text, ...)` (macOS clipboard).
- lanes.py:816: `["osascript", "-e", ghostty_tab_script(...)]`; lanes.py:869 `pgrep -f Ghostty.app/Contents/MacOS/ghostty` (macOS and Ghostty only).
- lanes.py:878-888: `if name == "grok":` and clipboard steps for "Grok Build."
- SKILL.md:193: `post_cycle = ["python3", "/Users/pgutowski/.claude/skills/board-lanes/scripts/lanes.py", ...]` (hard absolute path).
- SKILL.md:8-10: "a Relay feeder, a local fable builder session, and named remote Grok agents ('Cratebot', 'Cratebot 2', addressed as `grok-1`...)."
- SKILL.md:136-137: "A board with no feeder at all sets `feeder = false`... (zig-board, 2026-10-11...)."

## 4. Note items with no footprint in these two systems
- Eval or grading: none in either system.
- Bitbucket adapter: none in the runner. The lanes script is GitHub-only (gh).
- Hosted-agent transcript retrieval: no reference. The runner reads only local ~/.claude/projects/*/*.jsonl (launch.py:178).
- Per-task trace folder with transcript, actions and output: not present. The runner writes logs, digests and gate logs under ~/.relay.
