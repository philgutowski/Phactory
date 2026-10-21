"""The durable card workflow: one run per build, from claimed to scored (U14).

The card workflow is the process that takes one claimed build through the
state machine in the plan's High-Level Technical Design: dispatch, build,
open the pull request, wait for the host to land it, verify, and score. It
runs on Hatchet (KTD2), whose durable log lets a run sleep for days and
survive a crash or a redeploy. worker.py turns the functions here into
Hatchet tasks; nothing in this module imports the engine's SDK, so the unit
tests drive it with fakes for the engine's two primitives, start a child and
wait.

Terms used here:

- Parent: the durable task card_workflow. Durable means its progress is in
  the engine's log; after a crash the engine replays the log and the parent
  carries on. A replay reruns the parent's own code but reads each child's
  result from the log (docs/spikes/hatchet.md), so the parent does nothing
  but start children, wait, and branch on what they return. Every record,
  host call, and tracker read is in a child.
- Child: a task the parent starts and waits for. Each child below is safe to
  run twice, because a worker lost mid child gets its child handed to another
  worker even with no retries (the spike's step 5).
- Concurrency key: a value read from a child's input that the engine counts
  running children under (KTD4). build holds the project's key up to the
  project's cap; open holds the repository's key at one; the parent holds
  none, so a card waiting days for approval holds no slot.
- Claim before act: a side effect step writes its claim column (pr_number,
  session_ref, and the rest) in the same transaction as the transition that
  follows, and skips the call when the column is already set (KTD16). Where
  the host call is itself idempotent (create_branch at one commit, opening a
  second pull request on one branch returns the first), a crash between the
  call and the record repeats the call harmlessly. Where it is not (starting
  a builder session), an attempt row is written before the call, and a
  re executed step that finds an attempt with no session claim ends the
  build as returned_empty rather than start a second session.

The children, in the order a card meets them:

    attach_run    write this run's id onto the build (KTD3); a build already
                  held by another run id ends this run as a no op. Records
                  claimed to waiting_for_slot when the project is at its cap.
    build         (key: project, max runs: input.cap) re read the card,
                  resolve the builder binding from the files as they are now,
                  create the branch on the host at the base (KTD27), render the
                  handoff, start the session, and poll it with the role's budget
                  as a deadline that starts at building (KTD22). A
                  worker_process binding runs laptop_build on the worker's
                  label instead and records its result.
    laptop_build  (on the laptop worker's label) run one session and return
                  its result; the laptop holds no records connection (KTD12).
    post_build    the post_build seam, from every exit of building when a
                  session was started.
    open          (key: repository, max runs: 1) confirm the commit, open the
                  pull request, post the deterministic check.
    on_wake       one wake of the landing wait, handled after a re read of
                  the pull request (KTD10).
    verify        merged to verifying to landed, or to halted (KTD11), with
                  the ten minute tracker re read before merged_card_not_moved.
    post_landed   the post_landed seam, then landed to scored.

The deterministic check (R18) Slipway posts before the merge says one
thing: the commit it sits on is the one Slipway confirmed, or the queue
commit built from it. It passes on the confirmed head at open and on a
tested commit whose pull request head is the confirmed commit (KTD7), and
fails on a new head pushed after open (AE12) or a tested commit built from
another head. The landing check, slipway/landing, fails with the reason
whenever the build enters a needs you state that keeps the run alive:
gate_red, halted, and awaiting_approval after the seven day deadline
(KTD27, R42).
"""

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from slipway.builders.base import BuilderResult, Schedule, check_base
from slipway.builders.handoff import Handoff, render
from slipway.cards.contract import parse
from slipway.cards.contract import ready as ready_rule
from slipway.config.bindings import BindingInvalid, ResolvedBinding
from slipway.config.operator import Account
from slipway.config.project import LandingPath
from slipway.halts import (
    BINDING_INVALID,
    BUILDER_EXPIRED,
    BUILDER_FAILED,
    COMMIT_MISMATCH,
    TRACKER_UNREADABLE,
    halt,
)
from slipway.hosts.base import (
    CLOSED,
    MERGED,
    OPEN,
    BranchConflict,
    CheckState,
    HostError,
    PullRequest,
)
from slipway.redact import scrub
from slipway.verify import PARTIAL_LANDING
from slipway.workflow import events
from slipway.workflow.events import Wake
from slipway.workflow.post_landed import (
    GATE_OBSERVED,
    POST_BUILD,
    POST_LANDED,
    Registry,
    SeamContext,
)

ACTOR = "workflow"
CHECK_DETERMINISTIC = "slipway/deterministic"
CHECK_LANDING = "slipway/landing"

# The ownership terminal set (KTD3, records.store.OWNERSHIP_TERMINAL): a run
# whose build reaches one of these ends.
TERMINAL = frozenset({"scored", "abandoned", "returned_empty"})
BEFORE_DISPATCH = ("claimed", "waiting_for_slot")
# The states the landing wait runs in, and the ones its events move between.
WAITING = ("pr_open", "awaiting_approval", "queued", "gate_running", "gate_red", "halted")
CLOSABLE = ("pr_open", "awaiting_approval", "queued", "gate_running", "gate_red")
# The states that hold one of the project's build slots (KTD4: from
# dispatched to returned).
SLOT_STATES = ("dispatched", "building")

# The seven day deadline on the landing wait, after which a pull request
# still waiting moves to needs you without ending the run (plan U14 step 4).
DEADLINE = timedelta(days=7)
# How often the build child polls a remote session.
BUILD_POLL_SECONDS = 30
# The tracker re read before merged_card_not_moved (KTD11, halts.py).
NOT_MOVED_RECHECK_SECONDS = 30
NOT_MOVED_RECHECK_WINDOW = timedelta(minutes=10)

# The child names. worker.py registers one Hatchet task under each.
ATTACH_RUN = "attach_run"
BUILD = "build"
LAPTOP_BUILD = "laptop_build"
POST_BUILD_CHILD = "post_build"
OPEN_PR = "open"
ON_WAKE = "on_wake"
VERIFY = "verify"
POST_LANDED_CHILD = "post_landed"


@dataclass(frozen=True)
class Child:
    """How the engine runs one child: its concurrency key and its timeouts.

    key names the input field the concurrency key is read from, max_runs an
    input field or a number. Hatchet declares timeouts per task, not per run,
    so execution is the ceiling for every project; the role's own budget is
    the deadline the build child enforces itself (KTD22). schedule is how
    long the child may wait to be picked up, which for laptop_build is how
    long a closed laptop may hold a build.
    """

    name: str
    execution: timedelta
    schedule: timedelta = timedelta(hours=2)
    key: str | None = None
    max_runs: str | int | None = None
    # Every child is safe to run twice, so a transient host error is retried.
    # laptop_build is not: a second session on the laptop is a second spend.
    retries: int = 3


# The longest role budget a project file can usefully name, plus a margin.
BUILD_CEILING = timedelta(hours=12)
CHILDREN = {c.name: c for c in (
    Child(ATTACH_RUN, timedelta(minutes=2)),
    # build waits for its slot as long as a laptop build may take, so a
    # queued card is not failed for waiting.
    Child(BUILD, BUILD_CEILING + timedelta(minutes=15), schedule=timedelta(days=3),
          key="project", max_runs="cap"),
    Child(LAPTOP_BUILD, BUILD_CEILING + timedelta(minutes=15), schedule=timedelta(days=2),
          retries=0),
    Child(POST_BUILD_CHILD, timedelta(minutes=15)),
    Child(OPEN_PR, timedelta(minutes=10), schedule=timedelta(hours=6),
          key="repository", max_runs=1),
    Child(ON_WAKE, timedelta(minutes=5)),
    Child(VERIFY, NOT_MOVED_RECHECK_WINDOW + timedelta(minutes=10)),
    Child(POST_LANDED_CHILD, timedelta(minutes=30)),
)}
# The parent's own timeouts. Its execution timeout bounds every wait it
# makes, since the engine counts wall time across durable waits (the spike's
# first finding), so it is well past the seven day deadline.
PARENT_EXECUTION = timedelta(days=30)
PARENT_SCHEDULE = timedelta(hours=2)


@dataclass(frozen=True)
class Build:
    """One builds row, with the project slug beside it."""

    id: int
    project: str
    project_id: int
    card_key: str
    state: str
    last_seq: int
    run_id: str | None = None
    branch: str | None = None
    session_ref: str | None = None
    pr_number: int | None = None
    check_run_id: str | None = None
    confirmed_commit: str | None = None
    tested_commit: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


class Records(Protocol):
    """The records the workflow reads and writes (records.store, records.queries)."""

    def build(self, build_id: int) -> Build: ...
    def attach_run(self, build_id: int, run_id: str): ...
    def transition(self, build_id: int, from_state: str, to_state: str, actor: str,
                   evidence: dict, claims: dict, expected_seq: int | None): ...
    def entered(self, build_id: int, state: str) -> tuple[datetime, dict] | None: ...
    def slots_taken(self, project_id: int) -> int: ...
    def in_flight(self, project_id: int) -> list: ...
    def attempts(self, build_id: int) -> int: ...
    def start_attempt(self, build_id: int, binding: ResolvedBinding) -> int: ...
    def end_attempt(self, build_id: int, result: BuilderResult) -> None: ...
    def ingest_poll(self, project_id: int, normalised: dict) -> bool: ...


class Git(Protocol):
    """Reads of the project's repository through Slipway's own cache (gitread)."""

    def base(self, project) -> str: ...
    def is_ancestor(self, project, ancestor: str, descendant: str) -> bool: ...
    def hold(self, project, branch: str, commit: str) -> None: ...
    def cache(self, project, commits: tuple[str, ...]) -> Any: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass
class Ports:
    """Everything a child reaches, each built from the files as they are now.

    project(slug) loads the project file, binding(slug, role) resolves a role
    (raising BindingInvalid), and tracker, host, builder, and credential build
    the adapters for that project. laptop(input, label) runs the
    laptop_build child on the worker carrying label and returns its output.
    """

    records: Records
    project: Callable[[str], Any]
    operator: Callable[[], Any]
    binding: Callable[[str, str], ResolvedBinding]
    tracker: Callable[[Any], Any]
    host: Callable[[Any], Any]
    builder: Callable[[Any, ResolvedBinding], Any]
    credential: Callable[[Any], Any]
    worker_label: Callable[[Any], str]
    laptop: Callable[[dict, str], dict]
    git: Git
    verify: Callable[..., Any]
    clock: Clock
    seams: Registry = field(default_factory=Registry)


class Engine(Protocol):
    """The two engine primitives the parent uses. worker.py adapts Hatchet's."""

    async def child(self, name: str, input: dict) -> dict: ...

    async def wait(self, signal: str, build_id: int) -> Wake: ...


def branch_name(ports: Ports, project, card_key: str, build_id: int) -> str:
    """The card's branch under the project's branch_template (U26).

    The default template carries the build id, so a reopened card gets a new
    name (KTD3). The card's title is read only when the template uses {slug}.
    """
    title = ""
    if "{slug}" in project.branch_template:
        title = ports.tracker(project).read(card_key).title or ""
    return project.branch_for(card_key, title, build_id)


# The parent.

async def card_workflow(engine: Engine, project: str, key: str, build_id: int,
                        run_id: str) -> dict:
    """The parent's whole body: start children, wait, branch. No side effects here."""
    card = {"project": project, "key": str(key), "build_id": int(build_id)}
    attached = await engine.child(ATTACH_RUN, {**card, "run_id": run_id})
    if not attached["attached"]:
        return {"outcome": "not_attached", "held_by": attached.get("run_id")}
    card.update(cap=attached["cap"], repository=attached["repository"])
    state = attached["state"]

    if state in (*BEFORE_DISPATCH, "dispatched", "building"):
        built = await engine.child(BUILD, card)
        state = built["state"]
        if built.get("session_ref") or built.get("laptop"):
            await engine.child(POST_BUILD_CHILD, {**card, "built": built})
    if state == "returned":
        state = (await engine.child(OPEN_PR, card))["state"]

    waits = 0
    while state not in TERMINAL:
        if state in ("merged", "verifying"):
            state = (await engine.child(VERIFY, card))["state"]
        elif state == "landed":
            state = (await engine.child(POST_LANDED_CHILD, card))["state"]
        elif state in WAITING:
            wake = await engine.wait(f"wake-{waits}", build_id)
            waits += 1
            state = (await engine.child(ON_WAKE, {**card, "wake": wake.to_json()}))["state"]
        else:
            raise RuntimeError(f"build {build_id} is in {state}, which no child moves")
    return {"outcome": state, "waits": waits}


# Shared by the children.

def _move(ports: Ports, build: Build, to_state: str, evidence: Mapping | None = None,
          claims: Mapping | None = None) -> Build:
    """Record one transition from where the build is now; return the build as it then is.

    expected_seq pins the move to the lap the caller read, so a replayed step
    finds the build already past and writes nothing (records.store).
    """
    ports.records.transition(build.id, build.state, to_state, ACTOR, dict(evidence or {}),
                             dict(claims or {}), build.last_seq)
    return ports.records.build(build.id)


def _walk(ports: Ports, build: Build, path: tuple[str, ...], evidence: Mapping,
          claims: Mapping | None = None) -> Build:
    """Move along path from the build's state, writing claims on the last edge."""
    if build.state in path:
        path = path[path.index(build.state) + 1:]
    for index, state in enumerate(path):
        last = index == len(path) - 1
        build = _move(ports, build, state, evidence, claims if last else None)
    return build


def _summary(build: Build, **extra) -> dict:
    return {"state": build.state, "session_ref": build.session_ref,
            "pr_number": build.pr_number, **extra}


def _entered(ports: Ports, build: Build, state: str) -> tuple[datetime | None, dict]:
    found = ports.records.entered(build.id, state)
    return found if found else (None, {})


def not_wanted(ports: Ports, project, card_key: str, before_dispatch: bool) -> str | None:
    """Why the card is no longer the project's to build, or None (plan U14 step 6).

    Before dispatch the card must pass the whole ready rule. After it, the
    tracker's own automation has moved the card out of the ready status
    (KTD27), so the card must still carry the project's lane label and a
    block that parses; removing the label is how a person takes a card back.
    """
    card = ports.tracker(project).read(card_key)
    if card.skipped:
        return None  # an unreadable tracker is no evidence the card was taken back
    if before_dispatch:
        readiness = ready_rule(card, project)
        if not readiness.ready:
            return readiness.reason or f"card {card_key} is no longer in {project.ready_status}"
        return None
    labels = {label.lower() for label in card.labels}
    if project.lane_label.lower() not in labels:
        return f"card {card_key} no longer carries the lane label {project.lane_label}"
    block = parse(card.body)
    if not block.ok:
        return f"card {card_key}: {block.error}"
    return None


def _seam(ports: Ports, seam: str, project, build: Build, event: Mapping | None = None
          ) -> dict:
    return ports.seams.run(seam, SeamContext(seam, project, build.to_json(), ports, event))


def _landing_failure(ports: Ports, project, build: Build, why: str) -> None:
    """Post the failing landing check that shows a needs you state on the pull request."""
    sha = build.confirmed_commit
    if sha:
        ports.host(project).post_check(sha, CHECK_LANDING, CheckState.FAILURE.value, None,
                                       why)


# attach_run.

def attach_run(ports: Ports, input: Mapping) -> dict:
    build = ports.records.build(input["build_id"])
    attached = ports.records.attach_run(build.id, input["run_id"])
    if not attached.attached and attached.run_id != input["run_id"]:
        return {"attached": False, "run_id": attached.run_id, "state": build.state}
    project = ports.project(input["project"])
    if build.state == "claimed":
        taken = ports.records.slots_taken(build.project_id)
        if taken >= project.build_cap:
            build = _move(ports, build, "waiting_for_slot",
                          {"cap": project.build_cap, "taken": taken})
    return {"attached": True, "run_id": input["run_id"], "state": build.state,
            "cap": project.build_cap, "repository": project.repository}


# build.

def _binding_json(binding: ResolvedBinding) -> dict:
    return {"role": binding.role, "account": asdict(binding.account), "model": binding.model,
            "effort": binding.effort, "time_budget_minutes": binding.time_budget_minutes}


def binding_from_json(data: Mapping) -> ResolvedBinding:
    return ResolvedBinding(data["role"], Account(**data["account"]), data["model"],
                           data["effort"], int(data["time_budget_minutes"]))


def _end_empty(ports: Ports, build: Build, halt_class: str, **evidence) -> Build:
    found = halt(halt_class, **evidence)
    return _move(ports, build, "returned_empty",
                 {"halt": found.name, "line": found.line(), **evidence})


def run_build(ports: Ports, input: Mapping) -> dict:
    """The build child: from claimed to returned, returned_empty, or abandoned."""
    build = ports.records.build(input["build_id"])
    project = ports.project(build.project)
    if build.state in BEFORE_DISPATCH:
        build = _dispatch(ports, project, build)
    laptop = None
    if build.state == "dispatched":
        build, laptop = _start(ports, project, build)
    if build.state == "building" and laptop is None:
        build = _await_session(ports, project, build)
    return _summary(build, laptop=laptop)


def _dispatch(ports: Ports, project, build: Build) -> Build:
    reason = not_wanted(ports, project, build.card_key, before_dispatch=True)
    if reason:
        return _move(ports, build, "abandoned", {"reason": reason})
    try:
        binding = ports.binding(project.slug, "builder")
        if Schedule(ports.builder(project, binding).schedule) is Schedule.WORKER_PROCESS:
            ports.worker_label(project)
    except BindingInvalid as error:
        return _end_empty(ports, build, BINDING_INVALID, role="builder", reason=error.message,
                          field=error.field)
    branch = build.branch or branch_name(ports, project, build.card_key, build.id)
    base = ports.git.base(project)
    try:
        ports.host(project).create_branch(branch, base)
    except BranchConflict as conflict:
        if "{build}" not in project.branch_template:
            # Without the build id the name is the card's alone, so the branch
            # may be an earlier build's, and building on it would start from
            # that build's work. Nothing here can tell, so the build ends and
            # names the branch a person deletes before the card is claimed again.
            return _end_empty(ports, build, BUILDER_FAILED,
                              reason=f"branch {branch} already exists at {conflict.found}; "
                                     "delete it to build this card again")
        # A re executed step whose first try created the branch before the
        # base moved: the branch name carries the build id, so nobody else
        # made it, and the commit it sits at is this build's base.
        base = conflict.found
    return _move(ports, build, "dispatched",
                 {"base": base, "branch": branch, "binding": _binding_json(binding)},
                 {"branch": branch})


def _start(ports: Ports, project, build: Build) -> tuple[Build, dict | None]:
    _, dispatched = _entered(ports, build, "dispatched")
    base = dispatched["base"]
    reason = not_wanted(ports, project, build.card_key, before_dispatch=False)
    if reason:
        return _move(ports, build, "abandoned", {"reason": reason}), None
    try:
        binding = ports.binding(project.slug, "builder")
        builder = ports.builder(project, binding)
        label = (ports.worker_label(project)
                 if Schedule(builder.schedule) is Schedule.WORKER_PROCESS else None)
    except BindingInvalid as error:
        return _end_empty(ports, build, BINDING_INVALID, role="builder", reason=error.message,
                          field=error.field), None
    tracker = ports.tracker(project)
    card = tracker.read(build.card_key)
    handoff = render(project, card, base, build.branch,
                     in_flight=ports.records.in_flight(build.project_id),
                     closing=tracker.closing_reference(build.card_key))
    budget = binding.time_budget_minutes * 60
    if label is not None:
        # A re executed build child reaches the same laptop run, not a new
        # one: the trigger carries a child key made from the build id, which
        # the engine dedupes on (worker.laptop_trigger).
        if not ports.records.attempts(build.id):
            ports.records.start_attempt(build.id, binding)
        returned = ports.laptop({"build_id": build.id, "handoff": asdict(handoff),
                                 "binding": _binding_json(binding), "budget_seconds": budget},
                                label)
        build = _move(ports, build, "building",
                      {"worker": label, "started_at": returned["started_at"],
                       "budget_seconds": budget})
        result = BuilderResult(**returned["result"])
        build = _finish(ports, project, build, result, base, budget)
        return build, {"transcript": returned.get("transcript", ""),
                       "skipped": returned.get("skipped", 0)}
    if ports.records.attempts(build.id):
        # An earlier try wrote its attempt and may have started a session
        # before it could record the claim; starting again could make two.
        return _end_empty(ports, build, BUILDER_FAILED,
                          reason="a session may have started before a crash and its "
                                 "reference was not recorded; dispatch again"), None
    ports.records.start_attempt(build.id, binding)
    try:
        session_ref = builder.start(handoff, binding, ports.credential(project))
    except Exception as error:  # a provider error at start ends the build (KTD22)
        return _end_empty(ports, build, BUILDER_FAILED,
                          reason=scrub(f"the session did not start: {error}")), None
    return _move(ports, build, "building",
                 {"started_at": ports.clock.now().isoformat(), "budget_seconds": budget},
                 {"session_ref": session_ref}), None


def _await_session(ports: Ports, project, build: Build) -> Build:
    """Poll the session until it is terminal or the budget, counted from building, runs out."""
    started, evidence = _entered(ports, build, "building")
    budget = int(evidence.get("budget_seconds") or 0)
    _, dispatched = _entered(ports, build, "dispatched")
    binding = binding_from_json(dispatched["binding"])
    builder = ports.builder(project, binding)
    deadline = (started or ports.clock.now()) + timedelta(seconds=budget)
    while True:
        status = builder.poll(build.session_ref)
        if status.terminal:
            result = status.result
            break
        if ports.clock.now() >= deadline:
            result = BuilderResult(build.branch, None, f"ran past its budget of {budget} seconds",
                                   build.session_ref, halt_class=BUILDER_EXPIRED)
            break
        ports.clock.sleep(BUILD_POLL_SECONDS)
    return _finish(ports, project, build, result, dispatched["base"], budget)


def _finish(ports: Ports, project, build: Build, result: BuilderResult, base: str,
            budget: int | None = None) -> Build:
    """Leave building for returned, returned_empty, or abandoned."""
    ports.records.end_attempt(build.id, result)
    result = check_base(result, base,
                        lambda a, b: ports.git.is_ancestor(project, a, b))
    if result.commit and not ports.host(project).commit_on_remote(build.branch, result.commit):
        result = replace(result, commit=None, halt_class=BUILDER_FAILED,
                         reason=f"commit {result.commit} is not the head of {build.branch} "
                                "on the host")
    reason = not_wanted(ports, project, build.card_key, before_dispatch=False)
    if reason:
        return _move(ports, build, "abandoned", {"reason": reason, "commit": result.commit})
    if result.commit:
        return _move(ports, build, "returned",
                     {"commit": result.commit, "reason": result.reason,
                      "provider_ref": result.provider_ref, "cost": result.cost})
    if result.halt_class == BUILDER_EXPIRED:
        return _end_empty(ports, build, BUILDER_EXPIRED, budget_seconds=budget)
    return _end_empty(ports, build, result.halt_class or BUILDER_FAILED,
                      reason=result.reason or "the builder pushed no branch")


# laptop_build, run on the laptop worker. It holds no records connection.

@dataclass
class LaptopPorts:
    """What the laptop worker reaches: its own builder, its own credential, a clock."""

    run: Callable[[Any, ResolvedBinding], Any]  # (handoff, binding) -> LaptopReturn
    clock: Clock


def laptop_build(ports: LaptopPorts, input: Mapping) -> dict:
    """Run one session on the laptop and return what the build child records.

    The deadline starts here, when the laptop picks the task up, not when the
    card was claimed, so a build queued overnight is not expired before it
    starts (KTD22).
    """
    started = ports.clock.now()
    handoff = Handoff(**{k: tuple(v) if isinstance(v, list) else v
                         for k, v in input["handoff"].items()})
    returned = ports.run(handoff, binding_from_json(input["binding"]))
    out = returned.to_json()
    out["started_at"] = started.isoformat()
    return out


# post_build.

def post_build(ports: Ports, input: Mapping) -> dict:
    build = ports.records.build(input["build_id"])
    project = ports.project(build.project)
    outcome = _seam(ports, POST_BUILD, project, build, input.get("built"))
    return _summary(build, seam=outcome)


# open.

def open_pull_request(ports: Ports, input: Mapping) -> dict:
    build = ports.records.build(input["build_id"])
    if build.state != "returned":
        return _summary(build)
    project = ports.project(build.project)
    reason = not_wanted(ports, project, build.card_key, before_dispatch=False)
    if reason:
        return _summary(_move(ports, build, "abandoned", {"reason": reason}))
    _, returned = _entered(ports, build, "returned")
    commit = returned["commit"]
    host, tracker = ports.host(project), ports.tracker(project)
    if not host.commit_on_remote(build.branch, commit):
        # A head that moved after the return is not the commit Slipway
        # confirmed, and no retry brings it back, so the build ends here and
        # the card can be claimed again.
        why = f"commit {commit} is no longer the head of {build.branch}"
        return _summary(_move(ports, build, "abandoned", {"reason": why, "commit": commit}))
    ports.git.hold(project, build.branch, commit)
    card = tracker.read(build.card_key)
    title = project.title_for(build.card_key, card.title or f"card {build.card_key}")
    pr = host.open_pull_request(build.branch, title, tracker.closing_reference(build.card_key))
    check = host.post_check(commit, CHECK_DETERMINISTIC, CheckState.SUCCESS.value, None,
                            f"{commit} is the commit Slipway confirmed")
    evidence = {"pr_number": pr.number, "url": pr.url, "confirmed": commit}
    # On the external lander path a lander of the project's own lands the
    # branch, so Slipway arms nothing, whatever auto_merge says (U26).
    if project.auto_merge and project.landing_path is not LandingPath.EXTERNAL_LANDER:
        try:
            evidence["enqueue"] = host.enqueue(pr)
        except HostError as error:
            evidence["enqueue"] = scrub(f"not armed: {error}")
    build = _move(ports, build, "pr_open", evidence,
                  {"pr_number": pr.number, "check_run_id": check.id,
                   "confirmed_commit": commit})
    return _summary(build)


# on_wake.

def on_wake(ports: Ports, input: Mapping) -> dict:
    """Handle one wake of the landing wait. Every move here follows a fresh read."""
    build = ports.records.build(input["build_id"])
    if build.state not in WAITING:
        return _summary(build)
    project = ports.project(build.project)
    wake = Wake.from_json(input["wake"])
    if wake.kind == events.CARD_RESUME and build.state == "halted":
        return _summary(_resume(ports, project, build, wake.payload))
    if build.state == "halted":
        # Any other wake while halted is the hourly re verify, or the host's
        # next event, which verify reads for itself.
        return _summary(_move(ports, build, "verifying", {"woke_by": wake.kind}))
    host = ports.host(project)
    pr = host.pull_request(build.pr_number)
    kind = wake.kind
    # card.resume outside a halt asks for a fresh look, which is what the
    # poll path gives.
    if wake.slept or kind == events.CARD_RESUME:
        polled = events.poll_event(pr, build.confirmed_commit, project.repository,
                                   str(project.host))
        if polled is None:
            return _summary(_deadline(ports, project, build))
        fresh = ports.records.ingest_poll(build.project_id, polled)
        kind = polled["kind"]
        if kind == events.PR_SYNCHRONIZE and not fresh:
            # This moved head was flagged on an earlier look; the hourly look
            # still owes the deadline.
            return _summary(_deadline(ports, project, build))
    handler = HANDLERS.get(kind)
    if handler is None:
        return _summary(build)
    build = handler(ports, project, build, pr, wake.payload)
    if wake.slept:
        build = _deadline(ports, project, build)
    if kind in events.GATE_KINDS:
        _seam(ports, GATE_OBSERVED, project, build, {"kind": kind, **wake.payload})
    return _summary(build)


def _deadline(ports: Ports, project, build: Build) -> Build:
    """Seven days in pr_open with nothing landed moves to needs you; the run goes on."""
    if build.state != "pr_open":
        return build
    opened, _ = _entered(ports, build, "pr_open")
    if opened is None or ports.clock.now() - opened < DEADLINE:
        return build
    why = f"pull request #{build.pr_number} has waited {DEADLINE.days} days to land"
    build = _move(ports, build, "awaiting_approval", {"deadline": True, "reason": why})
    _landing_failure(ports, project, build, why + "; approve it or close it")
    return build


def _on_closed(ports, project, build, pr: PullRequest, payload) -> Build:
    if pr.state != CLOSED or pr.merged or build.state not in CLOSABLE:
        return build  # the re read shows the pull request open: a forged or stale event
    return _move(ports, build, "abandoned",
                 {"reason": f"pull request #{pr.number} closed unmerged"})


MERGE_PATHS = {
    "pr_open": ("pr_open", "merged"),
    "awaiting_approval": ("awaiting_approval", "merged"),
    "queued": ("queued", "gate_running", "merged"),
    "gate_running": ("gate_running", "merged"),
    "gate_red": ("gate_red", "queued", "gate_running", "merged"),
}


def _on_merged(ports, project, build, pr: PullRequest, payload) -> Build:
    if not (pr.merged or pr.state == MERGED) or build.state not in MERGE_PATHS:
        return build
    # R19: who approved, or nobody and auto merge, is recorded with the merge.
    evidence = {"merge_commit": pr.merge_commit, "approvals": list(pr.approvals),
                "merged_by": pr.merged_by, "auto_merge": project.auto_merge}
    return _walk(ports, build, MERGE_PATHS[build.state], evidence)


def _on_synchronize(ports, project, build, pr: PullRequest, payload) -> Build:
    if pr.state == OPEN and pr.head and pr.head != build.confirmed_commit:
        ports.host(project).post_check(
            pr.head, CHECK_DETERMINISTIC, CheckState.FAILURE.value, None,
            f"{pr.head} was pushed after Slipway confirmed {build.confirmed_commit}")
    return build


QUEUE_PATHS = {
    "pr_open": ("pr_open", "awaiting_approval", "queued", "gate_running"),
    "awaiting_approval": ("awaiting_approval", "queued", "gate_running"),
    "queued": ("queued", "gate_running"),
    "gate_red": ("gate_red", "queued", "gate_running"),
}


def _on_queue(ports, project, build, pr: PullRequest, payload) -> Build:
    tested = payload.get("tested_commit")
    if not tested or pr.state != OPEN:
        return build
    tested_head = payload.get("tested_head") or pr.head
    host = ports.host(project)
    if tested_head != build.confirmed_commit or pr.head != build.confirmed_commit:
        host.post_check(tested, CHECK_DETERMINISTIC, CheckState.FAILURE.value, None,
                        f"{tested} was built from {tested_head}, not the confirmed "
                        f"{build.confirmed_commit}")
        return build
    host.post_check(tested, CHECK_DETERMINISTIC, CheckState.SUCCESS.value, None,
                    f"{tested} was built from the confirmed {build.confirmed_commit}")
    if build.state not in QUEUE_PATHS:
        return build
    return _walk(ports, build, QUEUE_PATHS[build.state],
                 {"tested_commit": tested, "tested_head": tested_head,
                  "approvals": list(pr.approvals)},
                 {"tested_commit": tested})


DEQUEUE_PATHS = {
    "queued": ("queued", "gate_running", "gate_red"),
    "gate_running": ("gate_running", "gate_red"),
}


def _on_dequeued(ports, project, build, pr: PullRequest, payload) -> Build:
    if pr.state != OPEN or build.state not in DEQUEUE_PATHS:
        return build
    why = f"pull request #{pr.number} left the queue: {payload.get('reason') or 'dequeued'}"
    build = _walk(ports, build, DEQUEUE_PATHS[build.state], {"reason": why})
    _landing_failure(ports, project, build, why + "; fix it and queue it again")
    return build


def _on_check(ports, project, build, pr: PullRequest, payload) -> Build:
    return build  # the gate_observed seam reads it; the state stays where the host put it


HANDLERS = {
    events.PR_CLOSED: _on_closed,
    events.PR_MERGED: _on_merged,
    events.PR_SYNCHRONIZE: _on_synchronize,
    events.QUEUE_REQUESTED: _on_queue,
    events.PR_DEQUEUED: _on_dequeued,
    events.CHECK_COMPLETED: _on_check,
}


def _resume(ports: Ports, project, build: Build, payload: Mapping) -> Build:
    """card.resume: re verify a halt, or land it on the operator's override (KTD11)."""
    _, halted = _entered(ports, build, "halted")
    if not payload.get("override"):
        return _move(ports, build, "verifying", {"woke_by": events.CARD_RESUME,
                                                 "actor": payload.get("actor")})
    actor, why = payload.get("actor"), (payload.get("reason") or "").strip()
    operator = ports.operator()
    evidence = halted.get("evidence") or {}
    held_by = halted.get("halt")
    if held_by != COMMIT_MISMATCH or not why or actor != operator.operator_subject:
        # The land verb refuses these before it pushes (U25); a push that
        # gets here anyway changes nothing.
        return build
    override = {"by": actor, "why": why, "expected": evidence.get("expected"),
                "found": evidence.get("found"), "confirmed": build.confirmed_commit,
                "merged": evidence.get("merged")}
    build = _move(ports, build, "verifying", {"woke_by": events.CARD_RESUME,
                                              "override": override})
    return _move(ports, build, "landed", {"override": override})


# verify.

def verify(ports: Ports, input: Mapping) -> dict:
    """merged to verifying, then landed or halted, with the tracker re read (KTD11)."""
    build = ports.records.build(input["build_id"])
    if build.state == "merged":
        build = _move(ports, build, "verifying", {})
    if build.state != "verifying":
        return _summary(build)
    project = ports.project(build.project)
    host, tracker = ports.host(project), ports.tracker(project)
    pr = host.pull_request(build.pr_number)
    landing = host.probe_landing(project)
    _, dispatched = _entered(ports, build, "dispatched")
    merged = pr.merge_commit
    deadline = ports.clock.now() + NOT_MOVED_RECHECK_WINDOW
    reads = 0
    while True:
        status = tracker.status(build.card_key)
        reads += 1
        cache = ports.git.cache(project, tuple(c for c in (build.confirmed_commit,
                                                           build.tested_commit, merged) if c))
        verdict = ports.verify(cache, dispatched.get("base"), build.confirmed_commit,
                               build.tested_commit, merged, build.card_key, status,
                               landing.path,
                               tested_head=build.confirmed_commit if build.tested_commit
                               else None)
        if verdict.outcome != PARTIAL_LANDING or ports.clock.now() >= deadline:
            break
        ports.clock.sleep(NOT_MOVED_RECHECK_SECONDS)
    checks = {c.name: c.result for c in verdict.checks}
    # The commit verify judged: on the external lander path with no merge
    # commit from the host, verify names the lander's push (verify.landed_at).
    merged = verdict.evidence.get("merged") or merged
    if verdict.landed:
        return _summary(_move(ports, build, "landed",
                              {"merged": merged, "checks": checks, "reads": reads,
                               "landing_path": landing.path}))
    found = verdict.halt or halt(TRACKER_UNREADABLE, card_key=build.card_key,
                                 reason=status.skipped or "the tracker gave no status")
    build = _move(ports, build, "halted",
                  {"halt": found.name, "line": found.line(),
                   "evidence": {**found.evidence, "merged": merged}, "checks": checks,
                   "reads": reads})
    _landing_failure(ports, project, build, found.line())
    return _summary(build)


# post_landed.

def post_landed(ports: Ports, input: Mapping) -> dict:
    build = ports.records.build(input["build_id"])
    if build.state != "landed":
        return _summary(build)
    project = ports.project(build.project)
    outcome = _seam(ports, POST_LANDED, project, build)
    return _summary(_move(ports, build, "scored", {"seam": outcome}))


# The child bodies by name, for worker.py and the tests' fake engine.
BODIES: dict[str, Callable[[Ports, Mapping], dict]] = {
    ATTACH_RUN: attach_run,
    BUILD: run_build,
    POST_BUILD_CHILD: post_build,
    OPEN_PR: open_pull_request,
    ON_WAKE: on_wake,
    VERIFY: verify,
    POST_LANDED_CHILD: post_landed,
}
