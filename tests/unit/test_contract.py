"""U8 test scenarios for the card contract: the phactory block and the ready rule."""

from dataclasses import dataclass

import pytest

from phactory.cards.contract import FIELDS, parse, ready


@dataclass(frozen=True)
class FakeProject:
    """The four project fields the ready rule reads, with invented values."""

    lane_label: str = "cloud"
    ready_status: str = "Ready"
    landed_status: str = "Done"


@dataclass(frozen=True)
class FakeCard:
    key: str = "12"
    body: str = ""
    status: str | None = "Ready"
    labels: tuple[str, ...] = ("cloud",)


def block(text: str) -> str:
    return f"Some prose first.\n\n```phactory\n{text}\n```\n\nSome prose after.\n"


COMPLETE = block(
    '{"good": "The page renders.", "paths": ["src/page.py", "tests/test_page.py"], '
    '"test": "tests/test_page.py", "lane": "cloud"}'
)
PROJECT = FakeProject()


# parse.

def test_a_complete_block_parses_all_three_fields():
    parsed = parse(COMPLETE)
    assert parsed.ok
    assert parsed.good == "The page renders."
    assert parsed.paths == ("src/page.py", "tests/test_page.py")
    assert parsed.test == "tests/test_page.py"
    assert parsed.lane == "cloud"
    assert parsed.absent == ()


def test_a_body_with_no_block_yields_three_absences_and_still_parses():
    parsed = parse("Just a description, no block at all.")
    assert parsed.ok
    assert parsed.absent == FIELDS == ("good", "paths", "test")
    assert (parsed.good, parsed.paths, parsed.test) == (None, (), None)


def test_an_empty_body_parses_as_all_absent():
    assert parse(None).absent == FIELDS
    assert parse("").ok


def test_the_template_block_with_empty_values_counts_as_absent():
    parsed = parse(block('{"good": "", "paths": [], "test": "", "lane": "cloud"}'))
    assert parsed.ok
    assert parsed.absent == FIELDS
    assert parsed.lane == "cloud"


def test_one_missing_field_is_named_alone():
    parsed = parse(block('{"good": "It works.", "paths": ["a.py"]}'))
    assert parsed.absent == ("test",)


@pytest.mark.parametrize("text, words", [
    ("{not json", "not valid JSON"),
    ('["a list"]', "JSON object"),
    ('{"paths": "a.py"}', "paths must be a list"),
    ('{"good": 3}', "good must be a string"),
])
def test_a_malformed_block_is_an_error_not_an_absence(text, words):
    parsed = parse(block(text))
    assert not parsed.ok
    assert words in parsed.error
    assert parsed.absent == ()


def test_two_blocks_are_an_error():
    parsed = parse(COMPLETE + COMPLETE)
    assert not parsed.ok
    assert "2 phactory blocks" in parsed.error


def test_a_block_in_another_language_is_not_read():
    assert parse("```json\n{\"good\": \"x\"}\n```").absent == FIELDS


# ready.

def test_a_card_in_ready_status_with_the_lane_label_and_a_block_is_ready():
    answer = ready(FakeCard(body=COMPLETE), PROJECT)
    assert answer.ready
    assert answer.reason is None
    assert answer.block.good == "The page renders."


def test_a_ready_card_with_absent_fields_is_ready_and_records_them():
    answer = ready(FakeCard(body="No block."), PROJECT)
    assert answer.ready
    assert answer.block.absent == FIELDS


def test_a_card_with_two_lane_labels_is_not_ready_with_the_reason():
    card = FakeCard(body=COMPLETE, labels=("cloud", "operator", "docs"))
    answer = ready(card, PROJECT, other_lanes=("operator",))
    assert not answer.ready
    assert answer.reason == "card 12 carries 2 lane labels (cloud, operator); keep one"


def test_a_card_in_a_done_status_with_a_lane_label_is_not_ready_and_silent():
    answer = ready(FakeCard(body=COMPLETE, status="Done"), PROJECT)
    assert not answer.ready
    assert answer.reason is None


def test_a_card_with_no_status_is_not_ready_and_silent():
    answer = ready(FakeCard(status=None), PROJECT)
    assert (answer.ready, answer.reason) == (False, None)


def test_status_and_label_compare_without_case():
    answer = ready(FakeCard(body=COMPLETE, status="ready", labels=("Cloud",)), PROJECT)
    assert answer.ready


def test_a_ready_card_with_no_lane_label_is_not_ready_with_the_reason():
    answer = ready(FakeCard(labels=("docs",)), PROJECT)
    assert not answer.ready
    assert answer.reason == "card 12 carries no lane label"


def test_a_ready_card_in_another_lane_is_not_ready_and_silent():
    answer = ready(FakeCard(labels=("operator",)), PROJECT, other_lanes=("operator",))
    assert (answer.ready, answer.reason) == (False, None)


def test_a_ready_card_with_a_malformed_block_is_not_ready_with_the_reason():
    answer = ready(FakeCard(body=block("{oops")), PROJECT)
    assert not answer.ready
    assert answer.reason.startswith("card 12: the phactory block is not valid JSON")


def test_a_block_naming_another_lane_is_not_ready_with_the_reason():
    body = block('{"good": "x", "lane": "operator"}')
    answer = ready(FakeCard(body=body), PROJECT)
    assert not answer.ready
    assert "names lane operator" in answer.reason


def test_a_block_with_crlf_line_endings_parses():
    """GitHub's web editor returns bodies with CRLF; the closing fence must still close."""
    parsed = parse(COMPLETE.replace("\n", "\r\n"))
    assert parsed.ok and parsed.absent == ()
    assert parsed.paths == ("src/page.py", "tests/test_page.py")


def test_a_fence_indented_up_to_three_spaces_parses():
    """CommonMark allows a fence behind one to three spaces."""
    indented = COMPLETE.replace("```phactory", "  ```phactory").replace("\n```\n", "\n  ```\n")
    parsed = parse(indented)
    assert parsed.ok and parsed.absent == ()
