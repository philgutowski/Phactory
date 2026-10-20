---
name: dispatch
description: Dispatch Slipway's own ready cards to Claude Code cloud sessions. Use when asked to dispatch, build the next cards, run the factory, or check which cards are ready.
---

# Dispatch: hand the ready cards to cloud sessions

This is the conductor's procedure. A conductor is a Claude Code session (a scheduled one or a person's) that finds the cards the ready rule allows, posts each handoff on its issue, creates its branch, and starts one cloud session per card. The explainer is `docs/explainers/build-loop.md`; the ready rule and the handoff shape are in `factory/handoff.py`.

Everything below is idempotent: a card with a branch is dispatched and is skipped, so running the procedure twice dispatches nothing twice.

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
gh api --paginate --slurp "repos/$REPO/issues?state=all&per_page=100" > "$S/issues.json"
gh api --paginate --slurp "repos/$REPO/pulls?state=open&per_page=100" > "$S/pulls.json"
gh api --paginate --slurp "repos/$REPO/branches?per_page=100" > "$S/branches.json"
git fetch origin main
BASE=$(git rev-parse origin/main)
```

## 2. Ask the ready rule

```sh
. .venv/bin/activate
python -m factory.handoff ready --issues "$S/issues.json" --pulls "$S/pulls.json" \
  --branches "$S/branches.json" --assume-done U2
```

It prints every open cloud card with `ready` true or the reasons it is not: a dependency still open, a branch already there, a declared file in flight on another card. `--assume-done U2` names the operator spike whose findings U9 needs and are recorded in `docs/spikes/merge-paths.md` while its issue stays open for steps 4 to 6; add another unit only when its findings are recorded the same way, and say so in the issue comment.

## 3. Choose, within the caps

- At most **three** cards in flight at once (open pull requests from `slipway/` branches plus sessions started this run). Each in flight card holds its declared files, and each pull request that lands moves `main` under the others, so more in flight means more rebases and more held files, not faster landing.
- At most **two** new dispatches per run.
- Prefer, in order: a plan unit on the critical path (U9, then U14, then U25, then U16, U18, U20, U22), then follow ups oldest first.
- Skip a card whose issue has a comment from the operator saying to hold it.

## 4. For each chosen card

Render the handoff, post it on the issue, create the branch at the base, start the session.

```sh
N=12   # the issue number
python -m factory.handoff render --card $N --base "$BASE" --repository "$REPO" \
  --issues "$S/issues.json" --pulls "$S/pulls.json" --branches "$S/branches.json" \
  --assume-done U2 > "$S/handoff-$N.md"
BRANCH=$(grep -m1 '^2. Branch:' "$S/handoff-$N.md" | awk '{print $3}')
gh api -X POST "repos/$REPO/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$BASE"
gh api -X POST "repos/$REPO/issues/$N/comments" -F body=@"$S/handoff-$N.md"
```

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

Do not open, approve, merge, or close pull requests. The builder session opens its pull request, enables auto merge, and watches the `ci` and `review` checks; auto merge lands it when both are green. Nobody approves by hand. If a builder session has failed (its pull request is red for a day with no push, or the session ended without a pull request), say so in one line on the issue and leave the branch; the operator decides whether to re dispatch, which is: delete the branch, then run this procedure again.

## 6. Report

End with a short note: which cards were dispatched with their session ids, which are in flight, which are blocked and on what, and anything that needs the operator. If nothing was ready and nothing changed, say that in one line.
