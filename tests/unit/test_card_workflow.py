"""U14 test scenarios: the card workflow's logic, driven through fakes.

The parent body (card.card_workflow) runs on FakeEngine and every child on
the fakes in tests/_card_fakes.py, which also explains how each fake keeps
to what it stands in for. The project and operator files are the real
fixtures, copied under a temporary SLIPWAY_HOME and read through the real
config loaders, so a test that edits the project file between claim and
build sees the edit.
"""

import asyncio
import threading
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from _card_fakes import OPERATOR, Crash, World, drive, parked, run, sha, until
from slipway.builders.base import Schedule
from slipway.halts import BUILDER_EXPIRED, COMMIT_MISMATCH
from slipway.hosts.base import (
    CLOSED,
    MERGED,
    OPEN,
    PullRequest,
)
from slipway.trackers.base import TrackerStatus
from slipway.workflow import card, events, post_landed
from slipway.workflow.events import SLEEP, Wake
from slipway.workflow.post_landed import POST_BUILD, POST_LANDED, Registry, SeamContext

# The scenarios.

def test_a_project_at_cap_holds_the_next_build_and_releases_it_when_a_build_returns(tmp_path):
    world = World(tmp_path, advance=False, projects={"alpha": 1, "beta": 2})
    alpha = [world.card(f"A{n}", "alpha") for n in (1, 2)]
    beta = [world.card(f"B{n}", "beta") for n in (1, 2, 3)]
    for key in ("A1", "A2", "B1", "B2", "B3"):
        world.builder.gates[key] = threading.Event()

    async def scenario():
        a1 = world.start(alpha[0], project="alpha")
        await until(lambda: world.state(alpha[0]) == "building")
        a2 = world.start(alpha[1], "run-2", project="alpha")
        betas = [world.start(b, f"run-b{i}", project="beta") for i, b in enumerate(beta)]
        await until(lambda: sum(world.state(b) == "building" for b in beta) == 2)
        await asyncio.sleep(0.05)
        # alpha at cap 1: a2's build child is held, and a2 recorded waiting_for_slot.
        assert world.state(alpha[1]) == "waiting_for_slot"
        assert world.engine.active[(card.BUILD, "alpha")] == 1
        # beta at cap 2: two building, the third held.
        assert world.engine.active[(card.BUILD, "beta")] == 2
        assert sum(world.state(b) == "claimed" for b in beta) == 1

        world.builder.gates["A1"].set()
        await parked(world, alpha[0])
        # a1's parent now waits for approval and holds no key, so a2 builds.
        await until(lambda: world.state(alpha[1]) == "building")
        assert world.state(alpha[0]) == "pr_open"
        for key in ("A2", "B1", "B2", "B3"):
            world.builder.gates[key].set()
        for build_id in (alpha[1], *beta):
            await parked(world, build_id)
        for build_id in (*alpha, *beta):
            world.host.change(world.pr_of(build_id), state=MERGED, merged=True,
                              merge_commit=sha(900 + build_id))
            world.engine.push(events.PR_MERGED, build_id)
        return await asyncio.gather(a1, a2, *betas)

    outcomes = run(scenario)
    assert [o["outcome"] for o in outcomes] == ["scored"] * 5
    assert world.engine.peak[(card.BUILD, "alpha")] == 1
    assert world.engine.peak[(card.BUILD, "beta")] == 2
    assert world.records.path(alpha[1])[:3] == ["claimed", "waiting_for_slot", "dispatched"]


def test_two_parents_for_one_build_leave_one_active(tmp_path):
    world = World(tmp_path)
    build_id = world.card("EX-1")

    async def scenario():
        first = world.start(build_id, "run-1")
        second = world.start(build_id, "run-2")
        done = await second
        await parked(world, build_id)
        world.host.change(world.pr_of(build_id), state=MERGED, merged=True,
                          merge_commit=sha(99))
        world.engine.push(events.PR_MERGED, build_id)
        return await first, done

    first, second = run(scenario)
    assert second == {"outcome": "not_attached", "held_by": "run-1"}
    assert first["outcome"] == "scored"
    assert len(world.builder.starts) == 1
    assert world.engine.started.count((card.BUILD, build_id)) == 1


def test_a_builder_returning_no_branch_ends_returned_empty_through_post_build(tmp_path):
    world = World(tmp_path)
    seen = []
    world.seams.register(POST_BUILD, lambda ctx: seen.append(("export", ctx.build["id"])),
                         "export_trace")
    world.seams.register(POST_BUILD, lambda ctx: seen.append(("release", ctx.build["id"])),
                         "release_session")
    world.builder.push = False
    build_id = world.card("EX-2")

    outcome = drive(world, build_id)
    assert outcome["outcome"] == "returned_empty"
    assert world.records.path(build_id) == ["claimed", "dispatched", "building",
                                            "returned_empty"]
    assert world.records.evidence(build_id, "returned_empty")["halt"] == "builder_failed"
    assert seen == [("export", build_id), ("release", build_id)]
    assert world.host.created == 0


def test_a_budget_expiry_ends_the_build_returned_empty_with_builder_expired(tmp_path):
    world = World(tmp_path)
    world.builder.never_finish = True
    build_id = world.card("EX-3")

    outcome = drive(world, build_id)
    assert outcome["outcome"] == "returned_empty"
    evidence = world.records.evidence(build_id, "returned_empty")
    assert evidence["halt"] == BUILDER_EXPIRED
    assert evidence["budget_seconds"] == 90 * 60
    started, _ = world.records.entered(build_id, "building")
    assert world.clock.now() - started >= timedelta(minutes=90)


def test_a_worker_process_build_queued_past_its_budget_still_runs_to_completion(tmp_path):
    world = World(tmp_path)
    world.builder.schedule = Schedule.WORKER_PROCESS
    world.laptop_queue_seconds = 3 * 90 * 60  # the laptop was closed for four and a half hours
    build_id = world.card("EX-4")

    async def scenario():
        task = world.start(build_id)
        await parked(world, build_id)
        return task

    run(scenario)
    assert world.state(build_id) == "pr_open"
    building = world.records.evidence(build_id, "building")
    claimed_at = world.records.entered(build_id, "claimed")[0]
    started = datetime.fromisoformat(building["started_at"])
    assert started - claimed_at >= timedelta(seconds=world.laptop_queue_seconds)
    assert building["worker"] == "laptop-builder"
    assert world.builder.starts == []  # the laptop ran the session, not the VPS


def test_a_step_re_executed_after_a_crash_opens_no_second_pr_and_starts_no_second_session(
        tmp_path):
    world = World(tmp_path)
    ports = world.ports

    # A crash between the session start and the record of its claim.
    first = world.card("EX-5")
    world.records.attach_run(first, "run-1")
    world.records.crash_before.add("building")
    with pytest.raises(Crash):
        card.run_build(ports, {"build_id": first})
    again = card.run_build(ports, {"build_id": first})
    assert len(world.builder.starts) == 1
    assert again["state"] == "returned_empty"
    assert "may have started" in world.records.evidence(first, "returned_empty")["reason"]

    # A crash after the claim was recorded: the re run polls, it does not start.
    second = world.card("EX-6")
    world.records.crash_before.add("returned")
    with pytest.raises(Crash):
        card.run_build(ports, {"build_id": second})
    assert world.state(second) == "building"
    card.run_build(ports, {"build_id": second})
    assert len(world.builder.starts) == 2
    assert world.state(second) == "returned"

    # A crash between opening the pull request and recording it.
    world.host.crash_after_open = True
    with pytest.raises(Crash):
        card.open_pull_request(ports, {"build_id": second})
    assert world.state(second) == "returned"
    opened = card.open_pull_request(ports, {"build_id": second})
    assert world.host.created == 1
    assert opened["state"] == "pr_open"
    assert opened["pr_number"] == 101


async def _opened(world, key):
    build_id = world.card(key)
    task = world.start(build_id)
    await parked(world, build_id)
    return build_id, task


def test_a_pr_closed_event_abandons_only_after_the_re_read_confirms_it(tmp_path):
    world = World(tmp_path)

    async def scenario():
        build_id, task = await _opened(world, "EX-7")
        world.engine.push(events.PR_CLOSED, build_id)  # forged: the pull request is open
        await until(lambda: world.engine.parked(build_id) == 1 and len(
            [s for s in world.engine.started if s == (card.ON_WAKE, build_id)]) == 1)
        assert world.state(build_id) == "pr_open"
        world.host.change(world.pr_of(build_id), state=CLOSED)
        world.engine.push(events.PR_CLOSED, build_id)
        return build_id, await task

    build_id, outcome = run(scenario)
    assert outcome["outcome"] == "abandoned"
    assert world.records.path(build_id)[-2:] == ["pr_open", "abandoned"]


def test_a_pr_synchronize_posts_a_failing_check_on_the_new_head_and_changes_nothing(tmp_path):
    world = World(tmp_path)

    async def scenario():
        build_id, task = await _opened(world, "EX-8")
        world.host.change(world.pr_of(build_id), head=sha(777))
        world.engine.push(events.PR_SYNCHRONIZE, build_id, head_commit=sha(777))
        await until(lambda: world.host.checks_on(sha(777)))
        await parked(world, build_id)
        task.cancel()
        return build_id

    build_id = run(scenario)
    assert world.host.checks_on(sha(777)) == [(card.CHECK_DETERMINISTIC, "failure")]
    assert world.state(build_id) == "pr_open"


def test_queue_requested_records_the_tested_commit_only_when_built_from_the_confirmed_head(
        tmp_path):
    world = World(tmp_path)

    async def scenario():
        build_id, task = await _opened(world, "EX-9")
        confirmed = world.records.builds[build_id]["confirmed_commit"]
        world.engine.push(events.QUEUE_REQUESTED, build_id, tested_commit=sha(501),
                          tested_head=sha(400))
        await until(lambda: world.host.checks_on(sha(501)))
        await parked(world, build_id)
        assert world.host.checks_on(sha(501)) == [(card.CHECK_DETERMINISTIC, "failure")]
        assert world.state(build_id) == "pr_open"
        world.engine.push(events.QUEUE_REQUESTED, build_id, tested_commit=sha(502),
                          tested_head=confirmed)
        await until(lambda: world.state(build_id) == "gate_running")
        await parked(world, build_id)
        task.cancel()
        return build_id

    build_id = run(scenario)
    assert world.host.checks_on(sha(502)) == [(card.CHECK_DETERMINISTIC, "success")]
    assert world.records.builds[build_id]["tested_commit"] == sha(502)
    assert world.records.path(build_id)[-3:] == ["awaiting_approval", "queued", "gate_running"]


def test_a_failing_check_completed_reaches_gate_observed_while_the_card_stays_gate_red(
        tmp_path):
    world = World(tmp_path)
    observed = []
    world.seams.register(post_landed.GATE_OBSERVED,
                         lambda ctx: observed.append((ctx.event["kind"], ctx.build["state"])),
                         "observe")

    async def scenario():
        build_id, task = await _opened(world, "EX-10")
        confirmed = world.records.builds[build_id]["confirmed_commit"]
        world.engine.push(events.QUEUE_REQUESTED, build_id, tested_commit=sha(601),
                          tested_head=confirmed)
        await until(lambda: world.state(build_id) == "gate_running")
        await parked(world, build_id)
        world.engine.push(events.PR_DEQUEUED, build_id, reason="checks failed")
        await until(lambda: world.state(build_id) == "gate_red")
        await parked(world, build_id)
        world.engine.push(events.CHECK_COMPLETED, build_id, check_name="ci",
                          check_state="failure")
        await until(lambda: len(observed) == 3)
        await parked(world, build_id)
        task.cancel()
        return build_id, confirmed

    build_id, confirmed = run(scenario)
    assert observed == [(events.QUEUE_REQUESTED, "gate_running"),
                        (events.PR_DEQUEUED, "gate_red"),
                        (events.CHECK_COMPLETED, "gate_red")]
    assert world.state(build_id) == "gate_red"
    assert (card.CHECK_LANDING, "failure") in world.host.checks_on(confirmed)


def test_a_card_relabelled_during_building_moves_to_abandoned_at_the_step_boundary(tmp_path):
    world = World(tmp_path, advance=False)
    released = []
    world.seams.register(POST_BUILD, lambda ctx: released.append(ctx.build["session_ref"]),
                         "release_session")
    build_id = world.card("EX-11")
    world.builder.gates["EX-11"] = threading.Event()

    async def scenario():
        task = world.start(build_id)
        await until(lambda: world.state(build_id) == "building")
        world.tracker.cards["EX-11"] = replace(world.tracker.cards["EX-11"], labels=())
        world.builder.gates["EX-11"].set()
        return await task

    outcome = run(scenario)
    assert outcome["outcome"] == "abandoned"
    assert "lane label cloud" in world.records.evidence(build_id, "abandoned")["reason"]
    assert released == ["session-EX-11-1"]
    assert world.host.created == 0


def test_verify_reads_the_tracker_until_the_card_moves_within_ten_minutes(tmp_path):
    world = World(tmp_path)
    build_id = world.card("EX-12")
    world.tracker.statuses["EX-12"] = [TrackerStatus("In review", False),
                                       TrackerStatus("In review", False),
                                       TrackerStatus("Done", True)]

    async def scenario():
        task = world.start(build_id)
        await parked(world, build_id)
        world.host.change(world.pr_of(build_id), state=MERGED, merged=True,
                          merge_commit=sha(42))
        world.engine.push(events.PR_MERGED, build_id, merge_commit=sha(42))
        return await task

    outcome = run(scenario)
    assert outcome["outcome"] == "scored"
    assert "halted" not in world.records.path(build_id)
    assert world.records.evidence(build_id, "landed")["reads"] == 3
    assert world.verify_calls == 3
    assert world.records.path(build_id)[-5:] == ["pr_open", "merged", "verifying", "landed",
                                                 "scored"]
    assert world.records.evidence(build_id, "merged")["auto_merge"] is True


def test_a_commit_mismatch_halts_and_an_operator_override_lands_it(tmp_path):
    world = World(tmp_path)
    world.verdicts = [COMMIT_MISMATCH]
    build_id = world.card("EX-13")

    async def scenario():
        task = world.start(build_id)
        await parked(world, build_id)
        world.host.change(world.pr_of(build_id), state=MERGED, merged=True,
                          merge_commit=sha(43))
        world.engine.push(events.PR_MERGED, build_id)
        await until(lambda: world.state(build_id) == "halted")
        await parked(world, build_id)
        confirmed = world.records.builds[build_id]["confirmed_commit"]
        assert (card.CHECK_LANDING, "failure") in world.host.checks_on(confirmed)
        # Not the operator: nothing moves.
        world.engine.push(events.CARD_RESUME, build_id, actor="agent_manager_example",
                          reason="looks fine", override=True)
        await until(lambda: world.engine.started.count((card.ON_WAKE, build_id)) == 2)
        await parked(world, build_id)
        assert world.state(build_id) == "halted"
        world.engine.push(events.CARD_RESUME, build_id, actor=OPERATOR,
                          reason="the squash reordered two files; read both trees",
                          override=True)
        return await task, confirmed

    outcome, confirmed = run(scenario)
    assert outcome["outcome"] == "scored"
    override = world.records.evidence(build_id, "landed")["override"]
    assert override == {"by": OPERATOR, "why": "the squash reordered two files; read both trees",
                        "expected": "tree-1", "found": "tree-2", "confirmed": confirmed,
                        "merged": sha(43)}
    assert world.records.path(build_id)[-5:] == ["verifying", "halted", "verifying", "landed",
                                                 "scored"]


def test_the_seven_day_deadline_moves_to_needs_you_without_ending_the_run(tmp_path):
    world = World(tmp_path)

    async def scenario():
        build_id, task = await _opened(world, "EX-14")
        world.clock.forward(timedelta(days=3).total_seconds())
        world.engine.push(SLEEP, build_id)
        await until(lambda: world.engine.started.count((card.ON_WAKE, build_id)) == 1)
        await parked(world, build_id)
        assert world.state(build_id) == "pr_open"
        world.clock.forward(timedelta(days=4).total_seconds())
        world.engine.push(SLEEP, build_id)
        await until(lambda: world.state(build_id) == "awaiting_approval")
        await parked(world, build_id)  # the run is still alive and waiting
        world.host.change(world.pr_of(build_id), state=MERGED, merged=True,
                          merge_commit=sha(44))
        world.engine.push(SLEEP, build_id)  # the poll path finds the merge
        return build_id, await task

    build_id, outcome = run(scenario)
    assert outcome["outcome"] == "scored"
    assert world.records.evidence(build_id, "awaiting_approval")["deadline"] is True
    assert world.records.path(build_id)[-6:] == ["pr_open", "awaiting_approval", "merged",
                                                 "verifying", "landed", "scored"]
    assert [e["kind"] for e in world.records.polled] == [events.PR_MERGED]


def test_the_binding_read_at_the_build_child_reflects_an_edit_after_claim(tmp_path):
    world = World(tmp_path)
    build_id = world.card("EX-15")
    path = world.home / "projects" / "example.toml"
    path.write_text(path.read_text().replace('model = "model-large"', 'model = "model-edited"'))

    async def scenario():
        task = world.start(build_id)
        await parked(world, build_id)
        task.cancel()

    run(scenario)
    assert world.builder.starts == [(f"slipway/EX-15-{build_id}", "model-edited")]
    assert world.records.evidence(build_id, "dispatched")["binding"]["model"] == "model-edited"


def test_a_seam_runs_its_steps_in_registration_order_and_an_empty_seam_is_a_no_op():
    registry = Registry()
    calls = []
    registry.register(POST_LANDED, lambda ctx: calls.append("deterministic") or {"total": 3},
                      "score_deterministic")
    registry.register(POST_LANDED, lambda ctx: calls.append("judged"), "score_judged")
    context = SeamContext(POST_LANDED, None, {"id": 1}, None)
    assert registry.run(POST_LANDED, context) == {"score_deterministic": {"total": 3},
                                                  "score_judged": {}}
    assert calls == ["deterministic", "judged"]
    assert registry.run(POST_BUILD, replace(context, seam=POST_BUILD)) == {}
    with pytest.raises(ValueError):
        registry.register("post_merge", lambda ctx: None)


# The pieces around the scenarios.

def test_load_registers_each_present_steps_module_in_order_and_skips_absent_ones():
    class Module:
        def __init__(self, *pairs):
            self.SLIPWAY_STEPS = pairs

    def first(ctx):
        return None

    def second(ctx):
        return None

    def third(ctx):
        return None

    modules = {"one.steps": Module((POST_LANDED, first), (POST_BUILD, second)),
               "three.steps": Module((POST_LANDED, third))}

    def importer(name):
        if name not in modules:
            raise ModuleNotFoundError(name=name)
        return modules[name]

    registry = post_landed.load(("one.steps", "two.steps", "three.steps"), importer)
    assert registry.names(POST_LANDED) == ["first", "third"]
    assert registry.names(POST_BUILD) == ["second"]
    assert post_landed.load().names(POST_LANDED) == []  # this unit registers nothing


def test_a_steps_module_that_fails_its_own_import_is_not_skipped():
    def importer(name):
        raise ModuleNotFoundError(name="slipway.trace.langfuse")

    with pytest.raises(ModuleNotFoundError):
        post_landed.load(("slipway.trace.steps",), importer)


def test_the_concurrency_keys_sit_on_build_and_open_only():
    keyed = {name: (c.key, c.max_runs) for name, c in card.CHILDREN.items() if c.key}
    assert keyed == {card.BUILD: ("project", "cap"), card.OPEN_PR: ("repository", 1)}
    assert card.PARENT_EXECUTION > card.DEADLINE
    assert card.CHILDREN[card.LAPTOP_BUILD].retries == 0


def test_task_options_turn_the_table_into_hatchet_declarations():
    from slipway.workflow import worker

    build = worker.task_options(card.CHILDREN[card.BUILD])
    assert build["concurrency"].expression == "input.project"
    assert build["concurrency"].max_runs == "input.cap"
    opened = worker.task_options(card.CHILDREN[card.OPEN_PR])
    assert opened["concurrency"].expression == "input.repository"
    assert opened["concurrency"].max_runs == 1
    assert "concurrency" not in worker.task_options(card.CHILDREN[card.ON_WAKE])
    assert build["execution_timeout"] > timedelta(hours=1)


def test_register_declares_the_parent_durable_with_no_key_and_a_timeout_past_the_deadline():
    from slipway.workflow import worker

    declared = {}

    class FakeHatchet:
        class stubs:
            @staticmethod
            def task(**options):
                declared["stub"] = options
                return "stub"

        def task(self, **options):
            def wrap(fn):
                declared[options["name"]] = options
                return fn
            return wrap

        def durable_task(self, **options):
            def wrap(fn):
                declared["parent"] = options
                return fn
            return wrap

    tasks = worker.register(FakeHatchet(), make_ports=lambda: None)
    assert set(tasks) == {*card.BODIES, card.LAPTOP_BUILD, "card_workflow"}
    assert declared["parent"]["name"] == "card_workflow"
    assert "concurrency" not in declared["parent"]
    assert declared["parent"]["execution_timeout"] > timedelta(days=7)
    assert declared["stub"]["name"] == card.LAPTOP_BUILD


def test_a_wait_answer_reads_as_the_condition_that_ended_it():
    answer = {"CREATE": {events.PR_MERGED: [{"build_id": 3, "merge_commit": "m"}]}}
    assert events.read_wake(answer) == Wake(events.PR_MERGED, {"build_id": 3,
                                                               "merge_commit": "m"})
    assert events.read_wake({"CREATE": {SLEEP: [{"sleep_duration": "1h"}]}}).slept
    assert events.filter_expression(7) == "input.build_id == 7"
    assert set(events.HOST_KINDS) < set(events.WAIT_KINDS)
    assert events.CARD_RESUME in events.WAIT_KINDS
    with pytest.raises(ValueError):
        events.read_wake({})


def test_the_poll_path_turns_a_re_read_into_the_event_the_host_would_send():
    pr = PullRequest(5, OPEN, False, "c1")
    assert events.poll_event(pr, "c1", "o/r", "github") is None
    assert events.poll_event(replace(pr, head="c2"), "c1", "o/r", "github")["kind"] == (
        events.PR_SYNCHRONIZE)
    merged = events.poll_event(replace(pr, state=MERGED, merged=True, merge_commit="m"), "c1",
                               "o/r", "github")
    assert (merged["kind"], merged["merge_commit"], merged["delivery_id"]) == (
        events.PR_MERGED, "m", None)
    assert events.poll_event(replace(pr, state=CLOSED), "c1", "o/r", "github")["kind"] == (
        events.PR_CLOSED)


def test_the_engine_facade_names_the_workflow_and_carries_the_build_id():
    from slipway.service import engine

    pushed, started = [], []

    class Client:
        class runs:
            @staticmethod
            def create(name, input):
                started.append((name, input))

                class Details:
                    class run:
                        class metadata:
                            id = "run-9"
                return Details

        class event:
            @staticmethod
            def push(kind, payload):
                pushed.append((kind, payload))

    facade = engine.HatchetEngine(Client)
    assert facade.start_card("example", "EX-1", 4) == "run-9"
    assert started == [("card_workflow", {"project": "example", "key": "EX-1", "build_id": 4})]
    engine.push_host_event(facade, 4, {"kind": events.PR_MERGED, "pr_number": 7})
    assert pushed == [(events.PR_MERGED, {"kind": events.PR_MERGED, "pr_number": 7,
                                          "build_id": 4})]
