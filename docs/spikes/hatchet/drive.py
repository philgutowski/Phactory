"""Drive the U4 spike against a running engine and worker.

`python drive.py start <key> <project> <repository> <cap> [open_seconds] [build_seconds]`
starts one durable parent and prints its run id. `python drive.py merge <key>`
pushes the `pr.merged` event the parent waits on, the way Slipway's own webhook
endpoint pushes it through the SDK. `python drive.py status <run id>` prints the
run's status. The environment is the worker's (see `task.py`).
"""

from __future__ import annotations

import sys

from task import CardInput, hatchet, spike_card


def main(argv: list[str]) -> None:
    verb = argv[0]
    if verb == "start":
        key, project, repository, cap = argv[1], argv[2], argv[3], int(argv[4])
        open_seconds = float(argv[5]) if len(argv) > 5 else 60
        build_seconds = float(argv[6]) if len(argv) > 6 else 5
        ref = spike_card.run_no_wait(
            CardInput(
                project=project,
                repository=repository,
                cap=cap,
                key=key,
                open_seconds=open_seconds,
                build_seconds=build_seconds,
            )
        )
        print(ref.workflow_run_id)
    elif verb == "merge":
        hatchet.event.push("pr.merged", {"key": argv[1]})
        print("pushed")
    elif verb == "status":
        print(hatchet.runs.get_status(argv[1]))
    else:
        raise SystemExit(f"unknown verb {verb}")


if __name__ == "__main__":
    main(sys.argv[1:])
