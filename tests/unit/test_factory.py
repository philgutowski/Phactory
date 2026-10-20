"""The factory: the ready rule, the handoff, and the review check's pure parts.

Inputs are small hand written issue, pull request, and branch records in the
shape the GitHub REST API returns, so nothing here reaches git, gh, or a
model. The two scripts' network and subprocess parts are exercised by the
workflow itself.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from factory import handoff, review

ROOT = Path(__file__).resolve().parents[2]
# The two dash characters, as code points so this file holds neither.
EM, EN = chr(0x2014), chr(0x2013)


def block(good="done", paths=(), test="tests/unit/test_x.py", lane="cloud") -> str:
    return "```slipway\n" + json.dumps(
        {"good": good, "paths": list(paths), "test": test, "lane": lane}) + "\n```\n"


def issue(number, title, *, state="open", labels=("cloud",), deps="none", paths=(),
          body_extra="", block_text=None):
    body = (f"- **Goal:** build it\n- **Dependencies:** {deps}.\n- **Files:** x\n\n"
            + (block_text if block_text is not None else block(paths=paths)) + body_extra)
    return {"number": number, "title": title, "state": state,
            "labels": [{"name": name} for name in labels], "body": body}


def pull(number, head, body):
    return {"number": number, "head": {"ref": head}, "body": body}


BOARD = [
    issue(8, "U5: Repository scaffold", state="closed"),
    issue(9, "U6: Configuration", state="closed"),
    issue(5, "U2: Spike: merge paths", labels=("operator",)),
    issue(12, "U9: Host adapters and webhooks", deps="U2, U5, U6",
          paths=("src/slipway/hosts/base.py", "tests/fixtures/host/*.json")),
    issue(17, "U14: Card workflow on Hatchet", deps="U4, U7, U9, U10, U11",
          paths=("src/slipway/workflow/card.py",)),
    issue(41, "U7 follow up: polled host events", paths=("src/slipway/records/schema/",
                                                          "src/slipway/records/store.py")),
    issue(42, "U7 follow up: forward only migrations",
          paths=("src/slipway/records/migrate.py", "src/slipway/records/schema/")),
    issue(39, "Decide where the app reads the operator subject", paths=("deploy/README.md",)),
    issue(2, "feat: the plan", labels=()),
]


# The card reader.

def test_unit_number_and_follow_up():
    assert handoff.unit_number("U9: Host adapters") == 9
    assert handoff.unit_number("U13 follow up: the thread") == 13
    assert handoff.unit_number("Decide where the app reads") is None
    assert handoff.read_card(BOARD[3]).is_unit
    assert not handoff.read_card(BOARD[5]).is_unit


def test_dependencies_are_read_from_the_body_line():
    assert handoff.dependencies("- **Dependencies:** U2, U5, U6.\n") == (2, 5, 6)
    assert handoff.dependencies("- **Dependencies:** none.\n") == ()
    assert handoff.dependencies("- **Dependencies:** U2 step 4 (the ruleset).") == (2,)
    assert handoff.dependencies("no such line") == ()


def test_branch_name_carries_the_unit_token_or_the_issue_number():
    assert handoff.branch_name(handoff.read_card(BOARD[3])) == "slipway/u9-host-adapters-webhooks"
    assert handoff.branch_name(handoff.read_card(BOARD[5])) == "slipway/u7-polled-host-events"
    assert handoff.branch_name(handoff.read_card(BOARD[7])).startswith("slipway/issue-39-")


def test_pull_requests_listed_as_issues_are_not_cards():
    cards = handoff.cards_of(BOARD + [{**issue(70, "a pr"), "pull_request": {}}])
    assert [c.number for c in cards] == [c["number"] for c in BOARD]


# The ready rule.

def test_overlaps_handles_equal_directory_and_glob_paths():
    assert handoff.overlaps("a/b.py", "a/b.py")
    assert handoff.overlaps("src/slipway/records/schema/", "src/slipway/records/schema/002.sql")
    assert handoff.overlaps("tests/fixtures/host/*.json", "tests/fixtures/host/pr_closed.json")
    assert not handoff.overlaps("a/b.py", "a/c.py")
    assert not handoff.overlaps("src/slipway/records/", "src/slipway/recordsx.py")


def test_ready_names_each_reason():
    verdicts = {v.card.number: v for v in handoff.ready(BOARD, [], [])}
    assert verdicts[12].reasons == ("waits for U2",)
    assert verdicts[17].reasons == ("waits for U4, U7, U9, U10, U11",)
    assert verdicts[41].ready and verdicts[42].ready and verdicts[39].ready
    assert 5 not in verdicts and 2 not in verdicts and 8 not in verdicts


def test_assume_done_stands_in_for_a_recorded_spike():
    verdicts = {v.card.number: v for v in handoff.ready(BOARD, [], [], assume_done=(2,))}
    assert verdicts[12].ready


def test_a_branch_marks_a_card_dispatched_and_holds_its_files():
    branches = [{"name": "slipway/u7-polled-host-events"}]
    verdicts = {v.card.number: v for v in handoff.ready(BOARD, [], branches)}
    assert verdicts[41].reasons == ("dispatched on slipway/u7-polled-host-events",)
    assert verdicts[42].reasons == (
        "declares files in flight: src/slipway/records/schema/",)
    assert verdicts[42].in_flight == ("src/slipway/records/schema/",
                                      "src/slipway/records/store.py")


def test_an_open_pull_request_marks_a_card_dispatched_by_its_closes_line():
    pulls = [pull(70, "slipway/u7-anything", "Closes #42\nBase: abc")]
    verdicts = {v.card.number: v for v in handoff.ready(BOARD, pulls, [])}
    assert verdicts[42].reasons == ("dispatched on slipway/u7-anything",)
    assert not verdicts[41].ready


def test_a_pull_request_off_a_slipway_branch_does_not_count():
    pulls = [pull(70, "claude/some-branch", "Closes #42")]
    assert {v.card.number: v for v in handoff.ready(BOARD, pulls, [])}[42].ready


def test_a_bad_block_is_not_ready_with_the_parser_reason():
    board = BOARD + [issue(90, "U99: bad", block_text="```slipway\n{not json}\n```\n")]
    verdict = {v.card.number: v for v in handoff.ready(board, [], [])}[90]
    assert verdict.reasons[0].startswith("slipway block: the slipway block is not valid JSON")


def test_ready_cards_come_first_then_by_number():
    numbers = [v.card.number for v in handoff.ready(BOARD, [], [])]
    assert numbers == [39, 41, 42, 12, 17]


# The handoff.

def test_handoff_carries_the_five_items_in_order_then_the_card():
    card = handoff.read_card(BOARD[3])
    text = handoff.render(card, "0123abcd", "slipway/u9-host-adapters-webhooks",
                          ("src/slipway/records/store.py",), "owner/repo")
    order = ["1. Base: 0123abcd", "2. Branch: slipway/u9-host-adapters-webhooks", "3. Rules:",
             "4. Files other dispatched cards declare", "* src/slipway/records/store.py",
             "5. Done:", "Closes #12", "The card:", "```slipway"]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions)
    assert "CLAUDE.md" in text and "docs/explainers/" in text
    assert EM not in text and EN not in text


def test_handoff_refuses_a_missing_base():
    with pytest.raises(ValueError):
        handoff.render(handoff.read_card(BOARD[3]), "", "slipway/u9-x", (), "owner/repo")


def test_command_ready_and_render(tmp_path):
    for name, data in (("issues", BOARD), ("pulls", []), ("branches", [])):
        (tmp_path / f"{name}.json").write_text(json.dumps(data))
    args = ["--issues", str(tmp_path / "issues.json"), "--pulls", str(tmp_path / "pulls.json"),
            "--branches", str(tmp_path / "branches.json")]
    out = subprocess.run([sys.executable, "-m", "factory.handoff", "ready", *args],
                         capture_output=True, text=True, cwd=ROOT, check=True).stdout
    ready = {row["number"]: row for row in json.loads(out)}
    assert ready[12]["ready"] is False and ready[41]["ready"] is True

    render = subprocess.run(
        [sys.executable, "-m", "factory.handoff", "render", "--card", "12", "--base", "abc",
         "--repository", "o/r", *args], capture_output=True, text=True, cwd=ROOT)
    assert render.returncode == 1 and "waits for U2" in render.stderr

    render = subprocess.run(
        [sys.executable, "-m", "factory.handoff", "render", "--card", "12", "--base", "abc",
         "--repository", "o/r", "--assume-done", "U2", *args],
        capture_output=True, text=True, cwd=ROOT, check=True)
    assert render.stdout.startswith("Slipway handoff for card #12: U9: Host adapters")


def test_paginated_input_is_flattened(tmp_path):
    (tmp_path / "issues.json").write_text(json.dumps([BOARD[:4], BOARD[4:]]))
    for name in ("pulls", "branches"):
        (tmp_path / f"{name}.json").write_text("[]")
    assert len(handoff._load(str(tmp_path / "issues.json"))) == len(BOARD)


# The review check's facts.

DIFF = """\
diff --git a/src/slipway/hosts/base.py b/src/slipway/hosts/base.py
--- a/src/slipway/hosts/base.py
+++ b/src/slipway/hosts/base.py
@@ -1,2 +1,4 @@
 import json
+# the host EMDASH any host
+parser.add_argument("--format", default="json")
+x = 1  # the gate -- which is ci
-old
""".replace("EMDASH", EM)


def test_dash_findings_split_dashes_from_double_hyphens():
    dashes, doubles = review.dash_findings(DIFF)
    assert dashes == (f"src/slipway/hosts/base.py: em dash: # the host {EM} any host",)
    assert doubles == ('src/slipway/hosts/base.py: x = 1  # the gate -- which is ci',)


def test_facts_name_outside_paths_clashes_and_closes():
    facts = review.facts_from(
        DIFF, "src/slipway/hosts/base.py\ndocs/explainers/u9.md\n",
        card_paths=("src/slipway/hosts/base.py",), other_paths=("docs/explainers/u9.md",),
        commit_messages=(f"U9: hosts\n\nBody {EN} with dash",), pr_body="Closes #12\nBase: abc")
    assert facts.outside_paths == ("docs/explainers/u9.md",)
    assert facts.in_flight_clashes == ("docs/explainers/u9.md",)
    assert facts.closes == (12,)
    assert any(d.startswith("commit 1: en dash") for d in facts.dashes)


def test_prompt_holds_brief_card_facts_and_diff_in_order():
    facts = review.facts_from(DIFF, "src/slipway/hosts/base.py\n", (), (), ("m",), "")
    text = review.prompt_for("BRIEF", "U9: hosts", block(good="all good"), facts, DIFF)
    order = ["BRIEF", "## The card", "U9: hosts", "## Facts",
             "Definition of done (good): all good", "Issues the pull request body closes: none",
             "## The diff", "```diff"]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions)
    long = review.prompt_for("BRIEF", "t", "", facts, DIFF, diff_path="/tmp/d.patch")
    assert "Read it from /tmp/d.patch" in long and "```diff" not in long


# The verdict and the review body.

def cli(structured=None, **extra) -> str:
    data = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 1.5,
            "result": "", "structured_output": structured, **extra}
    return json.dumps(data)


def test_verdict_from_structured_output():
    verdict = review.verdict_from(cli({
        "verdict": "block", "summary": "Nearly.",
        "findings": [{"severity": "blocking", "file": "a.py", "line": 3, "title": "t",
                      "detail": "d"},
                     {"severity": "advisory", "file": "b.py", "line": 0, "title": "n",
                      "detail": ""}]}))
    assert verdict.blocked and verdict.cost_usd == 1.5
    assert [f.severity for f in verdict.findings] == ["blocking", "advisory"]


def test_a_review_that_could_not_run_is_a_block():
    assert review.verdict_from("not json").blocked
    assert review.verdict_from(cli(None)).blocked
    failed = review.verdict_from(cli(None, is_error=True, subtype="error_during_execution",
                                     result="rate limited"))
    assert failed.blocked and "rate limited" in failed.summary
    passed = review.verdict_from(cli({"verdict": "pass", "summary": "ok", "findings": []}))
    assert not passed.blocked


def test_a_blocking_finding_blocks_even_when_the_verdict_says_pass():
    verdict = review.verdict_from(cli({
        "verdict": "pass", "summary": "ok",
        "findings": [{"severity": "blocking", "file": "a", "line": 1, "title": "t",
                      "detail": "d"}]}))
    assert verdict.blocked


def test_review_body_lists_blocking_then_advisory():
    verdict = review.Verdict("block", "Summary.", (
        review.Finding("advisory", "b.py", 0, "Name it", "because"),
        review.Finding("blocking", "a.py", 3, "Wrong result", "for empty input"),
    ), cost_usd=0.42)
    body = review.review_body(verdict, "claude-fable-5-1", "https://run")
    assert body.startswith("## Review check: Blocked")
    assert body.index("### Blocking") < body.index("`a.py:3`") < body.index("### Advisory")
    assert "`b.py` Name it" in body and "$0.42" in body and "[log](https://run)" in body
    assert EM not in body and EN not in body
    assert review.review_body(review.Verdict("pass", "Fine.", ()), "m").startswith(
        "## Review check: Passed")


# The files that ship.

def test_workflow_names_the_script_and_keeps_the_two_step_shape():
    text = (ROOT / ".github/workflows/review.yml").read_text()
    assert "python3 factory/review.py" in text
    assert text.count("- uses:") == 1 and text.count("- name:") == 1
    assert "pull-requests: write" in text
    assert "secrets.ANTHROPIC_API_KEY" in text


def test_brief_names_every_blocking_rule_and_the_schema_fields():
    text = (ROOT / "factory/review-brief.md").read_text()
    for rule in ("R1", "KTD18", "network", "credential", "dashes", "explainer", "Closes #"):
        assert rule in text, rule
    for key in review.SCHEMA["properties"]:
        assert f"`{key}`" in text, key


def test_factory_prose_carries_no_dash_punctuation():
    files = [*(ROOT / "factory").glob("*"), ROOT / ".github/workflows/review.yml",
             ROOT / ".claude/skills/dispatch/SKILL.md", ROOT / "docs/explainers/build-loop.md"]
    for path in files:
        if path.is_file():
            text = path.read_text()
            assert EM not in text and EN not in text, path
            assert not re.search(r"\w -- \w", text), path
