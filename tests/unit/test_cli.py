"""U21 test scenarios for the slipway CLI, through its entry point on fakes.

Every test calls cli.main with argv, the way the console script does, and a
Wiring over _service_fakes' world(): the fixture config under a temporary
SLIPWAY_HOME, in memory records, a fake engine, and a fake tracker. Nothing
reaches a network or a database.
"""

import json
import re
import tomllib
from datetime import timedelta
from pathlib import Path

import pytest

from _service_fakes import BUCKETS, MANAGER, OPERATOR, SLUG, ready_card, world
from slipway import cli
from slipway.onboard import machine
from slipway.page import app as page_app
from slipway.service import status
from test_imports import SRC, walk

ROOT = Path(__file__).resolve().parents[2]
HALT = {"halt": "commit_mismatch", "line": "commit mismatch: the merge is not the confirmed commit",
        "evidence": {"expected": "a" * 40, "found": "c" * 40}}
# A project with no file yet, so onboard starts its walk from the first question.
FRESH = "fresh"
TITLES = {title: name for name, title, _ in cli.GROUPS}


class StubProbe:
    """A probe with nothing to offer: enough for onboard's first question."""

    def builders(self):
        return ("cli",)

    def boards(self, draft):
        return ()

    def labels(self, draft):
        return ()

    def statuses(self, draft):
        return ()


def scripted(lines):
    feed = iter(lines)

    def read(prompt):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return read


class Terminal:
    """One world and the wiring the CLI runs on, with everything it printed."""

    def __init__(self, tmp_path, cards=(), lanes=None, lines=()):
        self.w = world(tmp_path, cards, lanes)
        self.out: list[str] = []
        self.sleeps: list[float] = []
        self.on_sleep = None
        self.wiring = cli.Wiring(
            root=self.w.root, service=lambda: self.w.ports,
            onboard=lambda slug: machine.Ports(
                root=self.w.root, operator=self.w.ports.operator, probe=StubProbe(),
                records=self.w.records, clock=self.w.clock),
            read=scripted(lines), write=self.out.append, sleep=self.sleep)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep is None:
            raise KeyboardInterrupt
        step, self.on_sleep = self.on_sleep, None
        step()

    def run(self, *argv) -> int:
        self.out.clear()
        return cli.main(list(argv), self.wiring)

    @property
    def text(self) -> str:
        return "\n".join(self.out)


def cli_groups(lines: list[str]) -> dict[str, list[str]]:
    """Each group's first words, one per row, from the CLI's status output."""
    groups, current = {}, None
    for line in lines:
        heading = re.fullmatch(r"(.+) \((\d+)\)", line)
        if heading and heading.group(1) in TITLES:
            current = TITLES[heading.group(1)]
            groups[current] = []
        elif current and line.startswith("  ") and not line.startswith("    "):
            first = line.split()[0]
            empty = next(e for n, _, e in cli.GROUPS if n == current)
            if line.strip() != empty:
                groups[current].append(first)
    return groups


def page_groups(html: str) -> dict[str, list[str]]:
    """Each group's keys, or each finding's class, from the page's HTML."""
    groups = {}
    for name, _, _ in cli.GROUPS:
        [fragment] = re.findall(rf'<ul class="rows bucket-{name}">(.*?)</ul>', html, re.S)
        if name == "findings":
            groups[name] = re.findall(r'<li class="finding"><span class="key">([^<]+)<',
                                      fragment)
        else:
            groups[name] = re.findall(r'data-key="([^"]+)"', fragment)
    return groups


# The card's test scenarios.

def test_status_prints_the_same_buckets_the_page_shows(tmp_path):
    t = Terminal(tmp_path)
    for n, state in enumerate(sorted(BUCKETS)):
        t.w.records.seed(f"S-{state}", state, pr_number=n + 1,
                         evidence=HALT if state == "halted" else {"reason": state})
    done = min(state for state, bucket in BUCKETS.items() if bucket == "done")
    landed = t.w.records.latest(t.w.records.project_id(SLUG), f"S-{done}")["build_id"]
    t.w.records.score(landed, "deterministic", {"verdict": "pass", "passed": 3, "of": 3})
    t.w.records.finding("runner_step", "fp-1", "the runner's step failed")

    code = t.run("status", SLUG)

    assert code == 0
    page = page_app.render_project(status.compose(t.w.ports, SLUG), "")
    expected = page_groups(page)
    assert cli_groups(t.out) == expected
    assert sorted(expected) == sorted(TITLES.values())
    assert all(expected[name] for name in ("needs_you", "being_built", "waiting_to_land",
                                           "landed", "findings")), expected
    assert t.out[0] == status.compose(t.w.ports, SLUG)["spoken"]
    assert HALT["line"] in t.text
    assert "https://github.com/example-org/example-app/pull/" in t.text
    assert "Deterministic: pass, 3 of 3. Judged: not scored yet." in t.text


def test_dispatch_on_an_unlabelled_card_prints_the_allowed_labels_and_exits_non_zero(tmp_path):
    t = Terminal(tmp_path, [ready_card("EX-3", labels=())])

    code = t.run("dispatch", SLUG, "EX-3")

    assert code != 0
    assert "Refused:" in t.out[0]
    assert "  Allowed labels: cloud" in t.out
    assert t.w.records.claims == [] and t.w.engine.starts == []


def test_land_override_without_a_reason_is_refused(tmp_path):
    t = Terminal(tmp_path)
    t.w.records.seed("EX-5", "halted", pr_number=5, evidence=HALT)

    code = t.run("land", SLUG, "EX-5", "--override")

    assert code != 0
    assert t.out[0] == "Refused: an override needs a reason"
    assert t.w.engine.pushes == []
    [row] = t.w.records.audit_rows
    assert (row["tool"], row["subject"], row["result"]["outcome"]) == ("land", OPERATOR,
                                                                        "refused")


def _dispatch(t):
    t.w.tracker.cards["EX-1"] = ready_card("EX-1")
    return ("dispatch", SLUG, "EX-1")


def _land(t):
    t.w.records.seed("EX-5", "halted", pr_number=5, evidence=HALT)
    return ("land", SLUG, "EX-5", "--override", "--reason", "the merge is the reviewed tree")


def _status_one(t):
    return ("status", SLUG)


def _status_all(t):
    return ("status",)


def _watch(t):
    t.w.records.seed("EX-1", "building")
    return ("watch", SLUG)


def _migrate(t):
    return ("migrate",)


def _onboard(t):
    return ("onboard", FRESH)


@pytest.mark.parametrize("arrange", [_dispatch, _land, _status_one, _status_all, _watch,
                                     _migrate, _onboard], ids=lambda f: f.__name__[1:])
def test_every_command_records_the_operators_subject_as_the_actor(tmp_path, arrange):
    t = Terminal(tmp_path)
    argv = arrange(t)

    t.run(*argv)

    subjects = {row["subject"] for row in t.w.records.audit_rows}
    actors = {payload["actor"] for _, _, payload in t.w.engine.pushes}
    claimed = {claim["actor"] for claim in t.w.records.claims}
    assert subjects | actors | claimed == {OPERATOR}, (subjects, actors, claimed)
    if argv[0] != "onboard" and t.w.records.audit_rows:
        # onboard's rows come from U19's machine, which names no client; a land
        # that pushes writes no row, its record is the run's transition.
        assert {row["client"] for row in t.w.records.audit_rows} == {cli.CLIENT}


# The verbs' other paths.

def test_dispatch_claims_a_ready_card_and_starts_its_run(tmp_path):
    t = Terminal(tmp_path, [ready_card("EX-1")])

    assert t.run("dispatch", SLUG, "EX-1") == 0
    assert t.out == ["Dispatched EX-1 as build 1.", "  Run run-1 started."]

    assert t.run("dispatch", SLUG, "EX-1") == 0
    assert t.out == ["Already owned: build 1 owns card EX-1 in claimed."]
    assert len(t.w.engine.starts) == 1


def test_land_with_a_reason_pushes_the_override_with_the_operators_subject(tmp_path):
    t = Terminal(tmp_path)
    t.w.records.seed("EX-5", "halted", pr_number=5, evidence=HALT)

    code = t.run("land", SLUG, "EX-5", "--override", "--reason", "checked by hand")

    assert code == 0
    assert t.out == ["Asked build 1 (halted) to land.",
                     "  https://github.com/example-org/example-app/pull/5"]
    [(_, build_id, payload)] = t.w.engine.pushes
    assert payload == {"actor": OPERATOR, "reason": "checked by hand", "override": True}


def test_land_on_a_card_with_no_build_exits_non_zero(tmp_path):
    t = Terminal(tmp_path)

    assert t.run("land", SLUG, "EX-9") == 1
    assert t.out == ["Not found: card EX-9 has no build."]


def test_status_with_no_project_prints_every_project(tmp_path):
    t = Terminal(tmp_path, lanes={"other": "laptop"})
    t.w.records.seed("EX-1", "building")
    t.w.records.seed("OT-1", "halted", project="other", pr_number=2, evidence=HALT)

    assert t.run("status") == 0
    assert t.out[0].startswith("1 being built, 1 needs you.")
    assert f"{SLUG}: 1 being built." in t.out
    assert "other: 1 needs you. OT-1 is halted: commit mismatch: the merge is not the " \
           "confirmed commit." in t.out
    assert "  Being built (1)" in t.out
    [row] = t.w.records.audit_rows
    assert row["project_slug"] == "*"


def test_status_for_a_subject_the_project_does_not_allow_is_refused(tmp_path):
    t = Terminal(tmp_path)
    from slipway.service import operate

    found = operate.status(t.w.ports, SLUG, "agent_stranger")
    assert found["outcome"] == "refused"
    assert operate.status(t.w.ports, SLUG, MANAGER)["project"] == SLUG


def test_watch_follows_new_events_until_ctrl_c(tmp_path):
    t = Terminal(tmp_path)
    t.w.records.seed("EX-1", "claimed")

    def later():
        t.w.later(timedelta(minutes=1))
        t.w.records.seed("EX-2", "building", evidence={"line": "the builder started"})

    t.on_sleep = later
    code = t.run("watch", SLUG, "--follow", "--every", "2")

    assert code == 0
    assert t.sleeps == [2.0, 2.0]
    assert [line.split("  ")[0] for line in t.out] == ["#1", "#2"]
    assert "EX-2  entered building: the builder started" in t.out[1]


def test_watch_since_starts_after_the_cursor(tmp_path):
    t = Terminal(tmp_path)
    for key in ("EX-1", "EX-2", "EX-3"):
        t.w.records.seed(key, "building")

    assert t.run("watch", SLUG, "--since", "2") == 0
    assert [line.split("  ")[0] for line in t.out] == ["#3"]


def test_migrate_prints_the_versions_applied(tmp_path):
    t = Terminal(tmp_path)
    t.w.records.pending_migrations = ("004", "005")

    assert t.run("migrate", "--database", "slipway_test") == 0
    assert t.out == ["Applied: 004, 005"]
    assert t.w.records.migrations == ["slipway_test"]
    assert t.run("migrate") == 0
    assert t.w.records.migrations[-1] == "slipway"


def test_json_prints_the_service_result_and_keeps_the_exit_code(tmp_path):
    t = Terminal(tmp_path, [ready_card("EX-3", labels=())])

    code = t.run("--json", "dispatch", SLUG, "EX-3")

    assert code == 1
    result = json.loads(t.text)
    assert result["outcome"] == "refused" and result["allowed_labels"] == ["cloud"]


def test_onboard_asks_the_machines_first_question_and_stops_at_end_of_input(tmp_path):
    t = Terminal(tmp_path)

    code = t.run("onboard", FRESH)

    assert code == 1
    assert t.out[-1] == "Stopped. Run onboard again to pick up where you left off."
    assert t.w.records.audit_rows[0]["tool"] == "onboard_start"


def test_an_unknown_project_is_a_usage_error_not_a_traceback(tmp_path, capsys):
    t = Terminal(tmp_path)

    assert t.run("status", "nope") == 2
    assert "slipway status:" in capsys.readouterr().err


def test_no_database_named_is_a_usage_error(tmp_path, monkeypatch, capsys):
    t = Terminal(tmp_path)
    monkeypatch.setenv("SLIPWAY_HOME", str(t.w.root))
    monkeypatch.delenv(cli.DSN_VARIABLE, raising=False)

    assert cli.main(["status", SLUG]) == 2
    assert f"set {cli.DSN_VARIABLE}" in capsys.readouterr().err


def test_the_console_script_is_the_entry_point_these_tests_call():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["slipway"] == "slipway.cli:main"


def test_the_cli_imports_no_mcp_or_workflow_module():
    reached = {edge.imported for edge in walk(SRC) if edge.importer == "slipway.cli"}
    assert reached
    assert not [m for m in reached if m.split(".")[1] in ("mcp", "workflow", "records")]
