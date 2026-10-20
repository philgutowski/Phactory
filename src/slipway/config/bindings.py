"""Resolve a project's role or tracker credential, reading both files now (F4).

A binding is the pairing a project makes between a role (builder, judge, or
a helper) and an operator account, a model, an effort, and a time budget
(R13). Each workflow step calls resolve_binding when it runs, so an edit to
the project file takes effect on the next step with no restart, and the
step records what it used.

Anything that stops a binding from resolving raises BindingInvalid, whose
halt_class is binding_invalid (KTD11): dispatch ends the build as
returned_empty with that reason before any spend.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from slipway.config import ConfigError
from slipway.config.operator import Account, load_operator
from slipway.config.project import load_project

HALT_CLASS = "binding_invalid"


class BindingInvalid(Exception):
    """A role or credential could not be resolved. field is the key at fault."""

    halt_class = HALT_CLASS

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{HALT_CLASS}: {field}: {message}")


@dataclass(frozen=True)
class ResolvedBinding:
    role: str
    account: Account
    model: str
    effort: str
    time_budget_minutes: int


@dataclass(frozen=True)
class ResolvedTrackerCredential:
    """The tracker site and the two values read from the environment."""

    site: str
    email: str | None
    token: str

    def __repr__(self) -> str:
        # Never print the token, even by accident in a log line.
        return f"ResolvedTrackerCredential(site={self.site!r}, email=..., token=...)"


def _load(slug: str, root: Path | None):
    try:
        return load_project(slug, root), load_operator(root)
    except ConfigError as error:
        raise BindingInvalid(error.field, f"{error.path.name}: {error.message}") from None


def resolve_binding(slug: str, role: str, root: Path | None = None) -> ResolvedBinding:
    project, operator = _load(slug, root)
    binding = project.roles.get(role)
    if binding is None:
        raise BindingInvalid(f"roles.{role}", "the project binds no such role")
    account = operator.accounts.get(binding.account)
    if account is None:
        raise BindingInvalid(
            f"roles.{role}.account", f"{binding.account!r} is not in the operator file's accounts"
        )
    return ResolvedBinding(
        role, account, binding.model, binding.effort, binding.time_budget_minutes
    )


def resolve_tracker_credential(
    slug: str, root: Path | None = None, environ: Mapping[str, str] | None = None
) -> ResolvedTrackerCredential:
    env = os.environ if environ is None else environ
    project, operator = _load(slug, root)
    entry = operator.tracker_credentials.get(project.tracker_credential)
    if entry is None:
        raise BindingInvalid(
            "tracker_credential",
            f"{project.tracker_credential!r} is not in the operator file's tracker_credentials",
        )
    if entry.site != project.tracker_site:
        raise BindingInvalid(
            "tracker_credential",
            f"{entry.name!r} is for site {entry.site!r}, the project's site is "
            f"{project.tracker_site!r}",
        )

    def value(name: str | None, key: str) -> str | None:
        if name is None:
            return None
        found = env.get(name, "")
        if not found:
            raise BindingInvalid(
                f"tracker_credentials.{entry.name}.{key}", f"environment variable {name} is unset"
            )
        return found

    return ResolvedTrackerCredential(
        site=entry.site,
        email=value(entry.email_env, "email_env"),
        token=value(entry.token_env, "token_env"),
    )
