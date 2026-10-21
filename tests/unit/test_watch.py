"""U25 test scenarios for the watch cron, through fakes."""

from datetime import timedelta

from _service_fakes import SLUG, ready_card, world
from slipway.service import verbs
from slipway.workflow import watch


def test_watch_dispatches_a_ready_unowned_card_once_across_two_cycles(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])

    first = watch.cycle(w.ports, SLUG)
    w.later(watch.INTERVAL)
    second = watch.cycle(w.ports, SLUG)

    assert [d["key"] for d in first["dispatched"]] == ["EX-1"]
    assert second["dispatched"] == []
    assert len(w.records.claims) == 1 and len(w.engine.starts) == 1
    assert w.records.claims[0]["actor"] == watch.ACTOR
    assert [(r["subject"], r["tool"]) for r in w.records.audit_rows] == [(watch.ACTOR,
                                                                          "dispatch")]


def test_watch_skips_a_card_whose_build_is_awaiting_approval(tmp_path):
    w = world(tmp_path, [ready_card("EX-1"), ready_card("EX-2")])
    held = w.records.seed("EX-1", "awaiting_approval", pr_number=3)

    out = watch.cycle(w.ports, SLUG)

    assert [d["key"] for d in out["dispatched"]] == ["EX-2"]
    assert {"key": "EX-1", "build_id": held, "state": "awaiting_approval"} in out["skipped"]
    assert [c["key"] for c in w.records.claims] == ["EX-2"]


def test_a_stale_claim_is_started_by_watch_without_a_second_claim(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])
    w.engine.fail_start = True
    dispatched = verbs.dispatch(w.ports, SLUG, "EX-1", "user_operator_example")
    w.engine.fail_start = False
    build_id = dispatched["build_id"]

    early = watch.cycle(w.ports, SLUG)
    w.later(watch.INTERVAL + timedelta(seconds=1))
    late = watch.cycle(w.ports, SLUG)

    assert early["retried"] == [] and early["dispatched"] == []
    assert late["retried"] == [{"key": "EX-1", "build_id": build_id, "started": True,
                                "run_id": "run-1"}]
    assert w.engine.starts == [(SLUG, "EX-1", build_id)]
    assert len(w.records.claims) == 1
    assert w.records.builds[build_id]["run_id"] == "run-1"


def test_watch_leaves_returned_empty_and_halted_cards_alone(tmp_path):
    w = world(tmp_path, [ready_card("EX-1"), ready_card("EX-2")])
    w.records.seed("EX-1", "returned_empty")
    w.records.seed("EX-2", "halted", pr_number=2)

    out = watch.cycle(w.ports, SLUG)

    assert out["dispatched"] == [] and w.records.claims == []
    assert w.engine.pushes == []  # the hourly re verify is the run's own wait
    assert {s["state"] for s in out["skipped"]} == {"returned_empty", "halted"}


def test_watch_stops_at_the_daily_cap(tmp_path):
    w = world(tmp_path, [ready_card("EX-A"), ready_card("EX-B")])
    cap = w.ports.project(SLUG).daily_dispatch_cap
    for n in range(cap - 1):
        w.records.seed(f"EX-{n}", "scored")

    out = watch.cycle(w.ports, SLUG)

    assert [d["key"] for d in out["dispatched"]] == ["EX-A"]
    assert "daily_dispatch_cap" in out["cap"]


def test_watch_names_a_card_labelled_for_two_lanes_and_skips_one_not_in_ready(tmp_path):
    w = world(tmp_path, [ready_card("EX-1", labels=("cloud", "laptop")),
                         ready_card("EX-2", status="Backlog")],
              lanes={"other": "laptop"})

    out = watch.cycle(w.ports, SLUG)

    assert out["dispatched"] == [] and w.records.audit_rows == []
    assert [s["key"] for s in out["skipped"]] == ["EX-1"]
    assert "2 lane labels" in out["skipped"][0]["reason"]


def test_an_unreadable_tracker_dispatches_nothing(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])
    w.tracker.reason = "gh timed out"

    out = watch.cycle(w.ports, SLUG)

    assert out["tracker"] == "gh timed out" and w.records.claims == []


def test_run_reports_a_failing_project_and_carries_on(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])
    (w.root / "projects" / "broken.toml").write_text("not = [valid")

    out = watch.run(w.ports)

    by_project = {p["project"]: p for p in out["projects"]}
    assert "error" in by_project["broken"]
    assert [d["key"] for d in by_project[SLUG]["dispatched"]] == ["EX-1"]


def test_register_declares_a_five_minute_cron():
    seen = {}

    class Hatchet:
        def task(self, **options):
            seen.update(options)
            return lambda fn: fn

    watch.register(Hatchet(), make_ports=lambda: None)

    assert seen["name"] == watch.TASK and seen["on_crons"] == ["*/5 * * * *"]
    assert seen["execution_timeout"] < watch.INTERVAL
