"""The seam registry: the three places later units add steps to the card workflow.

A seam is a named point in the card workflow where an ordered list of step
callables runs, so a later unit can add work there without editing the
workflow. There are three:

    post_build     every exit of building when a session was started:
                   export the trace, commit the log, release the session (U15)
    gate_observed  every check.completed, queue.requested, and pr.dequeued
                   event: read the gate run and the runner's record (U23)
    post_landed    after landed, before scored: the deterministic score
                   (U16), the judged score (U17), the runner record (U23)

This unit registers no step on any seam, so each one is a no op until a
later unit adds its own.

How a later unit adds a step, without importing workflow. The import rule
(KTD23) lets nothing outside workflow/ import workflow, so a unit's package
cannot call a register function here. Instead each unit adds a steps module
in its own package, named in STEP_MODULES below, holding one registration
line:

    SLIPWAY_STEPS = (("post_build", export_trace), ("post_build", commit_log))

load() imports each module in STEP_MODULES that exists and registers its
pairs in the order written, module by module, so the order across units is
the order of STEP_MODULES and the order within a unit is its own. A module
that does not exist yet is skipped, which is the state of all three today.

A step is called as step(context) with a SeamContext and returns a mapping
of structured fields (keys, commits, numbers) or None. Its result is kept
under its own name in the seam's outcome, which the workflow writes into
the evidence of the transition that follows. A step that raises stops the
seam, and the workflow's child fails and is retried by the engine, so every
step must be safe to run twice.
"""

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

POST_BUILD = "post_build"
GATE_OBSERVED = "gate_observed"
POST_LANDED = "post_landed"
SEAMS = (POST_BUILD, GATE_OBSERVED, POST_LANDED)

# The steps modules later units add, in the order their steps run. Each is
# imported only if present.
STEP_MODULES = ("slipway.trace.steps", "slipway.evals.steps", "slipway.runner.steps")
REGISTRATION = "SLIPWAY_STEPS"


@dataclass(frozen=True)
class SeamContext:
    """What a step receives.

    project is the loaded project file, build the build's record as a
    mapping, ports the workflow's ports (records, host, tracker, builder),
    and event the host event that reached the seam, when one did.
    """

    seam: str
    project: Any
    build: Mapping[str, Any]
    ports: Any
    event: Mapping[str, Any] | None = None


Step = Callable[[SeamContext], Mapping[str, Any] | None]


@dataclass
class Registry:
    """An ordered list of steps per seam."""

    steps: dict[str, list[tuple[str, Step]]] = field(
        default_factory=lambda: {seam: [] for seam in SEAMS})

    def register(self, seam: str, step: Step, name: str | None = None) -> None:
        if seam not in self.steps:
            raise ValueError(f"{seam!r} is not a seam: {', '.join(SEAMS)}")
        self.steps[seam].append((name or getattr(step, "__name__", repr(step)), step))

    def names(self, seam: str) -> list[str]:
        return [name for name, _ in self.steps[seam]]

    def run(self, seam: str, context: SeamContext) -> dict[str, Any]:
        """Run the seam's steps in registration order; none registered is a no op."""
        outcome: dict[str, Any] = {}
        for name, step in self.steps[seam]:
            result = step(context)
            outcome[name] = dict(result) if result else {}
        return outcome


def load(modules=STEP_MODULES, importer=importlib.import_module) -> Registry:
    """A registry filled from every steps module that exists, in order."""
    registry = Registry()
    for name in modules:
        try:
            module = importer(name)
        except ModuleNotFoundError as error:
            # Only the module itself being absent is skipped; a module that
            # exists and fails on its own import is a real error.
            missing = error.name or ""
            if name == missing or name.startswith(missing + "."):
                continue
            raise
        for seam, step in getattr(module, REGISTRATION, ()):
            registry.register(seam, step)
    return registry
