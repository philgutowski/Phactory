"""The closed halt set (KTD11): seven classes, each with its kind and evidence."""

import pytest

from phactory import halts
from phactory.halts import Halt, HaltError, Kind, halt
from phactory.trackers import base

SEVEN = {
    "merged_card_not_moved", "commit_mismatch", "tracker_unreadable", "git_unreadable",
    "builder_failed", "builder_expired", "binding_invalid",
}

# One halt per class, with the evidence its needs you line requires. Verify
# produces the first, second, and fourth in test_verify.py; the build outcomes
# and tracker_unreadable are produced by their callers, so they are built here.
EXAMPLES = {
    "merged_card_not_moved": dict(card_key="13", merged="abc", tracker_status="In progress"),
    "commit_mismatch": dict(landing_path="merge_queue", relation="merged_equals_tested",
                            expected="aaa", found="bbb"),
    "tracker_unreadable": dict(card_key="13", reason="GitHub GraphQL budget exhausted"),
    "git_unreadable": dict(reason="git fetch exited 128"),
    "builder_failed": dict(reason="provider error at start"),
    "builder_expired": dict(budget_seconds=7200),
    "binding_invalid": dict(role="builder", reason="no account for the role"),
}


def test_the_set_is_exactly_seven():
    assert set(halts.CLASSES) == SEVEN
    assert set(EXAMPLES) == SEVEN


def test_a_class_outside_the_set_is_refused_at_construction():
    with pytest.raises(HaltError, match="closed"):
        Halt("gate_red", {})
    with pytest.raises(HaltError):
        halt("runner_regressed", reason="x")


@pytest.mark.parametrize("name", sorted(SEVEN))
def test_every_class_is_produced_and_renders_its_line(name):
    found = halt(name, **EXAMPLES[name])
    assert found.name == name
    line = found.line()
    for value in EXAMPLES[name].values():
        assert str(value) in line


@pytest.mark.parametrize("name", sorted(SEVEN))
def test_missing_evidence_is_refused(name):
    evidence = dict(EXAMPLES[name])
    dropped = halts.CLASSES[name].evidence_keys[0]
    del evidence[dropped]
    with pytest.raises(HaltError, match=dropped):
        halt(name, **evidence)


def test_builder_expired_is_a_build_outcome_and_commit_mismatch_a_verify_halt():
    expired = halt("builder_expired", **EXAMPLES["builder_expired"])
    assert expired.kind is Kind.BUILD_OUTCOME and not expired.is_wait
    mismatch = halt("commit_mismatch", **EXAMPLES["commit_mismatch"])
    assert mismatch.kind is Kind.VERIFY_HALT and mismatch.is_wait


def test_kinds_split_as_ktd11_says():
    waits = {n for n, c in halts.CLASSES.items() if c.is_wait}
    assert waits == {"merged_card_not_moved", "commit_mismatch", "tracker_unreadable",
                     "git_unreadable"}


def test_the_tracker_constant_names_a_member():
    assert base.TRACKER_UNREADABLE == halts.TRACKER_UNREADABLE
    assert base.skipped("down").halt_class in halts.CLASSES
