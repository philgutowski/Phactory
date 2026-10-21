"""The U4 spike's timeout probes (step 6, and the open question on durable waits).

Three tasks, run by `python timeouts.py worker` and started by
`python timeouts.py start <name>`:

- `spike_labelled` asks for a worker label no connected worker carries, under a
  raised scheduling timeout, so the run shows whether it waits past the default
  five minutes for such a worker. `python timeouts.py labelled_worker` starts
  the worker that carries the label.
- `spike_long` runs for ninety seconds under a raised execution timeout, past
  the sixty second default.
- `spike_default_wait` is a durable task left at the default execution timeout
  that sleeps durably for two minutes, which shows whether that timeout bounds
  a durable wait.

The environment is the one `task.py` names.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import timedelta

from hatchet_sdk import Context, DesiredWorkerLabel, DurableContext, EmptyModel, Hatchet

hatchet = Hatchet()


def log(event: str, task: str) -> None:
    with open(os.environ.get("SPIKE_LOG", "spike.jsonl"), "a") as f:
        line = {"t": time.time(), "event": event, "task": task, "key": "timeouts"}
        f.write(json.dumps(line) + "\n")


@hatchet.task(
    name="spike_labelled",
    desired_worker_labels={"laptop": DesiredWorkerLabel(value="yes", required=True)},
    schedule_timeout=timedelta(minutes=30),
)
async def spike_labelled(_: EmptyModel, ctx: Context) -> dict[str, str]:
    log("start", "labelled")
    return {"ran": "yes"}


@hatchet.task(name="spike_long", execution_timeout=timedelta(minutes=5))
async def spike_long(_: EmptyModel, ctx: Context) -> dict[str, str]:
    log("start", "long")
    await asyncio.sleep(90)
    log("end", "long")
    return {"ran": "yes"}


@hatchet.durable_task(name="spike_default_wait")
async def spike_default_wait(_: EmptyModel, ctx: DurableContext) -> dict[str, str]:
    log("start", "default_wait")
    await ctx.aio_sleep_for(timedelta(minutes=2))
    log("end", "default_wait")
    return {"ran": "yes"}


TASKS = {"labelled": spike_labelled, "long": spike_long, "default_wait": spike_default_wait}


def main(argv: list[str]) -> None:
    if argv[0] == "worker":
        hatchet.worker(
            "spike-timeouts",
            slots=5,
            workflows=[spike_long, spike_default_wait, spike_labelled],
        ).start()
    elif argv[0] == "labelled_worker":
        hatchet.worker(
            "spike-laptop", slots=1, labels={"laptop": "yes"}, workflows=[spike_labelled]
        ).start()
    elif argv[0] == "start":
        print(TASKS[argv[1]].run_no_wait().workflow_run_id)
    elif argv[0] == "status":
        print(hatchet.runs.get_status(argv[1]))


if __name__ == "__main__":
    main(sys.argv[1:])
