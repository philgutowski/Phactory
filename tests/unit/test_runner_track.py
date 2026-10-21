"""U22 test scenarios for the hourly runner tracking cron, through fakes."""

import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from _runner_fakes import REPOSITORY, A, B, C, instance, world
from slipway.runner import instances, rollback
from slipway.workflow import runner_track


def deployed(w, commit=A):
    """An instance already running commit, an hour before the test begins."""
    w.head = commit
    out = runner_track.reconcile(w.ports)
    assert {o["outcome"] for o in out["instances"].values()} == {runner_track.SWAPPED}
    w.later()
    w.scripts.calls.clear()


def test_ae11_b_is_rolled_on_then_back_off_for_a_runner_regression_only():
    w = world()
    deployed(w, A)

    w.head = B
    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"]["outcome"] == runner_track.SWAPPED
    assert out["instances"]["vps"]["previous"] == A
    active = w.records.active_build("vps")
    assert active.runner_commit == B
    assert w.records.previous_build(active).runner_commit == A
    assert [c[0] for c in w.scripts.calls] == ["build", "test", "swap"]

    # The project's last gate on A was clean.
    w.records.gate_run(1, "vps", active.activated_at - timedelta(minutes=30))
    # A project red on B: a failed test, no runner class finding. B stays.
    red = w.records.gate_run(1, "vps", w.later(timedelta(minutes=5)))
    assert rollback.evaluate(w.ports, red)["outcome"] == rollback.NO_RUNNER_FINDING
    assert w.records.active_build("vps").runner_commit == B

    # An unsupported step on B: A is restored and B rejected.
    bad = w.records.gate_run(1, "vps", w.later(timedelta(minutes=5)), rollback.UNSUPPORTED_STEP)
    result = rollback.evaluate(w.ports, bad)

    assert result["outcome"] == rollback.ROLLED_BACK
    assert w.records.active_build("vps").runner_commit == A
    assert w.records.rejected(B)
    assert w.scripts.named("rollback") == [("rollback", "vps", "vps", A)]
    regression = [f for f in w.records.findings if f["class"] == instances.REGRESSION]
    assert len(regression) == 1
    assert regression[0]["runner_commit"] == B and regression[0]["gate_run_id"] == bad["id"]


def test_a_rejected_commit_is_not_redeployed_and_a_newer_head_is():
    w = world()
    deployed(w, A)
    w.head = B
    runner_track.reconcile(w.ports)
    b_build = w.records.active_build("vps")
    w.records.gate_run(1, "vps", b_build.activated_at - timedelta(minutes=1))
    bad = w.records.gate_run(1, "vps", w.later(timedelta(minutes=1)), rollback.LOG_GAP)
    assert rollback.evaluate(w.ports, bad)["outcome"] == rollback.ROLLED_BACK
    w.scripts.calls.clear()

    w.later()
    skipped = runner_track.reconcile(w.ports)

    assert skipped["skipped"] == "rejected"
    assert w.scripts.calls == []
    assert w.records.active_build("vps").runner_commit == A

    w.head = C
    w.later()
    newer = runner_track.reconcile(w.ports)

    assert newer["instances"]["vps"]["outcome"] == runner_track.SWAPPED
    assert w.records.active_build("vps").runner_commit == C
    # C replaced the restored A, never the rejected B.
    assert w.records.previous_build(w.records.active_build("vps")).runner_commit == A


def test_a_build_failure_records_a_finding_and_leaves_the_instance_on_a():
    w = world()
    deployed(w, A)
    w.head = B
    w.scripts.failing.add(("test", "vps"))

    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"]["outcome"] == runner_track.BUILD_RED_NOW
    assert w.records.active_build("vps").runner_commit == A
    assert w.scripts.named("swap") == []
    [failed] = w.records.findings
    assert failed["class"] == instances.BUILD_RED
    assert failed["runner_commit"] == B and failed["project_id"] is None
    assert "zig build test failed" in failed["excerpt"]

    # The next hour does not build B again, and records no second finding.
    w.later()
    w.scripts.calls.clear()
    again = runner_track.reconcile(w.ports)
    assert again["instances"]["vps"]["outcome"] == runner_track.BUILD_RED_BEFORE
    assert w.scripts.calls == [] and len(w.records.findings) == 1


def test_a_busy_instance_waits_and_one_offline_between_jobs_is_swapped():
    w = world(instance("vps", "vps"), instance("spare", "vps"))
    deployed(w, A)
    w.head = B
    w.api.listed = [
        {"name": "vps-runner-41", "status": "online", "busy": True},
        {"name": "vps-runner-42", "status": "online", "busy": False},
        {"name": "spare-runner-7", "status": "offline", "busy": False},
    ]

    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"] == {"outcome": runner_track.BUSY, "busy": ["vps-runner-41"]}
    assert out["instances"]["spare"]["outcome"] == runner_track.SWAPPED
    assert w.records.active_build("vps").runner_commit == A
    assert w.records.active_build("spare").runner_commit == B
    # One build per host, however many instances it serves.
    assert len(w.scripts.named("build")) == 1


def test_instances_on_two_hosts_build_on_each_and_a_failure_on_one_spares_the_other():
    w = world(instance("vps", "vps"), instance("laptop", "laptop"))
    deployed(w, A)
    w.head = B
    w.scripts.failing.add(("build", "laptop"))

    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"]["outcome"] == runner_track.SWAPPED
    assert out["instances"]["laptop"]["outcome"] == runner_track.BUILD_RED_NOW
    assert sorted(c[1] for c in w.scripts.named("build")) == ["laptop", "vps"]
    assert w.records.active_build("laptop").runner_commit == A


def test_an_instance_on_the_head_is_left_alone():
    w = world()
    deployed(w, A)

    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"] == {"outcome": runner_track.CURRENT}
    assert w.scripts.calls == []


def test_an_unreachable_read_api_never_counts_as_idle():
    w = world()
    deployed(w, A)
    w.head = B
    w.api.down = True

    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"]["outcome"] == runner_track.API_UNREACHABLE
    assert "connection refused" in out["api"]
    assert w.scripts.named("swap") == []
    assert w.records.active_build("vps").runner_commit == A


def test_a_lost_compare_and_set_points_the_binary_back_at_the_record():
    w = world()
    deployed(w, A)
    w.head = B
    real_activate = w.records.activate

    def raced(instance_name, commit, expected_id, at):
        # Someone else activated C between our read and our write.
        real_activate(instance_name, C, expected_id, at)
        return real_activate(instance_name, commit, expected_id, at)

    w.records.activate = raced
    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"] == {"outcome": runner_track.LOST_RACE, "active": C}
    assert w.scripts.named("swap") == [("swap", "vps", "vps", B), ("swap", "vps", "vps", C)]


def test_a_failed_swap_script_changes_no_record():
    w = world()
    deployed(w, A)
    w.head = B
    w.scripts.failing.add(("swap", "vps"))

    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"]["outcome"] == runner_track.SWAP_ERROR
    assert w.records.active_build("vps").runner_commit == A


def test_no_runner_repository_means_nothing_to_track():
    w = world()
    w.operator = type(w.operator)(path=w.operator.path,
                                  operator_subject=w.operator.operator_subject,
                                  identity_provider=w.operator.identity_provider)

    assert "skipped" in runner_track.reconcile(w.ports)
    assert w.scripts.calls == []


def test_the_cron_is_not_set_up_without_its_scripts_directory():
    assert runner_track.real_ports(conn=None, environ={}) is None


# The real ports' parsing, without the network or a subprocess.

def test_busy_under_matches_the_prefix_and_either_busy_shape():
    runners = [{"name": "vps-runner-1", "busy": True},
               {"name": "vps-runner-2", "status": "busy"},
               {"name": "vps-runner-3", "status": "online"},
               {"name": "laptop-runner-1", "busy": True}]

    assert instances.busy_under("vps-runner", runners) == ["vps-runner-1", "vps-runner-2"]


def test_parse_runners_takes_a_list_or_a_runners_key_and_refuses_anything_else():
    row = {"name": "vps-runner-1"}

    assert instances.parse_runners([row]) == [row]
    assert instances.parse_runners({"runners": [row]}) == [row]
    with pytest.raises(ValueError):
        instances.parse_runners({"error": "no"})


def test_git_head_reads_the_default_branch_from_ls_remote():
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{B}\trefs/heads/main\n", "")

    assert instances.git_head(REPOSITORY, run=run) == B
    assert seen == [["git", "ls-remote", "https://github.com/example-owner/example-runner.git",
                     "refs/heads/main"]]
    with pytest.raises(LookupError):
        instances.parse_ls_remote(f"{B}\trefs/heads/other\n", "refs/heads/main")


def test_shell_scripts_run_locally_or_quoted_through_a_remote_prefix():
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1 if "test.sh" in argv[-2] else 0, "out", "err")

    scripts = instances.ShellScripts("/opt/runner/", {"laptop": ["ssh", "runner@laptop"]},
                                     run=run)

    assert scripts.swap("vps", "vps", B).ok
    failed = scripts.test("laptop", B)
    built = scripts.build("vps", REPOSITORY, B)

    assert built.ok and not failed.ok and failed.output == "outerr"
    assert seen[0] == ["/opt/runner/swap.sh", "vps", B]
    assert seen[1] == ["ssh", "runner@laptop", "/opt/runner/test.sh", B]
    assert seen[2] == ["/opt/runner/build.sh",
                       "https://github.com/example-owner/example-runner.git", B, "VERSION"]


def test_a_script_that_cannot_start_is_a_failed_result():
    def run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    result = instances.ShellScripts("/missing", run=run).swap("vps", "vps", B)

    assert not result.ok and not result.finished
    assert "swap.sh did not finish" in result.output


def test_a_finding_excerpt_is_bounded_and_scrubbed():
    token = "ghp_" + "x" * 36
    item = instances.finding(instances.BUILD_RED, "fp", B,
                             "\n".join([f"line {n}" for n in range(100)] + [token]))

    assert token not in item.excerpt
    assert len(item.excerpt.splitlines()) == instances.EXCERPT_LINES


# The deploy/runner link scripts, run for real on a scratch root. build.sh
# and test.sh fetch Zig, so they run only in the operator's live smoke.

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "runner"


def script(root, name, *args):
    return subprocess.run([str(SCRIPTS / f"{name}.sh"), *args], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "SLIPWAY_RUNNER_ROOT": str(root)})


def test_swap_and_rollback_move_the_current_and_previous_links(tmp_path):
    for commit in (A, B):
        (tmp_path / "builds" / commit / "bin").mkdir(parents=True)
    links = tmp_path / "instances" / "vps"

    assert script(tmp_path, "swap", "vps", A).returncode == 0
    assert script(tmp_path, "swap", "vps", B).returncode == 0
    assert os.readlink(links / "current") == str(tmp_path / "builds" / B)
    assert os.readlink(links / "previous") == str(tmp_path / "builds" / A)

    assert script(tmp_path, "rollback", "vps").returncode == 0
    assert os.readlink(links / "current") == str(tmp_path / "builds" / A)
    assert os.readlink(links / "previous") == str(tmp_path / "builds" / B)

    assert script(tmp_path, "rollback", "vps", B).returncode == 0
    assert os.readlink(links / "current") == str(tmp_path / "builds" / B)


def test_swap_refuses_an_unbuilt_commit_a_branch_name_and_a_path_as_instance(tmp_path):
    (tmp_path / "builds" / A / "bin").mkdir(parents=True)

    unbuilt = script(tmp_path, "swap", "vps", C)
    branch = script(tmp_path, "swap", "vps", "main")
    escape = script(tmp_path, "swap", "../vps", A)

    assert unbuilt.returncode and "is not built" in unbuilt.stderr
    assert branch.returncode and "not a full commit" in branch.stderr
    assert escape.returncode and "not an instance name" in escape.stderr
    assert not (tmp_path / "instances").exists()


def test_check_operator_refuses_a_file_without_the_read_api(tmp_path, monkeypatch):
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "config" / "operator.toml"
    monkeypatch.setenv("SLIPWAY_HOME", str(fixture.parent))
    assert runner_track.check_operator().runner_read_api

    text = fixture.read_text().replace('runner_read_api = "http://runner-api.internal:8080"\n', "")
    (tmp_path / "operator.toml").write_text(text)
    monkeypatch.setenv("SLIPWAY_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="runner_read_api"):
        runner_track.check_operator()


def test_closing_a_build_finding_asks_for_a_retry():
    w = world()
    deployed(w, A)
    w.head = B
    w.scripts.failing.add(("build", "vps"))
    runner_track.reconcile(w.ports)
    w.scripts.failing.clear()
    w.records.findings[0]["state"] = "closed"

    w.later()
    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"]["outcome"] == runner_track.SWAPPED
    assert w.records.active_build("vps").runner_commit == B


def test_a_build_that_never_finished_records_nothing_and_is_retried():
    w = world()
    deployed(w, A)
    w.head = B
    w.scripts.unfinished.add(("build", "vps"))

    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"]["outcome"] == runner_track.BUILD_UNFINISHED
    assert w.records.findings == []
    w.scripts.unfinished.clear()
    w.later()
    assert runner_track.reconcile(w.ports)["instances"]["vps"]["outcome"] == runner_track.SWAPPED
