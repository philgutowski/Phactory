"""The U4 spike's durable task, kept as the pattern U14 follows.

A durable parent with no concurrency key runs two child tasks in order, then
waits for a host event raced with a durable sleep. The shape mirrors U14's card
workflow: `build` holds the per project key, `open` holds the per repository key,
and the parent holds nothing while it waits days for a merge.

Terms used here, for a reader new to Hatchet:

- Durable task: a task whose progress the engine writes to its own log, so that
  after a crash or a redeploy the engine replays the log instead of starting over.
- Concurrency key: a value computed from a task's input (a CEL expression, a
  small expression language) that the engine counts running tasks under. A key
  at `max_runs` N lets N tasks with the same value run and holds the rest.
- Or group: a set of wait conditions where the first one met ends the wait.

Run the worker with `HATCHET_CLIENT_TOKEN` and `HATCHET_CLIENT_TLS_STRATEGY=none`
set, from a venv holding `hatchet-sdk`, as `python task.py`. Every child appends
one JSON line on start and one on end to the file named by `SPIKE_LOG`, which
is how the spike reads overlaps and counts after the fact.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import timedelta

from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
    DurableContext,
    Hatchet,
    SleepCondition,
    UserEventCondition,
    or_,
)
from pydantic import BaseModel

hatchet = Hatchet()

# Changing this string between two worker starts is the spike's "trivial code
# change" for the redeploy steps. It is logged so the report can show which
# deploy finished each run.
VARIANT = os.environ.get("SPIKE_VARIANT", "v1")


class CardInput(BaseModel):
    project: str
    repository: str
    cap: int
    key: str
    build_seconds: float = 5
    open_seconds: float = 60
    wait_hours: float = 1


def log(event: str, task: str, card: CardInput, **extra: object) -> None:
    line = {
        "t": time.time(),
        "event": event,
        "task": task,
        "key": card.key,
        "project": card.project,
        "repository": card.repository,
        "variant": VARIANT,
        **extra,
    }
    with open(os.environ.get("SPIKE_LOG", "spike.jsonl"), "a") as f:
        f.write(json.dumps(line) + "\n")


@hatchet.task(
    name="spike_build",
    input_validator=CardInput,
    concurrency=ConcurrencyExpression(
        expression="input.project",
        max_runs="input.cap",
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
    execution_timeout=timedelta(minutes=10),
    schedule_timeout=timedelta(hours=2),
)
async def spike_build(card: CardInput, ctx: Context) -> dict[str, str]:
    log("start", "build", card, attempt=ctx.retry_count)
    await asyncio.sleep(card.build_seconds)
    log("end", "build", card)
    return {"branch": f"slipway/{card.key}"}


@hatchet.task(
    name="spike_open",
    input_validator=CardInput,
    concurrency=ConcurrencyExpression(
        expression="input.repository",
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
    execution_timeout=timedelta(minutes=10),
    schedule_timeout=timedelta(hours=2),
)
async def spike_open(card: CardInput, ctx: Context) -> dict[str, int]:
    log("start", "open", card, attempt=ctx.retry_count)
    await asyncio.sleep(card.open_seconds)
    log("end", "open", card)
    return {"pr_number": 1}


@hatchet.durable_task(
    name="spike_card",
    input_validator=CardInput,
    execution_timeout=timedelta(days=8),
    schedule_timeout=timedelta(hours=2),
)
async def spike_card(card: CardInput, ctx: DurableContext) -> dict[str, object]:
    log("start", "parent", card)
    built = await spike_build.aio_run(card)
    log("child_done", "parent", card, child="build", result=built)
    opened = await spike_open.aio_run(card)
    log("child_done", "parent", card, child="open", result=opened)
    log("wait", "parent", card)
    woke = await ctx.aio_wait_for(
        f"merge:{card.key}",
        or_(
            UserEventCondition(
                event_key="pr.merged",
                expression=f"input.key == '{card.key}'",
            ),
            SleepCondition(timedelta(hours=card.wait_hours)),
        ),
    )
    log("woke", "parent", card, woke=woke)
    return {"woke": woke, "variant": VARIANT}


def main() -> None:
    worker = hatchet.worker(
        "spike-worker",
        slots=20,
        workflows=[spike_card, spike_build, spike_open],
    )
    worker.start()


if __name__ == "__main__":
    main()
