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
fake. LiveProbe reads all of it from the host and tracker adapters (U28):
the repository, the board, the required check, the machine identity's
restriction, and the board moving on a scratch branch and pull request. A
read the host cannot answer at all still answers None, and onboard asks the
operator to attest that part, as U19 built it (see the U28 explainer).

Nothing here runs a project's command (KTD18): the gate command is text in
the project file, and the gate runs on the project's own host.
"""

import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from slipway.config.bindings import BindingInvalid, ResolvedTrackerCredential
from slipway.config.operator import Host
from slipway.config.project import Project, Tracker, branch_namespace, render_branch
from slipway.hosts.base import HostError, Landing, ProbeUndetermined
from slipway.trackers.base import SCRATCH_TITLE

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
    """Onboard's reads of the host and the tracker.

    None of them writes a card, with one exception: board_moves files a
    scratch card and closes it again (KTD27), through a Scratch class kept
    apart from the read only tracker adapter (R34).
    """

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


# The board movement check (KTD27). The scratch base sits in the namespace,
# under a name that carries no card key, so creating it moves nothing; the
# scratch branch is the project's own branch template rendered for the
# scratch card, so the board's automation finds the card exactly as it will
# for a real one.
SCRATCH_BASE = "onboard-scratch-base"
SCRATCH_BUILD = "scratch"
# How long to wait for each move, and how often to read the board meanwhile.
MOVE_WAIT_SECONDS = 180
MOVE_POLL_SECONDS = 5

# What a read of an adapter can raise when a credential, a repository, or the
# host itself is not right. Each becomes a failed read with its reason.
READ_ERRORS = (BindingInvalid, HostError, ValueError, OSError, subprocess.SubprocessError)


@dataclass(frozen=True)
class Draft:
    """The fields a tracker adapter reads, taken from onboarding's answers so far.

    Listings run before the project file exists, so LiveProbe builds the
    adapter from this instead of a Project. Fields not answered yet are
    empty, and an adapter lists what it can without them.
    """

    slug: str = ""
    repository: str = ""
    board: str = ""
    lane_label: str = ""
    ready_status: str = ""
    landed_status: str = ""

    @classmethod
    def of(cls, draft: Mapping) -> "Draft":
        return cls(**{name: str(draft.get(name) or "") for name in cls.__dataclass_fields__})


class LiveProbe:
    """The probe over the host and tracker adapters (U8, U9, U28).

    tracker_for and host_for build the adapters for a saved project.
    draft_tracker_for builds a tracker for listings from the answers so far;
    by default it builds the real adapter from the draft and the operator
    file's tracker credential. scratch_for builds the scratch card class for
    a project (GitHubScratch or JiraScratch by default). sleep and clock pace
    the board movement check, so tests pass fakes and never wait.

    An adapter that has no read for a property (one built before U28, or a
    fake) answers None for it, the same as a host that cannot answer.
    """

    def __init__(self, operator, tracker_for: Callable, host_for: Callable,
                 builders: tuple[str, ...] = (), draft_tracker_for: Callable | None = None,
                 scratch_for: Callable | None = None, sleep: Callable = time.sleep,
                 clock: Callable = time.monotonic, wait_seconds: float = MOVE_WAIT_SECONDS,
                 poll_seconds: float = MOVE_POLL_SECONDS, environ: Mapping | None = None):
        self._operator = operator
        self._tracker_for = tracker_for
        self._host_for = host_for
        self._builders = tuple(builders)
        self._draft_tracker_for = draft_tracker_for or self._real_draft_tracker
        self._scratch_for = scratch_for or _real_scratch
        self._sleep = sleep
        self._clock = clock
        self._wait = wait_seconds
        self._poll = poll_seconds
        self._environ = os.environ if environ is None else environ
        # Projects whose board was seen moving in this process, so a recheck
        # of another property does not open a second scratch pull request.
        self._moved: dict[tuple, Moves] = {}

    def builders(self) -> tuple[str, ...]:
        return self._builders

    # Listings for the draft.
    def _real_draft_tracker(self, draft: Mapping):
        if draft.get("tracker") == Tracker.GITHUB_PROJECTS.value:
            from slipway.trackers.github import GitHubTracker

            return GitHubTracker(Draft.of(draft))
        from slipway.trackers.jira import JiraTracker

        return JiraTracker(Draft.of(draft), credential=self._draft_credential(draft))

    def _draft_credential(self, draft: Mapping) -> ResolvedTrackerCredential:
        name = str(draft.get("tracker_credential") or "")
        entry = self._operator.tracker_credentials.get(name)
        if entry is None:
            raise BindingInvalid("tracker_credential", f"{name!r} is not in the operator file")

        def value(env: str | None, key: str) -> str | None:
            if env is None:
                return None
            found = self._environ.get(env, "")
            if not found:
                raise BindingInvalid(f"tracker_credentials.{name}.{key}",
                                     f"environment variable {env} is unset")
            return found

        return ResolvedTrackerCredential(site=entry.site, email=value(entry.email_env, "email_env"),
                                         token=value(entry.token_env, "token_env"))

    def _listing(self, draft: Mapping, name: str) -> tuple[str, ...]:
        """One listing from the draft's tracker; empty when it cannot be read, so the
        question still takes a typed answer."""
        try:
            read = getattr(self._draft_tracker_for(draft), name, None)
            if read is None:
                return ()
            found, _reason = read()
        except READ_ERRORS:
            return ()
        return tuple(found or ())

    def boards(self, draft) -> tuple[str, ...]:
        return self._listing(draft, "boards")

    def labels(self, draft) -> tuple[str, ...]:
        return self._listing(draft, "labels")

    def statuses(self, draft) -> tuple[str, ...]:
        return self._listing(draft, "statuses")

    # Reads of the saved project.
    def _host(self, project, name: str):
        """The host adapter's read named name, or None when the adapter has none."""
        return getattr(self._host_for(project), name, None)

    def repository(self, project) -> Finding:
        try:
            read = self._host(project, "read_repository")
            if read is None:
                return Finding(None, "the host adapter has no repository read")
            found = read()
        except READ_ERRORS as error:
            return Finding(False, str(error))
        return Finding(found.ok, found.detail)

    def board(self, project) -> Finding:
        try:
            read = getattr(self._tracker_for(project), "read_board", None)
            if read is None:
                return Finding(None, "the tracker adapter has no board read")
            ok, detail = read()
        except READ_ERRORS as error:
            return Finding(False, str(error))
        return Finding(ok, detail)

    def tracker(self, project) -> Finding:
        try:
            _, reason = self._tracker_for(project).ready()
        except READ_ERRORS as error:
            # An unset credential variable is BindingInvalid, raised while the
            # adapter is built; it is a failed read with a next step, not a crash.
            return Finding(False, str(error))
        return Finding(False, reason) if reason else Finding(True, "the ready list was read")

    def required_check(self, project) -> Finding:
        try:
            read = self._host(project, "read_required_checks")
            if read is None:
                return Finding(None, "the host adapter cannot read required checks")
            names = read()
        except ProbeUndetermined as error:
            return Finding(None, str(error))
        except READ_ERRORS as error:
            return Finding(False, str(error))
        if not names:
            return Finding(False, "no check is required on the base branch")
        return Finding(True, "the base branch requires " + ", ".join(names))

    def restriction(self, project) -> Finding:
        try:
            read = self._host(project, "read_restriction")
            if read is None:
                return Finding(None, "the host adapter cannot read rulesets or branch "
                                     "permissions")
            found = read(namespace(project))
        except ProbeUndetermined as error:
            return Finding(None, str(error))
        except READ_ERRORS as error:
            return Finding(False, str(error))
        return Finding(found.ok, found.detail)

    def landing(self, project) -> Landing:
        return self._host_for(project).probe_landing(project)

    def gate_output(self, project, pr_number) -> str:
        return ""

    # The board movement check (KTD27).
    def board_moves(self, project) -> Moves:
        """Watch the board move a scratch card on a scratch branch and pull request.

        A crashed run's leftovers go first. Then the scratch card is filed, a
        scratch base is created at the base branch's parent commit and the
        scratch branch at its head, so the pull request between them has one
        commit and needs no commit written. The board is read until the card
        shows each status or the wait ends. Whatever happens, the pull
        request is closed, both branches deleted, and the card closed.
        Nothing here runs project code (KTD18).
        """
        prefix = branch_namespace(project.branch_template)
        if not prefix:
            return Moves(None, None, "the branch template has no fixed prefix to hold "
                                     "scratch branches")
        seen = self._moved.get(_fingerprint(project))
        if seen is not None:
            return seen
        try:
            host = self._host_for(project)
            tracker = self._tracker_for(project)
            scratch = self._scratch_for(project)
        except READ_ERRORS as error:
            return Moves(None, None, f"the adapters could not be built: {error}")
        needed = ("base_commits", "create_branch", "open_pull_request", "branches",
                  "delete_branch", "open_pull_requests", "close_pull_request")
        if scratch is None or not all(hasattr(host, name) for name in needed):
            return Moves(None, None, "the adapters cannot watch the board move")
        run = _ScratchRun(project, host, tracker, scratch, prefix)
        try:
            run.sweep()
            moves = run.watch(self._await)
        except READ_ERRORS as error:
            moves = Moves(None, None, f"the scratch run failed: {error}")
        finally:
            try:
                run.sweep()
            except READ_ERRORS as error:
                moves = Moves(None, None, f"the scratch run could not clean up: {error}")
        if moves.in_progress and moves.in_review:
            self._moved[_fingerprint(project)] = moves
        return moves

    def _await(self, tracker, key: str, wanted: str) -> tuple[bool, str | None]:
        """Read the card's status until it is wanted or the wait ends: (moved, last status)."""
        deadline = self._clock() + self._wait
        last = None
        while True:
            last = tracker.status(key).status
            if last and last.lower() == wanted.lower():
                return True, last
            if self._clock() >= deadline:
                return False, last
            self._sleep(self._poll)


def _fingerprint(project) -> tuple:
    """What the board movement check's answer depends on."""
    return (project.host, project.repository, project.host_credential, project.tracker,
            project.board, project.tracker_credential, project.branch_template,
            project.in_progress_status, project.in_review_status)


def _real_scratch(project):
    if project.tracker is Tracker.GITHUB_PROJECTS:
        from slipway.trackers.github import GitHubScratch

        return GitHubScratch(project)
    from slipway.trackers.jira import JiraScratch

    return JiraScratch(project)


class _ScratchRun:
    """One board movement check's scratch card, branches, and pull request."""

    def __init__(self, project, host, tracker, scratch, prefix: str):
        self.project, self.host, self.tracker, self.scratch = project, host, tracker, scratch
        self.prefix = prefix
        self.base = f"{prefix}{SCRATCH_BASE}"
        self.keys: list[str] = []

    def branch_for(self, key: str) -> str:
        return render_branch(self.project.branch_template, key, SCRATCH_TITLE, SCRATCH_BUILD)

    def _delete(self, name: str) -> None:
        # Never a branch outside the namespace, whatever a pull request named.
        if name and name.startswith(self.prefix):
            self.host.delete_branch(name)

    def sweep(self) -> None:
        """Remove every scratch pull request, branch, and card, this run's or a crashed one's."""
        found, reason = self.scratch.leftovers(SCRATCH_TITLE)
        if reason:
            raise HostError(f"the scratch cards could not be listed: {reason}")
        keys = list(dict.fromkeys([*self.keys, *found]))
        for pr in self.host.open_pull_requests(self.base):
            self.host.close_pull_request(pr)
            self._delete(pr.head_ref or "")
        standing = set(self.host.branches(self.prefix))
        for key in keys:
            if self.branch_for(key) in standing:
                self._delete(self.branch_for(key))
        if self.base in standing:
            self._delete(self.base)
        for key in keys:
            if reason := self.scratch.close(key):
                raise HostError(f"scratch card {key} could not be closed: {reason}")
        self.keys = []

    def watch(self, wait: Callable) -> Moves:
        key, reason = self.scratch.open(SCRATCH_TITLE)
        if not key:
            return Moves(None, None, f"the scratch card could not be filed: {reason}")
        self.keys.append(key)
        main, head, parent = self.host.base_commits()
        if not head or not parent:
            return Moves(None, None, f"{main} has no parent commit to open a scratch pull "
                                     "request against")
        branch = self.branch_for(key)
        self.host.create_branch(self.base, parent)
        self.host.create_branch(branch, head)
        progress, seen = wait(self.tracker, key, self.project.in_progress_status)
        reference = self.tracker.closing_reference(key)
        pr = self.host.open_pull_request(branch, SCRATCH_TITLE, reference, base=self.base)
        review, last = wait(self.tracker, key, self.project.in_review_status)
        detail = (f"scratch card {key} on {branch}: {seen or 'no status'} after the branch, "
                  f"{last or 'no status'} after pull request #{pr.number}")
        return Moves(progress, review, detail)
