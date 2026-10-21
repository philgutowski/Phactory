---
name: dispatch
description: Dispatch Slipway's own ready cards to Claude Code cloud sessions. Use when asked to dispatch, build the next cards, run the factory, or check which cards are ready.
---

# Dispatch: hand the ready cards to cloud sessions

This is the conductor's procedure. A conductor is a Claude Code session (a scheduled one or a person's) that finds the cards the ready rule allows, posts each handoff on its issue, creates its branch, and starts one cloud session per card. The explainer is `docs/explainers/build-loop.md`; the ready rule and the handoff shape are in `factory/handoff.py`.

Everything below is idempotent: a card with a branch is dispatched and is skipped, so running the procedure twice dispatches nothing twice.

The conductor follows "Asking the operator" in `CLAUDE.md`: it decides what it can, keeps the factory moving around anything only the operator can supply, and asks for at most one thing per run, as a single step or a multiple choice question with its recommendation first.

## Where the conductor runs

The conductor needs two things a session does not always have: the `create_session` tool from the claude-code-remote tools, and GitHub access to this repository. A scheduled task that starts a fresh session gets neither today; the first live run on 2026-10-20 stopped at step 0 for exactly that reason. So the scheduled task fires into a standing conductor session that has both, and each firing resumes that session with this procedure. If a run finds either one missing, it says so in one line and stops, because without them it can neither read the gate nor start a builder.

## 0. Check the gate is live

The factory never runs ahead of its review gate. Read the rules on `main` and confirm `review` is a required status check:

```sh
gh api repos/philgutowski/slipway/rules/branches/main \
  --jq '[.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context]'
```

If `review` is not in the list, dispatch nothing: a pull request would auto merge on `ci` alone, unreviewed. Say in the report that the review check is not yet required and name the setup steps in `docs/explainers/build-loop.md`.

## 1. Read the board

Work from a clean checkout of `main` with the environment from `CLAUDE.md` set up (the venv, so `factory` can import the card parser). Fetch the three inputs with the REST API; GraphQL is not reachable from a cloud session, so use `gh api repos/...`, never `gh issue list`.

```sh
REPO=philgutowski/slipway
S=$(mktemp -d)
# Page by number: the proxy refuses the repositories/<id> links --paginate follows.
for kind in "issues?state=all" "pulls?state=open" "branches?"; do
  name=${kind%%\?*}; n=1; echo "[" > "$S/$name.json"
  while page=$(gh api "repos/$REPO/$kind&per_page=100&page=$n") && [ "$page" != "[]" ]; do
    [ $n -gt 1 ] && echo "," >> "$S/$name.json"; echo "$page" >> "$S/$name.json"; n=$((n + 1))
  done
  echo "]" >> "$S/$name.json"
done
git fetch origin main
BASE=$(git rev-parse origin/main)
```

## 2. Ask the ready rule

```sh
. .venv/bin/activate
python -m factory.handoff ready --issues "$S/issues.json" --pulls "$S/pulls.json" \
  --branches "$S/branches.json" --assume-done U2 --assume-done U4
```

It prints every open cloud card with `ready` true or the reasons it is not: a dependency still open, a branch already there, a declared file in flight on another card. `--assume-done U2 --assume-done U4` name the operator spikes whose findings are recorded while their issues stay open: U2's in `docs/spikes/merge-paths.md` (steps 4 to 6 open), which U9 needs, and U4's in `docs/spikes/hatchet.md` (step 1, sizing on the rented server, open), which U14 needs; add another unit only when its findings are recorded the same way, and say so in the issue comment.

## 3. Choose, within the caps

- At most **three** cards in flight at once (open pull requests from `slipway/` branches plus sessions started this run). Each in flight card holds its declared files, and each pull request that lands moves `main` under the others, so more in flight means more rebases and more held files, not faster landing.
- At most **two** new dispatches per run.
- Prefer, in order: a plan unit on the critical path in the order of "Reorder of 2026-10-21" in the plan (U14, then U25, then U26, U19, U21, U20, U16, U27, U28, U29, then U12, U18, U15, U17, U22, U23), then follow ups oldest first.
- Skip a card whose issue has a comment from the operator saying to hold it.
- Two cards chosen in the same run must not share a declared path. The ready rule checks each card only against the cards already in flight, so check the chosen pair against each other and take the next card down when they overlap.

## 4. For each chosen card

Render the handoff, post it on the issue, create the branch at the base, start the session.

```sh
N=12   # the issue number
python -m factory.handoff render --card $N --base "$BASE" --repository "$REPO" \
  --issues "$S/issues.json" --pulls "$S/pulls.json" --branches "$S/branches.json" \
  --assume-done U2 --assume-done U4 > "$S/handoff-$N.md"
BRANCH=$(grep -m1 '^2. Branch:' "$S/handoff-$N.md" | awk '{print $3}')
gh api -X POST "repos/$REPO/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$BASE"
gh api -X POST "repos/$REPO/issues/$N/comments" -F body=@"$S/handoff-$N.md"
```

If the proxy refuses either `gh api` write (it has refused git refs and issue comments with a 403), make the same two writes with the GitHub MCP tools: `create_branch` from `main`, checking that the new ref's commit is `$BASE`, then `add_issue_comment` with the handoff text exactly as rendered.

Creating the branch first is what marks the card dispatched for the next run, and the board workflow moves the card to In progress on it. Then start the builder with `create_session` from the claude-code-remote tools:

- `prompt`: the handoff text, exactly as rendered, plus any note the conductor adds (a recorded spike the card leans on, a file to read first).
- `source_url`: `https://github.com/philgutowski/Slipway`, and `source_revision`: the branch name. An outcome branch needs a GitHub source, and starting on the branch saves the session a checkout.
- `outcome_branch`: the branch name, so pushes land on it with no suffix.
- `model`: `claude-opus-5-5` by default. Use `claude-fable-5-1` for the units whose correctness rests on concurrency, authorization, or a credential: U9, U14, U25, U18, U19, U22.
- `title`: the card's title.
- `tags`: `["slipway-card:<issue number>"]`.
- `permission_mode`: inherit. Never `plan`: nobody is watching to approve one.

Append one line to the issue comment with the session id, so the next conductor can read what happened.

## 5. Leave the rest alone

Do not open, approve, merge, or close pull requests. One thing the conductor does do: the ruleset requires a branch to be up to date with `main`, so a pull request from a `slipway/` branch can sit green but behind after another card lands. When its builder session is idle, update the branch with the GitHub MCP tool `update_pull_request_branch` (a merge commit, which keeps the builder's checkout valid) and send the builder one message saying so, telling it to run `git pull` on its branch before its next push. The builder session opens its pull request, enables auto merge, and watches the `ci` and `review` checks; auto merge lands it when both are green. Nobody approves by hand. If a builder session has failed (its pull request is red for a day with no push, or the session ended without a pull request), the conductor re dispatches it without asking: first send the old session one message asking it to stop, then start a new builder with `create_session` on the same branch, with the original handoff plus a note on what the old session left behind, and say so in one line on the issue. A card that fails twice is the one case for the operator, asked as a single multiple choice question: rewrite the card, split it, or drop it.

## 6. Report

End with a short note: which cards were dispatched with their session ids, which are in flight, which are blocked and on what, and the decisions the conductor made on its own. Name something for the operator only when it is the actual bottleneck and only the operator can supply it, and then ask for it in one step, as a multiple choice question when that fits. If nothing was ready and nothing changed, say that in one line.
