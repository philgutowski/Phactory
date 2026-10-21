"""U22 test scenarios for the rollback evaluator, with fixture gate_runs rows."""

from dataclasses import replace
from datetime import timedelta

import pytest

from _runner_fakes import T0, A, B, C, world
from slipway.runner import instances, rollback


def on_b_after_a():
    """vps ran A from T0 and B from an hour later; returns the world and B's row."""
    w = world()
    w.records.activate("vps", A, None, T0)
    b_id = w.records.activate("vps", B, w.records.active_build("vps").id, T0 + timedelta(hours=1))
    w.now = T0 + timedelta(hours=1, minutes=10)
    return w, w.records.runner_build(b_id)


def regressions(w):
    return [f for f in w.records.findings if f["class"] == instances.REGRESSION]


def test_two_projects_one_regresses_and_the_other_had_the_finding_before():
    w, _ = on_b_after_a()
    on_a, on_b = T0 + timedelta(minutes=20), T0 + timedelta(hours=1, minutes=5)
    w.records.gate_run(1, "vps", on_a)
    w.records.gate_run(2, "vps", on_a, rollback.DB_WRITE_ERROR)
    two = w.records.gate_run(2, "vps", on_b, rollback.DB_WRITE_ERROR)
    one = w.records.gate_run(1, "vps", on_b + timedelta(minutes=1), rollback.UNSUPPORTED_STEP)

    first = rollback.evaluate(w.ports, two)
    second = rollback.evaluate(w.ports, one)

    assert first["outcome"] == rollback.NOT_COUNTED
    assert second["outcome"] == rollback.ROLLED_BACK
    assert len(w.scripts.named("rollback")) == 1
    [finding] = regressions(w)
    assert finding["project_id"] == 1

    # Project two, evaluated again after the rollback, is still not counted.
    assert rollback.evaluate(w.ports, two)["outcome"] == rollback.NOT_COUNTED
    assert len(regressions(w)) == 1


def test_two_regressions_in_one_hour_make_one_swap_and_a_second_finding_naming_the_first():
    w, b = on_b_after_a()
    w.records.gate_run(1, "vps", T0 + timedelta(minutes=20))
    w.records.gate_run(2, "vps", T0 + timedelta(minutes=25))
    one = w.records.gate_run(1, "vps", b.activated_at + timedelta(minutes=1), rollback.LOG_GAP)
    two = w.records.gate_run(2, "vps", b.activated_at + timedelta(minutes=2),
                             rollback.CONCLUSION_DISAGREE)

    first = rollback.evaluate(w.ports, one)
    second = rollback.evaluate(w.ports, two)

    assert first["outcome"] == rollback.ROLLED_BACK
    assert second["outcome"] == rollback.ALREADY_ROLLED_BACK
    assert w.scripts.named("rollback") == [("rollback", "vps", "vps", A)]
    original, repeat = regressions(w)
    assert repeat["related_finding_id"] == original["id"] == second["related_finding"]
    assert repeat["gate_run_id"] == two["id"] and repeat["runner_commit"] == B
    assert w.records.active_build("vps").runner_commit == A


def test_a_project_with_no_run_on_the_previous_build_has_no_baseline():
    w, b = on_b_after_a()
    run = w.records.gate_run(3, "vps", b.activated_at + timedelta(minutes=1),
                             rollback.UNSUPPORTED_STEP)

    assert rollback.evaluate(w.ports, run)["outcome"] == rollback.NO_BASELINE
    assert w.records.active_build("vps").runner_commit == B


def test_a_first_build_with_nothing_before_it_is_never_rolled_back():
    w = world()
    w.records.activate("vps", A, None, T0)
    run = w.records.gate_run(1, "vps", T0 + timedelta(minutes=1), rollback.UNSUPPORTED_STEP)

    assert rollback.evaluate(w.ports, run)["outcome"] == rollback.NO_PREVIOUS


def test_a_run_on_a_build_a_normal_swap_replaced_is_superseded():
    w, b = on_b_after_a()
    w.records.gate_run(1, "vps", T0 + timedelta(minutes=20))
    run = w.records.gate_run(1, "vps", b.activated_at + timedelta(minutes=1), rollback.LOG_GAP)
    w.records.activate("vps", C, b.id, b.activated_at + timedelta(minutes=30))

    assert rollback.evaluate(w.ports, run)["outcome"] == rollback.SUPERSEDED
    assert w.records.active_build("vps").runner_commit == C


def test_a_run_no_build_was_active_for_is_unattributed():
    w = world()
    run = w.records.gate_run(1, "vps", T0, rollback.UNSUPPORTED_STEP)

    assert rollback.evaluate(w.ports, run)["outcome"] == rollback.UNATTRIBUTED


def test_a_finding_outside_the_runner_classes_is_not_a_regression():
    w, b = on_b_after_a()
    w.records.gate_run(1, "vps", T0 + timedelta(minutes=20))
    run = w.records.gate_run(1, "vps", b.activated_at + timedelta(minutes=1), "project_red")

    assert rollback.evaluate(w.ports, run)["outcome"] == rollback.NO_RUNNER_FINDING


def test_a_failed_rollback_script_records_nothing_so_a_retry_rolls_back():
    w, b = on_b_after_a()
    w.records.gate_run(1, "vps", T0 + timedelta(minutes=20))
    run = w.records.gate_run(1, "vps", b.activated_at + timedelta(minutes=1), rollback.LOG_GAP)
    w.scripts.failing.add(("rollback", "vps"))

    with pytest.raises(RuntimeError, match="rollback.sh failed"):
        rollback.evaluate(w.ports, run)
    assert w.records.active_build("vps").runner_commit == B and regressions(w) == []

    w.scripts.failing.clear()
    assert rollback.evaluate(w.ports, run)["outcome"] == rollback.ROLLED_BACK


def test_losing_the_compare_and_set_records_a_repeat_not_a_second_rollback():
    w, b = on_b_after_a()
    w.records.gate_run(1, "vps", T0 + timedelta(minutes=20))
    run = w.records.gate_run(1, "vps", b.activated_at + timedelta(minutes=1), rollback.LOG_GAP)
    real = w.records.roll_back

    def raced(build_id, commit, at):
        real(build_id, commit, at)  # another evaluation got there first
        return real(build_id, commit, at)

    w.records.roll_back = raced
    out = rollback.evaluate(w.ports, run)

    assert out["outcome"] == rollback.ALREADY_ROLLED_BACK
    assert [b.state for b in w.records.builds] == ["previous", "rejected", "active"]


def test_an_instance_gone_from_the_operator_file_is_a_named_outcome():
    w, b = on_b_after_a()
    w.records.gate_run(1, "vps", T0 + timedelta(minutes=20))
    run = w.records.gate_run(1, "vps", b.activated_at + timedelta(minutes=1), rollback.LOG_GAP)
    w.operator = replace(w.operator, runner_instances={})

    assert rollback.evaluate(w.ports, run)["outcome"] == rollback.UNKNOWN_INSTANCE
    assert w.scripts.calls == [] and w.records.active_build("vps").runner_commit == B


def test_a_repeat_with_no_first_finding_reads_plainly():
    w, b = on_b_after_a()
    w.records.gate_run(1, "vps", T0 + timedelta(minutes=20))
    w.records.roll_back(b.id, A, b.activated_at + timedelta(minutes=5))
    run = w.records.gate_run(1, "vps", b.activated_at + timedelta(minutes=1), rollback.LOG_GAP)

    out = rollback.evaluate(w.ports, run)

    assert out["outcome"] == rollback.ALREADY_ROLLED_BACK and out["related_finding"] is None
    assert regressions(w)[-1]["excerpt"].endswith("already rolled back.")


def test_losing_the_compare_and_set_to_the_cron_points_the_link_back_at_its_swap():
    w, b = on_b_after_a()
    w.records.gate_run(1, "vps", T0 + timedelta(minutes=20))
    run = w.records.gate_run(1, "vps", b.activated_at + timedelta(minutes=1), rollback.LOG_GAP)

    def cron_won(build_id, commit, at):
        # The hourly swap to C landed between the evaluator's read and write.
        w.records.activate("vps", C, build_id, at)
        return None

    w.records.roll_back = cron_won
    out = rollback.evaluate(w.ports, run)

    assert out["outcome"] == rollback.SUPERSEDED
    assert w.scripts.calls == [("rollback", "vps", "vps", A), ("swap", "vps", "vps", C)]
    assert w.records.active_build("vps").runner_commit == C and regressions(w) == []
