"""The evals package's steps on the card workflow's seams (U16).

workflow/post_landed.py imports this module by name and registers the pairs
in SLIPWAY_STEPS, in order, so nothing here imports workflow (KTD23). U17
adds score_judged to the same tuple.

score_deterministic gathers what deterministic.score reads, and only reads:

    the card      the tracker's read of the card, parsed by the shared
                  block parser (cards.contract.parse).
    the diff      the files the confirmed commit changes since the commit
                  it branched from on the landing branch, read from
                  Slipway's own bare cache through gitread. A bare cache has
                  no checkout, so no project hook or filter runs (KTD18).
    the tree      the files of the landed commit, from the same cache.
    the gate      the host adapter's gate_run(commit), when the adapter has
                  one: the gate's conclusion and log for the commit the gate
                  tested. No host adapter reads a gate log yet, so today the
                  gate and runs items record "not read" and the score's
                  verdict is partial, never fail, until one does.

It writes the score through records.record_score when the records offer it
(the scores row is unique on build and kind, so a second run replaces the
first), and returns the total and each item's status, which the workflow
writes into the evidence of the landed to scored transition.
"""

from collections.abc import Mapping

from slipway import gitread
from slipway.cards.contract import parse
from slipway.evals.deterministic import Gate, score


def _evidence(ports, build_id: int, state: str) -> dict:
    entered = ports.records.entered(build_id, state)
    return dict(entered[1]) if entered else {}


def _git(ports, project, build: Mapping, base: str | None, merged: str | None
         ) -> tuple[tuple[str, ...] | None, frozenset[str] | None]:
    """The confirmed commit's changed files and the landed commit's files.

    Either is None when git could not read it. The diff starts where the
    confirmed commit meets the landing branch just before the merge (the
    merged commit's first parent), so a branch that merged main in partway
    is scored on its own changes only.
    """
    confirmed = build.get("confirmed_commit")
    if not confirmed:
        return None, None
    try:
        repo = ports.git.cache(project, tuple(c for c in (confirmed, merged) if c))
    except gitread.GitError:
        return None, None
    diff = tree = None
    try:
        before = (gitread.first_parent(repo, merged) if merged else None) or base
        if before:
            start = gitread.run(repo, "merge-base", before, confirmed).strip()
            out = gitread.run(repo, "diff", "--name-only", "--no-renames", "-z", start,
                              confirmed)
            diff = tuple(name for name in out.split("\0") if name)
    except gitread.GitError:
        diff = None
    try:
        out = gitread.run(repo, "ls-tree", "-r", "--name-only", "-z", merged or confirmed)
        tree = frozenset(name for name in out.split("\0") if name)
    except gitread.GitError:
        tree = None
    return diff, tree


def _gate(ports, project, build: Mapping) -> Gate | None:
    """The gate's result for the commit it tested, from the host when it can say."""
    reader = getattr(ports.host(project), "gate_run", None)
    commit = build.get("tested_commit") or build.get("confirmed_commit")
    if reader is None or not commit:
        return None
    found = reader(commit)
    if found is None or isinstance(found, Gate):
        return found
    return Gate(**found)


def score_deterministic(context) -> dict:
    """The post_landed step: score the landed build and write its scores row."""
    ports, project, build = context.ports, context.project, context.build
    card = ports.tracker(project).read(build["card_key"])
    block = None if getattr(card, "skipped", None) else parse(card.body)
    landed = _evidence(ports, build["id"], "landed")
    merged = landed.get("merged") or (landed.get("override") or {}).get("merged")
    base = _evidence(ports, build["id"], "dispatched").get("base")
    diff, tree = _git(ports, project, build, base, merged)
    result = score(build, diff, _gate(ports, project, build), block, tree)
    write = getattr(ports.records, "record_score", None)
    if write is not None:
        row = result.record()
        write(row["build_id"], row["kind"], row["result"])
    return {"total": result.total(),
            "items": {item.name: item.status for item in result.items}}


SLIPWAY_STEPS = (("post_landed", score_deterministic),)
