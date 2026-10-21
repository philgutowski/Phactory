"""U5 test scenarios: the network guard, the repository helper, the gate flag,
the fakes, and the board workflow's issue lookup.

The import rule scenario lives in tests/test_imports.py beside the rule.
"""

import json
import os
import re
import stat
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path

import pytest

from _fakes import FakeRun, UnscriptedCall
from _nonet import NetworkReached
from _repo import git, make_repo

ROOT = Path(__file__).resolve().parents[2]


def test_package_imports():
    import slipway

    assert slipway.__version__


# The network guard.

def test_urllib_socket_is_refused():
    with pytest.raises(NetworkReached):
        urllib.request.urlopen("https://example.com", timeout=1)


def test_raw_socket_to_remote_host_is_refused():
    import socket

    with pytest.raises(NetworkReached):
        socket.create_connection(("192.0.2.1", 443), timeout=1)


@pytest.mark.parametrize("program", ["gh", "curl", "wget", "ssh", "/usr/bin/gh"])
def test_network_program_is_refused(program):
    with pytest.raises(NetworkReached):
        subprocess.run([program, "version"], check=False)


def test_git_with_a_network_url_is_refused():
    with pytest.raises(NetworkReached):
        subprocess.run(["git", "ls-remote", "https://github.com/example/repo"], check=False)


def test_guard_cannot_be_swallowed_by_except_exception():
    with pytest.raises(NetworkReached):
        try:
            urllib.request.urlopen("http://example.com")
        except Exception:
            pass


# The repository helper.

def test_make_repo_has_bare_origin_and_origin_head(tmp_path):
    repo = make_repo(tmp_path)
    assert git(repo.origin, "rev-parse", "--is-bare-repository") == "true"
    assert repo.git("symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/main"
    assert repo.git("rev-parse", "origin/HEAD") == repo.git("rev-parse", "HEAD")


def test_repo_commit_returns_the_new_head(tmp_path):
    repo = make_repo(tmp_path)
    head = repo.commit("a.txt", "a\n", "add a")
    assert head == repo.git("rev-parse", "HEAD")
    assert repo.git("log", "-1", "--format=%s") == "add a"


# The integration gate.

_SKIPPING_SUITE = """
import pytest

def test_needs_database():
    pytest.skip("SLIPWAY_TEST_DSN is not set")
"""


_SMOKE_AND_DATABASE = """
import pytest

@pytest.mark.operator_smoke
def test_needs_an_engine():
    pytest.skip("SLIPWAY_TEST_HATCHET is not set")

def test_needs_database():
    pytest.skip("SLIPWAY_TEST_DSN is not set")
"""


def _run_inner(pytester, env_ci: str | None, suite: str = _SKIPPING_SUITE):
    pytester.mkdir("integration")
    pytester.path.joinpath("integration", "test_db.py").write_text(suite)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "tests")}
    env.pop("CI", None)
    if env_ci is not None:
        env["CI"] = env_ci
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "_gate", "-p", "no:cacheprovider"],
        cwd=pytester.path, env=env, capture_output=True, text=True,
    )


def test_skipped_integration_test_fails_when_ci_is_set(pytester):
    result = _run_inner(pytester, "true")
    assert result.returncode == 1, result.stdout
    assert "1 failed" in result.stdout
    assert "integration suite is required" in result.stdout


def test_skipped_integration_test_only_skips_locally(pytester):
    result = _run_inner(pytester, None)
    assert result.returncode == 0, result.stdout
    assert "1 skipped" in result.stdout


def test_an_operator_smoke_skip_stays_a_skip_under_ci_and_nothing_else_does(pytester):
    result = _run_inner(pytester, "true", _SMOKE_AND_DATABASE)
    assert result.returncode == 1, result.stdout
    assert "1 failed, 1 skipped" in result.stdout


# The fakes.

def test_fake_run_matches_longest_prefix_and_records_calls():
    run = FakeRun()
    run.reply(["gh", "issue"], stdout="short")
    run.reply(["gh", "issue", "view"], stdout="long")
    assert run(["gh", "issue", "view", "8"]).stdout == "long"
    assert run.calls == [["gh", "issue", "view", "8"]]
    with pytest.raises(UnscriptedCall):
        run(["gh", "pr", "list"])


# The board workflow (AE13). The live half, a real branch moving a real
# board item, is observed on the board when this card's branch is pushed with
# the secret set. Here the workflow's own shell script runs against a fake gh.

TEMPLATE = ROOT / "templates" / "slipway-board.yml"
OWN_COPY = ROOT / ".github" / "workflows" / "slipway-board.yml"


def _run_block(workflow: Path) -> str:
    lines = workflow.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "run: |")
    indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    body = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


_FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["issue", "list"]:
    print(json.dumps([{"number": 80, "title": "U50: other"},
                      {"number": 8, "title": "U5: scaffold"}]))
elif args[:2] == ["project", "view"]:
    print(json.dumps({"id": "PVT_1"}))
elif args[:2] == ["project", "item-add"]:
    print(json.dumps({"id": "ITEM_1"}))
elif args[:2] == ["project", "field-list"]:
    print(json.dumps({"fields": [{"name": "Status", "id": "FIELD_1", "options": [
        {"id": "OPT_P", "name": "In progress"}, {"id": "OPT_R", "name": "In review"},
        {"id": "OPT_D", "name": "Done"}]}]}))
elif args[:2] == ["project", "item-edit"]:
    pass
else:
    sys.exit("unexpected gh call: %r" % args)
"""


def _board(tmp_path, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "gh.log"
    log.touch()
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_LOG": str(log),
        "GH_TOKEN": "token", "BOARD_OWNER": "owner", "BOARD_NUMBER": "11",
        "STATUS_FIELD": "Status", "IN_PROGRESS": "In progress", "IN_REVIEW": "In review",
        "REPO": "owner/repo", "PR_BODY": "", "BRANCH": "slipway/u5-scaffold",
        "EVENT_NAME": "create",
    }
    result = subprocess.run(
        ["bash", "-c", _run_block(TEMPLATE)], env={**base, **env},
        capture_output=True, text=True,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def _edit(calls):
    edits = [c for c in calls if c[:2] == ["project", "item-edit"]]
    assert len(edits) == 1, calls
    return edits[0]


needs_jq = pytest.mark.skipif(
    subprocess.run(["bash", "-c", "command -v jq"], capture_output=True).returncode != 0,
    reason="jq is not installed",
)


def test_own_copy_matches_the_template():
    assert OWN_COPY.read_text() == TEMPLATE.read_text()


@needs_jq
def test_branch_created_moves_card_to_in_progress(tmp_path):
    result, calls = _board(tmp_path)
    assert result.returncode == 0, result.stderr
    added = next(c for c in calls if c[:2] == ["project", "item-add"])
    assert "https://github.com/owner/repo/issues/8" in added
    assert "OPT_P" in _edit(calls)


@needs_jq
def test_pull_request_opened_moves_card_to_in_review_by_closing_keyword(tmp_path):
    result, calls = _board(
        tmp_path, EVENT_NAME="pull_request", PR_BODY="Body.\n\nCloses #42\n",
    )
    assert result.returncode == 0, result.stderr
    assert not any(c[:2] == ["issue", "list"] for c in calls)
    added = next(c for c in calls if c[:2] == ["project", "item-add"])
    assert "https://github.com/owner/repo/issues/42" in added
    assert "OPT_R" in _edit(calls)


@needs_jq
def test_pull_request_without_keyword_falls_back_to_branch(tmp_path):
    result, calls = _board(tmp_path, EVENT_NAME="pull_request", PR_BODY="no link")
    assert result.returncode == 0, result.stderr
    assert "OPT_R" in _edit(calls)


def test_missing_secret_is_a_notice_not_a_failure(tmp_path):
    result, calls = _board(tmp_path, GH_TOKEN="")
    assert result.returncode == 0
    assert "SLIPWAY_BOARD_TOKEN is not set" in result.stdout
    assert calls == []


def test_unknown_branch_is_a_notice_not_a_failure(tmp_path):
    result, calls = _board(tmp_path, BRANCH="slipway/no-card")
    assert result.returncode == 0
    assert "no issue found" in result.stdout
    assert calls == []


# Writing rules from CLAUDE.md that a test can hold.

_OWNED = ["CLAUDE.md", "CODEOWNERS", "pyproject.toml", ".github", "templates", "src",
          "tests", "docs/explainers"]


# Built from code points so this file does not contain the characters it bans.
DASHES = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")


def test_no_em_or_en_dashes_in_files_this_scaffold_owns():
    offenders = []
    for name in _OWNED:
        target = ROOT / name
        paths = [target] if target.is_file() else sorted(target.rglob("*"))
        for p in paths:
            if p.is_file() and "__pycache__" not in p.parts:
                text = p.read_text(errors="ignore")
                if DASHES.search(text):
                    offenders.append(str(p.relative_to(ROOT)))
    assert offenders == []
