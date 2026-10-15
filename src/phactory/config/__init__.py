"""The two configuration files every other unit reads (R1, R2, R3).

Both files are TOML (a plain text format of keys, values, and [tables]) and
live outside any repository, under PHACTORY_HOME:

    $PHACTORY_HOME/operator.toml          one per operator (operator.py)
    $PHACTORY_HOME/projects/<slug>.toml   one per project (project.py)

PHACTORY_HOME defaults to ~/.phactory. bindings.py reads both at call time
and resolves a role to an account, or a project to its tracker credential.

This module holds what the three share: where the files are, the error a
bad file raises, and a small reader that names the offending key.
"""

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from phactory.redact import find_token

HOME_VARIABLE = "PHACTORY_HOME"

# An environment variable name as the operator file writes them: capitals,
# digits, and underscores, starting with a capital.
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
# A value that looks like one in a project file: at least one underscore, so
# a plain word such as "QA" or "X64" is not mistaken for a variable.
LOOKS_LIKE_ENV_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


class ConfigError(ValueError):
    """A configuration file failed validation. field is the dotted key at fault."""

    def __init__(self, path: Path | str, field: str, message: str):
        self.path = Path(path)
        self.field = field
        self.message = message
        super().__init__(f"{self.path.name}: {field}: {message}")


def home(environ: Mapping[str, str] | None = None) -> Path:
    """The configuration root: PHACTORY_HOME when set, else ~/.phactory."""
    env = os.environ if environ is None else environ
    value = env.get(HOME_VARIABLE, "").strip()
    return Path(value).expanduser() if value else Path.home() / ".phactory"


def read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(path, "(file)", "not found") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(path, "(file)", f"not valid TOML: {error}") from None


def strings(data, prefix: str = ""):
    """Yield (dotted key, value) for every string anywhere in data."""
    if isinstance(data, str):
        yield prefix, data
    elif isinstance(data, Mapping):
        for key, value in data.items():
            yield from strings(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from strings(value, f"{prefix}[{index}]")


def refuse_secrets(path: Path, data: dict) -> None:
    """Refuse any string that has the shape of a token or key."""
    for key, value in strings(data):
        kind = find_token(value)
        if kind:
            raise ConfigError(path, key, f"looks like a secret ({kind}); name it instead")


class Reader:
    """Typed reads from one TOML table, naming the dotted key on any failure."""

    def __init__(self, path: Path, data: Mapping, prefix: str = ""):
        self.path = path
        self.data = data
        self.prefix = prefix

    def key(self, name: str) -> str:
        return f"{self.prefix}.{name}" if self.prefix else name

    def fail(self, name: str, message: str):
        raise ConfigError(self.path, self.key(name), message)

    def _get(self, name: str, kind, required: bool, default):
        if name not in self.data:
            if required:
                self.fail(name, "missing")
            return default
        value = self.data[name]
        # bool is a subclass of int, so an int field must refuse it by name.
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            self.fail(name, f"must be {kind.__name__}, got {type(value).__name__}")
        return value

    def text(self, name: str, required: bool = True, default=None):
        value = self._get(name, str, required, default)
        if value is not None and not value.strip():
            self.fail(name, "must not be empty")
        return value

    def integer(self, name: str, required: bool = True, default=None, minimum: int = 1):
        value = self._get(name, int, required, default)
        if value is not None and value < minimum:
            self.fail(name, f"must be at least {minimum}")
        return value

    def flag(self, name: str, required: bool = True, default=None):
        return self._get(name, bool, required, default)

    def strs(self, name: str, required: bool = True, nonempty: bool = False) -> tuple:
        value = self._get(name, list, required, [])
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                self.fail(f"{name}[{index}]", "must be a non empty string")
        if nonempty and not value:
            self.fail(name, "must list at least one entry")
        return tuple(value)

    def env(self, name: str, required: bool = True):
        value = self.text(name, required)
        if value is not None and not ENV_NAME.fullmatch(value):
            self.fail(name, f"{value!r} is not an environment variable name")
        return value

    def choice(self, name: str, enum, required: bool = True):
        value = self.text(name, required)
        if value is None:
            return None
        try:
            return enum(value)
        except ValueError:
            allowed = ", ".join(member.value for member in enum)
            self.fail(name, f"{value!r} is not one of {allowed}")

    def table(self, name: str, required: bool = True) -> "Reader | None":
        value = self._get(name, dict, required, None)
        return None if value is None else Reader(self.path, value, self.key(name))

    def tables(self, name: str) -> dict[str, "Reader"]:
        """A table of named tables, such as [accounts.<name>], keyed by name."""
        outer = self._get(name, dict, False, {})
        found = {}
        for entry, value in outer.items():
            if not isinstance(value, dict):
                self.fail(f"{name}.{entry}", "must be a table")
            found[entry] = Reader(self.path, value, self.key(f"{name}.{entry}"))
        return found

    def array_of_tables(self, name: str) -> list["Reader"]:
        value = self._get(name, list, False, [])
        found = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                self.fail(f"{name}[{index}]", "must be a table")
            found.append(Reader(self.path, item, self.key(f"{name}[{index}]")))
        return found

    def refuse_unknown(self, known) -> None:
        for name in self.data:
            if name not in known:
                self.fail(name, "is not a known key")
