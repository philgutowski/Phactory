"""The fixed handoff document every builder receives (R10, R16, KTD26).

A handoff is the one message that starts a build. Every builder gets the same
shape, in the same order, whatever adapter carries it:

    1. Base          the commit on the base branch the branch starts from
    2. Branch        the branch name, which Slipway created at the base (KTD27)
    3. Rules         the operator's rules text for the project, plus the
                     project's machine bound tests (tests that need hardware a
                     remote builder lacks, R16)
    4. Tools         the fixed tool list (the Tools primitive)
    5. In flight     files other in flight builds declare or have pushed
    6. Done          the done line, from the card's good and test
    7. Credential    the name of the credential git pushes with, never its value
    8. Side reference the card's action log reference, refs/slipway/log/<key>

and then the card itself, its title and body as the tracker returned them.
The card's good and test come from its slipway block through the card
contract (cards.contract.parse), and the branch is checked against the
tracker's closing reference.

Every value comes from the project file, the card, the caller, or the
operator's rules text; nothing about any project is written here (R1). The
credential is not an argument, so the rendered text cannot carry its value.

In flight files (KTD4, KTD5) are the union over other builds from dispatched
to merged: a build not yet returned contributes the paths its card declares,
and a returned build contributes the paths its pushed diff actually touches
instead, since by then the diff is the truth. A card that declared no paths
contributes nothing until it pushes.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from slipway.cards.contract import parse
from slipway.trace.shape import ACTIONS_FILE, side_reference
from slipway.trackers.base import ClosingReference

# The Tools primitive. Generic capability names; each adapter grants its own
# tools that provide them and grants nothing else.
TOOLS = ("shell", "file read", "file edit", "git")

# KTD4's in flight files fact counts cards from dispatched to merged. The same
# list as records.queries.IN_FLIGHT_STATES, repeated because builders may not
# import records; the tests hold the two equal.
IN_FLIGHT_STATES = ("dispatched", "building", "returned", "pr_open", "awaiting_approval",
                    "queued", "gate_running", "gate_red", "merged")
# From returned on, the branch is on the remote, so its diff replaces the
# declared paths.
PUSHED_STATES = IN_FLIGHT_STATES[IN_FLIGHT_STATES.index("returned"):]

_INDENT = "   "


@dataclass(frozen=True)
class InFlightBuild:
    """Another build of the same project, as the records give it.

    declared is the card's paths (empty when it declared none). pushed is the
    list of paths its pushed diff touches, or None when nobody has read it.
    """

    card_key: str
    state: str
    declared: tuple[str, ...] = ()
    pushed: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Handoff:
    """A rendered handoff: the text the builder reads and the fields the adapter needs."""

    card_key: str
    base: str
    branch: str
    rules: str
    tools: tuple[str, ...]
    in_flight: tuple[str, ...]
    done: str
    credential_name: str
    side_reference: str
    text: str


def in_flight_files(builds: Iterable[InFlightBuild], exclude_key: str | None = None
                    ) -> tuple[str, ...]:
    """The sorted union of paths other in flight builds declare or have pushed."""
    files: set[str] = set()
    for build in builds:
        if build.card_key == exclude_key or build.state not in IN_FLIGHT_STATES:
            continue
        if build.state in PUSHED_STATES and build.pushed is not None:
            files.update(build.pushed)
        else:
            files.update(build.declared)
    return tuple(sorted(files))


def _block(text: str) -> str:
    """Indent every line of text under a numbered item; blank lines stay blank."""
    return "\n".join(_INDENT + line if line.strip() else "" for line in text.splitlines())


def _bullets(items: Iterable[str]) -> str:
    return "\n".join(f"{_INDENT}* {item}" for item in items)


def _rules(rules: str, machine_bound: tuple[str, ...]) -> str:
    text = rules.strip() or "The project gives no rules beyond this handoff."
    if machine_bound:
        text += ("\n\nThese tests are machine bound: they need hardware this session does "
                 "not have.\nDo not run them here; the project's gate runs them.\n")
        text += "\n".join(f"* {name}" for name in machine_bound)
    return text


def _done(good: str | None, test: str | None, gate_command: str, branch: str) -> str:
    parts = [good.strip() if good else
             "The card names no definition of done; meet its text as written."]
    parts.append(f"The card's test, {test}, passes." if test else "The card names no test.")
    parts.append(f"The project's gate command, {gate_command}, would pass.")
    parts.append(f"Every commit is pushed to {branch}. Do not open a pull request; "
                 "Slipway opens it.")
    return " ".join(parts)


def render(project, card, base: str, branch: str, in_flight: Iterable[InFlightBuild] = (),
           rules: str = "", closing: ClosingReference | None = None) -> Handoff:
    """Render the handoff for one card.

    project is the loaded project file (config.project.Project), card the
    tracker's Card, base the commit the branch starts from, branch the name
    Slipway created, in_flight the project's other builds, and rules the
    operator's rules text for this project. closing is the tracker's
    closing reference for the card: when the tracker keys on a branch token
    (Jira does), the branch must carry it, or the card would never move to
    in progress when the branch is created (KTD27).
    """
    if not base or not branch:
        raise ValueError("a handoff needs a base commit and a branch")
    if closing and closing.branch_token and closing.branch_token not in branch:
        raise ValueError(f"branch {branch} does not carry the tracker's token "
                         f"{closing.branch_token}")
    block = parse(card.body)
    reference = side_reference(card.key)
    files = in_flight_files(in_flight, exclude_key=card.key)
    rules_text = _rules(rules, tuple(project.machine_bound_tests))
    done = _done(block.good, block.test, project.gate_command, branch)

    lines = [
        f"Slipway handoff for card {card.key}: {card.title}",
        "",
        f"1. Base: {base}",
        f"{_INDENT}Start from this commit. Slipway created the branch below at it.",
        "",
        f"2. Branch: {branch}",
        f"{_INDENT}Push your commits to this branch and to no other branch.",
        "",
        "3. Rules:",
        _block(rules_text),
        "",
        f"4. Tools: {', '.join(TOOLS)}.",
        "",
        "5. Files other in flight builds declare or have pushed, which this card must "
        "not touch:",
        _bullets(files) if files else f"{_INDENT}None.",
        "",
        f"6. Done: {done}",
        "",
        f"7. Credential: git pushes with the credential named {project.host_credential}, "
        "already configured.",
        f"{_INDENT}Never print it, and never write it to a file or a commit.",
        "",
        f"8. Side reference: {reference}",
        f"{_INDENT}Keep an action log in {ACTIONS_FILE}: one JSON object per line, with its "
        "type under \"type\",",
        f"{_INDENT}for each tool call, file change, command, and outside connection.",
        f"{_INDENT}Before you finish, push it as the only file of a commit to {reference}.",
        f"{_INDENT}Never commit it to the branch.",
        "",
        "The card:",
        "",
        card.body.strip(),
        "",
    ]
    return Handoff(
        card_key=card.key, base=base, branch=branch, rules=rules_text, tools=TOOLS,
        in_flight=files, done=done, credential_name=project.host_credential,
        side_reference=reference, text="\n".join(lines),
    )
