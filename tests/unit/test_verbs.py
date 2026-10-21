"""U25 test scenarios for the verbs and the composed status, through fakes.

The records, the engine, and the tracker are the fakes in _service_fakes;
the project and operator files are the config fixtures under a temporary
SLIPWAY_HOME. Nothing here reaches a network or a database.
"""

from datetime import timedelta

import pytest

from _service_fakes import MANAGER, OPERATOR, SLUG, ready_card, world
from slipway.config import ConfigError
from slipway.config.bindings import BindingInvalid
from slipway.halts import COMMIT_MISMATCH, MERGED_CARD_NOT_MOVED
from slipway.service import status, verbs
from slipway.service.engine import CARD_RESUME
from slipway.trackers.base import Card

HALT = {"halt": COMMIT_MISMATCH, "line": "commit mismatch: the merge is not the confirmed commit",
        "evidence": {"expected": "a" * 40, "found": "c" * 40}}


# dispatch.

def test_three_dispatches_in_a_minute_make_one_claim_one_run_three_audits(tmp_path):
    """Covers AE9."""
    w = world(tmp_path, [ready_card("EX-1")])
    results = []
    for _ in range(3):
        results.append(verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR))
        w.later(timedelta(seconds=20))

    assert len(w.records.claims) == 1
    assert len(w.engine.starts) == 1
    assert [r["subject"] for r in w.records.audit_rows] == [OPERATOR] * 3
    assert [r["tool"] for r in w.records.audit_rows] == ["dispatch"] * 3
    first, second, third = results
    assert first["outcome"] == verbs.DISPATCHED and first["started"] is True
    assert second["outcome"] == third["outcome"] == verbs.OWNED
    assert second["build_id"] == third["build_id"] == first["build_id"]
    assert second["state"] == "claimed"


def test_a_lost_claim_race_returns_the_winner_and_starts_nothing(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])
    winner = w.records.claim(w.records.project_id(SLUG), "EX-1", "watch", None, None, None)
    latest = w.records.latest
    w.records.latest = lambda project_id, key: None  # the read ran before the winner's claim

    result = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR)

    w.records.latest = latest
    assert result["outcome"] == verbs.OWNED
    assert result["build_id"] == winner.build_id
    assert w.engine.starts == []


def test_a_card_at_the_daily_cap_is_refused_with_the_cap_named(tmp_path):
    w = world(tmp_path, [ready_card("EX-99")])
    cap = w.ports.project(SLUG).daily_dispatch_cap
    for n in range(cap):
        w.records.seed(f"EX-{n}", "scored")

    result = verbs.dispatch(w.ports, SLUG, "EX-99", OPERATOR)

    assert result["outcome"] == verbs.REFUSED
    assert result["cap"] == cap
    assert f"daily_dispatch_cap of {cap}" in result["reason"]
    assert w.records.claims == [] and w.engine.starts == []
    assert w.records.audit_rows[-1]["result"]["outcome"] == verbs.REFUSED


def test_the_cap_counts_today_only(tmp_path):
    w = world(tmp_path, [ready_card("EX-99")])
    cap = w.ports.project(SLUG).daily_dispatch_cap
    for n in range(cap):
        w.records.seed(f"EX-{n}", "scored", created=w.clock() - timedelta(days=1))

    assert verbs.dispatch(w.ports, SLUG, "EX-99", OPERATOR)["outcome"] == verbs.DISPATCHED


def test_an_unlabelled_card_is_refused_with_the_allowed_labels(tmp_path):
    w = world(tmp_path, [ready_card("EX-1", labels=("bug",))])

    result = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR)

    assert result["outcome"] == verbs.REFUSED
    assert result["allowed_labels"] == ["cloud"]
    assert "no lane label" in result["reason"]
    assert w.records.claims == []


def test_a_card_labelled_for_two_lanes_is_refused(tmp_path):
    """The other projects' lane words reach the ready rule (card #47's note)."""
    w = world(tmp_path, [ready_card("EX-1", labels=("cloud", "laptop"))],
              lanes={"other": "laptop"})

    result = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR)

    assert result["outcome"] == verbs.REFUSED
    assert "2 lane labels" in result["reason"]


def test_a_card_whose_latest_build_lives_is_refused_with_that_build(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])
    build_id = w.records.seed("EX-1", "gate_red", pr_number=7)

    result = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR, reason="try again")

    assert result["outcome"] == verbs.OWNED
    assert (result["build_id"], result["state"]) == (build_id, "gate_red")
    assert w.records.claims == []


def test_returned_empty_reopens_only_with_a_reason(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])
    w.records.seed("EX-1", "returned_empty")

    refused = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR)
    reopened = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR, reason="builder fixed")

    assert refused["outcome"] == verbs.REFUSED and "with a reason" in refused["reason"]
    assert reopened["outcome"] == verbs.DISPATCHED
    assert w.records.claims[0]["reason"] == "builder fixed"


def test_a_start_that_fails_leaves_the_claim_for_watch(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])
    w.engine.fail_start = True

    result = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR)

    assert result["outcome"] == verbs.DISPATCHED and result["started"] is False
    assert "engine unreachable" in result["start_error"]
    assert w.records.builds[result["build_id"]]["run_id"] is None


def test_the_claim_carries_the_cards_paths_and_title(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])

    verbs.dispatch(w.ports, SLUG, "EX-1", MANAGER, reason="now")

    claim = w.records.claims[0]
    assert claim["paths"] == ["src/page.py"] and claim["title"] == "Card EX-1"
    assert claim["actor"] == MANAGER


def test_a_token_shaped_reason_is_scrubbed_in_the_audit_row(tmp_path):
    w = world(tmp_path, [ready_card("EX-1")])
    token = "ghp_" + "a" * 36

    verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR, reason=f"with {token}")

    assert token not in str(w.records.audit_rows[-1]["arguments"])


# land.

def test_land_with_an_override_and_no_reason_is_refused(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-1", "halted", pr_number=7, evidence=HALT)

    result = verbs.land(w.ports, SLUG, "EX-1", OPERATOR, override=True)

    assert result["outcome"] == verbs.REFUSED and "needs a reason" in result["reason"]
    assert w.engine.pushes == []
    assert w.records.audit_rows[-1]["tool"] == "land"


def test_land_with_an_override_from_the_operator_pushes_one_resume_and_writes_nothing(tmp_path):
    w = world(tmp_path)
    build_id = w.records.seed("EX-1", "halted", pr_number=7, evidence=HALT)
    builds_before = {k: dict(v) for k, v in w.records.builds.items()}
    transitions_before = list(w.records.transitions)

    result = verbs.land(w.ports, SLUG, "EX-1", OPERATOR, override=True, reason="squash rewrote")

    assert result["outcome"] == verbs.PUSHED
    assert w.engine.pushes == [(CARD_RESUME, build_id, {
        "actor": OPERATOR, "reason": "squash rewrote", "override": True})]
    assert w.records.builds == builds_before
    assert w.records.transitions == transitions_before
    assert w.records.audit_rows == [] and w.records.claims == []


def test_land_with_an_override_from_another_subject_returns_the_halt_and_audits(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-1", "halted", pr_number=7, evidence=HALT)

    result = verbs.land(w.ports, SLUG, "EX-1", MANAGER, override=True, reason="please")

    assert result["outcome"] == verbs.REFUSED
    assert result["halt"]["class"] == COMMIT_MISMATCH
    assert result["halt"]["line"] == HALT["line"]
    assert result["link"] == "https://github.com/example-org/example-app/pull/7"
    assert w.engine.pushes == []
    [row] = w.records.audit_rows
    assert (row["subject"], row["tool"]) == (MANAGER, "land")
    assert row["arguments"]["override"] is True


def test_land_with_an_override_on_another_halt_is_refused(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-1", "halted", pr_number=7,
                   evidence={**HALT, "halt": MERGED_CARD_NOT_MOVED})

    result = verbs.land(w.ports, SLUG, "EX-1", OPERATOR, override=True, reason="moved by hand")

    assert result["outcome"] == verbs.REFUSED and COMMIT_MISMATCH in result["reason"]
    assert w.engine.pushes == []


def test_land_without_override_resumes_a_halt_for_anyone(tmp_path):
    w = world(tmp_path)
    build_id = w.records.seed("EX-1", "halted", pr_number=7, evidence=HALT)

    result = verbs.land(w.ports, SLUG, "EX-1", MANAGER)

    assert result["outcome"] == verbs.PUSHED
    assert w.engine.pushes == [(CARD_RESUME, build_id, {
        "actor": MANAGER, "reason": None, "override": False})]


def test_land_on_a_card_awaiting_approval_waits_and_changes_nothing(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-1", "awaiting_approval", pr_number=7)

    result = verbs.land(w.ports, SLUG, "EX-1", MANAGER)

    assert result["outcome"] == verbs.WAITING
    assert result["link"] == "https://github.com/example-org/example-app/pull/7"
    assert w.engine.pushes == [] and w.records.audit_rows == []


def test_land_on_a_card_with_no_build_is_not_found(tmp_path):
    w = world(tmp_path)
    assert verbs.land(w.ports, SLUG, "EX-1", OPERATOR)["outcome"] == verbs.NOT_FOUND


# status.

def _five_cards(w):
    for key in ("EX-1", "EX-2", "EX-3"):
        w.records.seed(key, "building")
    w.records.seed("EX-4", "pr_open", pr_number=4)
    w.records.seed("EX-5", "halted", pr_number=5, evidence=HALT)


def test_compose_names_all_three_facts_and_agrees_with_the_card_query(tmp_path):
    w = world(tmp_path)
    _five_cards(w)

    found = status.compose(w.ports, SLUG)

    assert found["counts"] == {"being_built": 3, "waiting_to_land": 1, "needs_you": 1,
                               "landed_today": 0}
    spoken = found["spoken"]
    assert "3 being built" in spoken
    assert "1 waiting to land" in spoken
    assert "1 needs you" in spoken
    assert "EX-5 is halted" in spoken and "commit mismatch" in spoken
    project_id = w.records.project_id(SLUG)
    for entry in found["cards"]:
        latest = w.records.card(project_id, entry["key"])["builds"][0]
        assert (entry["build_id"], entry["state"]) == (latest["build_id"], latest["state"])
    [needs] = found["needs_you"]
    assert needs["key"] == "EX-5"
    assert needs["link"] == "https://github.com/example-org/example-app/pull/5"
    assert needs["line"] == HALT["line"] and needs["what"]
    assert w.records.snapshots == 1


def test_compose_shows_the_latest_build_of_a_reopened_card(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-1", "returned_empty", evidence={"line": "the builder failed"})
    w.records.seed("EX-1", "building")

    found = status.compose(w.ports, SLUG)

    assert [(c["key"], c["state"]) for c in found["cards"]] == [("EX-1", "building")]
    assert found["needs_you"] == []


def test_compose_counts_what_landed_today(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-1", "scored")
    w.records.seed("EX-2", "scored", at=w.clock() - timedelta(days=2))

    found = status.compose(w.ports, SLUG)

    assert found["counts"]["landed_today"] == 1
    assert found["spoken"] == "1 landed today."


def test_compose_with_nothing_says_so(tmp_path):
    assert status.compose(world(tmp_path).ports, SLUG)["spoken"] == "Nothing is in flight."


def test_across_runs_compose_over_every_project_for_the_operator(tmp_path):
    w = world(tmp_path, lanes={"other": "laptop"})
    _five_cards(w)
    w.records.seed("OT-1", "gate_red", project="other", pr_number=9,
                   evidence={"reason": "pull request #9 left the queue"})

    found = status.across(w.ports, OPERATOR)

    assert [p["project"] for p in found["projects"]] == [SLUG, "other"]
    assert found["counts"]["needs_you"] == 2
    assert "2 need you" in found["spoken"] and "other OT-1 is gate red" in found["spoken"]


def test_across_is_refused_to_any_other_subject(tmp_path):
    w = world(tmp_path)
    found = status.across(w.ports, MANAGER)
    assert found["outcome"] == "refused" and "projects" not in found


@pytest.mark.parametrize("error", [
    BindingInvalid("tracker_credential", "EXAMPLE_TOKEN is not set"),
    ConfigError("operator.toml", "tracker_credentials", "missing"),
], ids=["binding", "config"])
def test_a_tracker_that_cannot_be_built_is_refused_and_audited(tmp_path, error):
    w = world(tmp_path, [ready_card("EX-1")])

    def unbuildable(project):
        raise error

    ports = verbs.Ports(**{**w.ports.__dict__, "tracker": unbuildable})

    result = verbs.dispatch(ports, SLUG, "EX-1", OPERATOR)

    assert result["outcome"] == verbs.REFUSED and "tracker_credential" in result["reason"]
    assert [r["tool"] for r in w.records.audit_rows] == ["dispatch"]
    assert w.records.claims == []


def test_an_owned_card_the_board_moved_on_still_returns_its_build(tmp_path):
    """The board moves a card out of Ready once its branch exists; AE9 still holds."""
    w = world(tmp_path, [ready_card("EX-1", status="In progress")])
    build_id = w.records.seed("EX-1", "building")

    result = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR)

    assert result["outcome"] == verbs.OWNED
    assert (result["build_id"], result["state"]) == (build_id, "building")


def test_a_token_in_the_trackers_error_is_scrubbed_from_the_audit_result(tmp_path):
    w = world(tmp_path)
    token = "ghp_" + "b" * 36
    w.tracker.read = lambda key: Card(str(key), skipped=f"gh failed with {token}")

    result = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR)

    assert result["outcome"] == verbs.REFUSED
    assert token not in str(w.records.audit_rows[-1]["result"])
    assert token not in result["reason"]
