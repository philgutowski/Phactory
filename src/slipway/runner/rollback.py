"""The rollback evaluator: put an instance back on its previous runner build (U22, KTD20, R37).

Observation (U23) calls evaluate(ports, gate_run) after it writes a gate run
and that run's findings. The evaluator rolls the gate run's instance back
only when the runner itself is to blame, never for a project's own red test:

1. The gate run carries a runner class finding, one of RUNNER_CLASSES: a
   step the runner does not support, a gap in its log sequence, a failed
   write to its database, or a conclusion that disagrees with the host's
   check. A run whose only failure is the project's test has none of these
   and changes nothing.
2. The run is attributed to the instance's active build, or to one already
   rolled back. A run on a build that a normal swap replaced is left alone.
3. This project's latest gate run on the previous build on the same
   instance exists and carries no runner class finding. A finding the
   previous build already produced for this project is not a regression,
   and with no run on the previous build there is nothing to compare.

When all three hold, the rollback script repoints the instance at the
previous build, then a compare and set marks the active row rejected and
opens a new active row for the previous commit, and a runner_regression
finding is written. The rejected row is what makes the hourly cron skip the
commit until a newer head exists.

The script runs before the compare and set so a script that fails leaves
the records untouched and the step is retried whole. Two evaluations racing
both repoint to the same previous build, which is harmless, and only one
wins the compare and set. A gate run that arrives after the rollback and
passes the same three tests (a second regression in the same hour) finds
its build already rejected: no second swap, and its finding points at the
first rollback's finding.
"""

from collections.abc import Mapping
from typing import Any

from slipway.runner.instances import REGRESSION, RunnerPorts, finding

UNSUPPORTED_STEP = "unsupported_step"
LOG_GAP = "log_gap"
DB_WRITE_ERROR = "db_write_error"
CONCLUSION_DISAGREE = "conclusion_disagree"
# The findings that blame the runner rather than the project (R39).
RUNNER_CLASSES = frozenset({UNSUPPORTED_STEP, LOG_GAP, DB_WRITE_ERROR, CONCLUSION_DISAGREE})

# Outcomes evaluate reports.
NO_RUNNER_FINDING = "no_runner_finding"
UNATTRIBUTED = "unattributed"
SUPERSEDED = "superseded"
NO_PREVIOUS = "no_previous"
NO_BASELINE = "no_baseline"
NOT_COUNTED = "not_counted"
ROLLED_BACK = "rolled_back"
ALREADY_ROLLED_BACK = "already_rolled_back"


def evaluate(ports: RunnerPorts, gate_run: Mapping[str, Any]) -> dict:
    """Roll gate_run's instance back if its runner build regressed; say what happened.

    gate_run is the gate_runs row as a mapping: id, project_id, instance,
    and runner_build_id.
    """
    records = ports.records
    classes = sorted(records.finding_classes(gate_run["id"]) & RUNNER_CLASSES)
    if not classes:
        return {"outcome": NO_RUNNER_FINDING}
    build = records.runner_build(gate_run["runner_build_id"]) \
        if gate_run.get("runner_build_id") is not None else None
    if build is None:
        return {"outcome": UNATTRIBUTED, "classes": classes}
    out = {"instance": build.instance, "runner_commit": build.runner_commit,
           "classes": classes}

    if build.state not in ("active", "rejected"):
        return {**out, "outcome": SUPERSEDED}

    previous = records.previous_build(build)
    if previous is None:
        return {**out, "outcome": NO_PREVIOUS}
    out["previous_commit"] = previous.runner_commit
    baseline = records.latest_gate_run(gate_run["project_id"], build.instance,
                                       previous.runner_commit)
    if baseline is None:
        return {**out, "outcome": NO_BASELINE}
    if records.finding_classes(baseline["id"]) & RUNNER_CLASSES:
        return {**out, "outcome": NOT_COUNTED, "baseline_gate_run": baseline["id"]}
    if build.state == "rejected":
        return {**out, **_repeat(ports, gate_run, build, classes)}

    host = ports.operator().runner_instances[build.instance].host
    script = ports.scripts.rollback(host, build.instance, previous.runner_commit)
    if not script.ok:
        # Nothing is recorded, so the step fails and the engine runs it again.
        raise RuntimeError(f"rollback.sh failed on {build.instance}: {script.output[-500:]}")
    restored = records.roll_back(build.id, previous.runner_commit, ports.now())
    if restored is None:
        # Another evaluation won the compare and set between our read and now.
        build = records.runner_build(build.id)
        return {**out, **_repeat(ports, gate_run, build, classes)}

    finding_id = records.add_finding(finding(
        REGRESSION, f"{REGRESSION}:{build.instance}:{build.runner_commit}",
        build.runner_commit,
        f"{', '.join(classes)} on {build.instance} at runner {build.runner_commit}; "
        f"restored {previous.runner_commit}, whose last gate run for this project "
        f"({baseline['id']}) had no runner class finding.",
        project_id=gate_run["project_id"], gate_run_id=gate_run["id"],
        runner_build_id=build.id))
    return {**out, "outcome": ROLLED_BACK, "restored_build": restored,
            "finding_id": finding_id}


def _repeat(ports: RunnerPorts, gate_run: Mapping[str, Any], build, classes) -> dict:
    """A regression on a build already rolled back: one more finding, no swap."""
    first = ports.records.rollback_finding(build.id)
    finding_id = ports.records.add_finding(finding(
        REGRESSION, f"{REGRESSION}:{build.instance}:{build.runner_commit}",
        build.runner_commit,
        f"{', '.join(classes)} on {build.instance} at runner {build.runner_commit}, "
        f"already rolled back (finding {first}).",
        project_id=gate_run["project_id"], gate_run_id=gate_run["id"],
        runner_build_id=build.id, related_finding_id=first))
    return {"outcome": ALREADY_ROLLED_BACK, "finding_id": finding_id, "related_finding": first}


__all__ = ["CONCLUSION_DISAGREE", "DB_WRITE_ERROR", "LOG_GAP", "RUNNER_CLASSES",
           "UNSUPPORTED_STEP", "evaluate"]
