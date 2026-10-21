"""The property checks onboard runs before a project may take cards (U19, R30, KTD27).

R30 names four properties a project must have before Slipway builds for it:

1. Independent tasks: each card can be built and landed on its own.
2. Durable state between tasks: the board records where each card is, and
   moves on the host events Slipway causes (KTD27): in progress once a
   branch exists, in review once a pull request opens.
3. An external gate that refuses broken changes: the host requires a check
   before merge, the landing path and merge method are known, and the
   machine identity (the credential builders push with, KTD6) is restricted
   to Slipway's branch namespace, refs/heads/slipway/** for the default
   branch template (U26 lets a project name its own prefix).
4. A written definition of verification: every card says what done means.

Slipway reads 2 and 3 from the host and the tracker through a Probe. It
cannot read 1 or 4, so those are attestations: a person says yes, and
onboard records who and when. Where a host gives no way to read part of 2
or 3, the probe answers None and the check asks the operator to attest that
part instead, recorded the same way.

Each check returns one Result. A failing one says what is missing and gives
one next step (AE6, AE13), so the operator always has a single thing to do.

A Probe is onboard's view of the adapters. Its listing methods offer live
choices (boards, labels, statuses, builders) for a draft, the answers so far
as a dict; its reading methods take the project as saved. The tests pass a
fake. LiveProbe wires what the U8 and U9 adapters can read today and
answers None for the rest, so a live onboarding finishes on attestations
until the adapters learn to read rulesets, branch permissions, and the
board's own movement (see the U19 explainer).

Nothing here runs a project's command (KTD18): the gate command is text in
the project file, and the gate runs on the project's own host.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from slipway.config.bindings import BindingInvalid
from slipway.config.operator import Host
from slipway.config.project import Project, Tracker, branch_namespace
from slipway.hosts.base import HostError, Landing, ProbeUndetermined

DURABLE_STATE = "durable_state"
EXTERNAL_GATE = "external_gate"
INDEPENDENCE = "independence"
VERIFICATION = "verification"
SINGLE_DISPATCHER = "single_dispatcher"
PROPERTIES = (DURABLE_STATE, EXTERNAL_GATE, INDEPENDENCE, VERIFICATION)



def namespace(project: Project) -> str:
    """The references builders may push to (KTD6), as a ruleset names them.

    The prefix is the literal text in front of the branch template's first
    placeholder (U26), so refs/heads/slipway/** for the default template.
    """
    return f"refs/heads/{branch_namespace(project.branch_template)}**"

# Where the GitHub Projects status workflow lives in Slipway's repository.
BOARD_TEMPLATE = "templates/slipway-board.yml"
# Where a project adopts it in its own repository.
BOARD_WORKFLOW = ".github/workflows/slipway-board.yml"


@dataclass(frozen=True)
class Finding:
    """One probe's answer. ok is None when the host or tracker gives no way to read it."""

    ok: bool | None
    detail: str = ""


@dataclass(frozen=True)
class Moves:
    """What the board did on the scratch branch and pull request (KTD27).

    in_progress is whether the card moved once the branch was created,
    in_review whether it moved once the pull request opened. None means the
    probe could not watch the board.
    """

    in_progress: bool | None
    in_review: bool | None
    detail: str = ""


class Probe(Protocol):
    """Onboard's reads of the host and the tracker. None of them writes a card."""

    def builders(self) -> tuple[str, ...]: ...

    def boards(self, draft: Mapping) -> tuple[str, ...]: ...

    def labels(self, draft: Mapping) -> tuple[str, ...]: ...

    def statuses(self, draft: Mapping) -> tuple[str, ...]: ...

    def repository(self, project: Project) -> Finding: ...

    def board(self, project: Project) -> Finding: ...

    def tracker(self, project: Project) -> Finding: ...

    def board_moves(self, project: Project) -> Moves: ...

    def required_check(self, project: Project) -> Finding: ...

    def restriction(self, project: Project) -> Finding: ...

    def landing(self, project: Project) -> Landing: ...

    def gate_output(self, project: Project, pr_number: int | None) -> str: ...


@dataclass(frozen=True)
class Result:
    """One property's verdict.

    missing says what is absent and next_step the one thing to do about it.
    question, when set, is the question whose answer is the fix (a landing
    pin, an attestation); otherwise the fix is on the host and onboard asks
    to check again. attest maps a check name to what the operator attests
    when a probe answered None. evidence is kept with the state.
    """

    property: str
    ok: bool
    missing: str | None = None
    next_step: str | None = None
    question: str | None = None
    attest: dict[str, str] = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


def _fail(prop, missing, step, question=None, attest=None, **evidence) -> Result:
    return Result(prop, False, missing, step, question, dict(attest or {}), evidence)


def _unread(prop: str, name: str, finding: Finding, what: str,
            attested: Mapping[str, dict]) -> Result | None:
    """The attestation a probe that answered None needs, or None when there is nothing to ask."""
    if finding.ok is not None or name in attested:
        return None
    return _fail(prop, f"Slipway cannot read {what}: {finding.detail}",
                 f"confirm {what} by hand", question=f"attest.{name}", attest={name: what})


def single_dispatcher(answers: Mapping) -> Result:
    if answers.get("other_dispatcher") == "yes":
        return _fail(SINGLE_DISPATCHER, "another system dispatches this board",
                     "turn the other dispatcher off, then answer other_dispatcher with no",
                     question="other_dispatcher")
    return Result(SINGLE_DISPATCHER, True)


def board_step(project: Project) -> str:
    """The next step for a board that does not move on Slipway's host events (AE13)."""
    if project.tracker is Tracker.GITHUB_PROJECTS:
        return (f"copy Slipway's {BOARD_TEMPLATE} into {project.repository} as "
                f"{BOARD_WORKFLOW}, with the board token secret it names, so the board moves "
                f"a card to {project.in_progress_status} when its branch is created and to "
                f"{project.in_review_status} when its pull request opens")
    return (f"add two Jira automation rules on {project.board}: branch created moves the "
            f"issue to {project.in_progress_status}, pull request created moves it to "
            f"{project.in_review_status}")


def durable_state(probe: Probe, project: Project, attested: Mapping[str, dict]) -> Result:
    readable = probe.tracker(project)
    if not readable.ok:
        return _fail(DURABLE_STATE, f"the tracker cannot be read: {readable.detail}",
                     f"give the tracker credential {project.tracker_credential!r} read access "
                     f"to {project.board}")
    board = probe.board(project)
    if board.ok is False:
        return _fail(DURABLE_STATE, f"the board cannot be used: {board.detail}",
                     "fix the board, or answer the board question again",
                     question="board")
    moves = probe.board_moves(project)
    if moves.in_progress is None or moves.in_review is None:
        what = "that the board moves cards on branch created and pull request opened"
        unread = Finding(None, moves.detail)
        if asked := _unread(DURABLE_STATE, "board_moves", unread, what, attested):
            return asked
    elif not (moves.in_progress and moves.in_review):
        stuck = [name for name, moved in (("in progress", moves.in_progress),
                                          ("in review", moves.in_review)) if not moved]
        return _fail(DURABLE_STATE, f"the board did not move the card to {' or '.join(stuck)} "
                     f"on Slipway's scratch branch and pull request ({moves.detail})",
                     board_step(project))
    return Result(DURABLE_STATE, True, evidence={"board": board.detail, "moves": moves.detail})


def gate_step(project: Project) -> str:
    """The next step for a repository whose host requires no check before merge (AE6)."""
    if project.host is Host.BITBUCKET:
        return (f"in {project.repository}'s branch restrictions, require a passing build "
                "before merge on the main branch, from the pipeline that runs the gate")
    return (f"add a ruleset on {project.repository}'s default branch that requires the "
            "status check the gate posts before merge")


def restriction_step(project: Project) -> str:
    if project.host is Host.BITBUCKET:
        return (f"in {project.repository}'s branch permissions, let the access token "
                f"{project.host_credential!r} write only to "
                f"{branch_namespace(project.branch_template)}** branches")
    return (f"add a ruleset on {project.repository} that blocks the identity of "
            f"{project.host_credential!r} from every reference except {namespace(project)}, with "
            "that identity absent from the bypass list")


def external_gate(probe: Probe, project: Project, attested: Mapping[str, dict]) -> Result:
    required = probe.required_check(project)
    if required.ok is False:
        return _fail(EXTERNAL_GATE, "an external gate that refuses broken changes: "
                     f"{project.repository} requires no check before merge "
                     f"({required.detail})", gate_step(project))
    what = "that the host requires a passing check before merge"
    if asked := _unread(EXTERNAL_GATE, "required_check", required, what, attested):
        return asked
    if not branch_namespace(project.branch_template):
        return _fail(EXTERNAL_GATE, "a branch namespace: the branch template starts with a "
                     "placeholder, so no push rule can hold the machine identity to it",
                     "answer branch_template with a fixed prefix, such as slipway/{key}-{build}",
                     question="branch_template")
    restricted = probe.restriction(project)
    if restricted.ok is False:
        return _fail(EXTERNAL_GATE, "the machine identity is not restricted to Slipway's "
                     f"branch namespace ({restricted.detail})", restriction_step(project))
    what = f"that the machine identity can push only to {namespace(project)}"
    if asked := _unread(EXTERNAL_GATE, "restriction", restricted, what, attested):
        return asked
    try:
        landing = probe.landing(project)
    except ProbeUndetermined as error:
        return _fail(EXTERNAL_GATE, f"the landing path cannot be read: {error}",
                     "pin the landing path and merge method",
                     question="landing_path" if project.landing_path is None else "merge_method")
    except HostError as error:
        return _fail(EXTERNAL_GATE, f"the host could not be read: {error}",
                     "check the host credential and the repository, then check again")
    return Result(EXTERNAL_GATE, True, evidence={
        "required_check": required.detail or "attested",
        "restriction": restricted.detail or "attested",
        "landing_path": landing.path, "merge_method": landing.method,
        "landing_detail": landing.detail})


def attested_property(prop: str, answers: Mapping) -> Result:
    """Independence or verification, from the person's own yes (R30)."""
    value = answers.get(prop)
    if value == "yes":
        return Result(prop, True)
    if prop == INDEPENDENCE:
        return _fail(prop, "cards that can each be built and landed on their own",
                     "split the work so no card needs another card's branch, then answer "
                     "independence with yes", question=INDEPENDENCE)
    return _fail(prop, "a written definition of done on every card",
                 "add a definition of done (the slipway block's good) to the card template, "
                 "then answer verification with yes", question=VERIFICATION)


def run(probe: Probe, project: Project, answers: Mapping,
        attested: Mapping[str, dict]) -> list[Result]:
    """Every check in order, stopping at the first that fails, so one next step comes back."""
    steps: list[Callable[[], Result]] = [
        lambda: single_dispatcher(answers),
        lambda: durable_state(probe, project, attested),
        lambda: external_gate(probe, project, attested),
        lambda: attested_property(INDEPENDENCE, answers),
        lambda: attested_property(VERIFICATION, answers),
    ]
    results = []
    for step in steps:
        result = step()
        results.append(result)
        if not result.ok:
            break
    return results


def stale(probe: Probe, project: Project, accounts) -> dict[str, str]:
    """Answers no longer true on re entry (R32): question id to why.

    The repository must still be reachable, the board must still exist, and
    each role's account must still be in the operator file. The restriction
    is re read by external_gate when the checks run again.
    """
    found = {}
    repository = probe.repository(project)
    if repository.ok is False:
        found["repository"] = f"the repository cannot be reached: {repository.detail}"
    board = probe.board(project)
    if board.ok is False:
        found["board"] = f"the board is gone: {board.detail}"
    for role, binding in project.roles.items():
        if binding.account not in accounts:
            found[f"roles.{role}.account"] = (
                f"{binding.account!r} is no longer in the operator file's accounts")
    return found


class LiveProbe:
    """The probe over the adapters U8 and U9 built.

    It reads the tracker (the adapter's ready list) and the landing path (the
    host adapter's probe). The adapters cannot yet list boards or labels,
    read a ruleset's restrictions or required checks, or watch the board
    move, so those answer None and onboard asks the operator to attest them.
    """

    def __init__(self, operator, tracker_for: Callable, host_for: Callable,
                 builders: tuple[str, ...] = ()):
        self._operator = operator
        self._tracker_for = tracker_for
        self._host_for = host_for
        self._builders = tuple(builders)

    def builders(self) -> tuple[str, ...]:
        return self._builders

    def boards(self, draft) -> tuple[str, ...]:
        return ()

    def labels(self, draft) -> tuple[str, ...]:
        return ()

    def statuses(self, draft) -> tuple[str, ...]:
        return ()

    def repository(self, project) -> Finding:
        return Finding(None, "the host adapter has no repository read")

    def board(self, project) -> Finding:
        return Finding(None, "the tracker adapter has no board read")

    def tracker(self, project) -> Finding:
        try:
            _, reason = self._tracker_for(project).ready()
        except (BindingInvalid, HostError, ValueError, OSError) as error:
            # An unset credential variable is BindingInvalid, raised while the
            # adapter is built; it is a failed read with a next step, not a crash.
            return Finding(False, str(error))
        return Finding(False, reason) if reason else Finding(True, "the ready list was read")

    def board_moves(self, project) -> Moves:
        return Moves(None, None, "the adapters cannot watch the board move yet")

    def required_check(self, project) -> Finding:
        return Finding(None, "the host adapter cannot read required checks yet")

    def restriction(self, project) -> Finding:
        return Finding(None, "the host adapter cannot read rulesets or branch permissions yet")

    def landing(self, project) -> Landing:
        return self._host_for(project).probe_landing(project)

    def gate_output(self, project, pr_number) -> str:
        return ""
