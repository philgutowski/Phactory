"""The headless Claude Code CLI adapter, in a container or on the laptop (U13, KTD12).

A headless session is the `claude` command line run with `-p` (print mode):
it reads the handoff on standard input, works until it is done, and exits,
with no person at the keyboard. Two adapters drive it, sharing everything
but where the session runs:

    ContainerBuilder  one Docker container per card on the VPS, started
                      detached; the workflow polls it (remote_session)
    LaptopBuilder     the laptop's Hatchet worker runs the session as a
                      second user and blocks until it exits (worker_process)

Every session gets its own directory under the adapter's root, named by its
session reference:

    <root>/<session_ref>/
      run/          token (the git credential, rewritten on refresh) and
                    handoff.txt; mounted read only into a container
      workspace/    home/, config/ (an empty CLAUDE_CONFIG_DIR), repo/ (a
                    fresh clone, or a worktree; below), out/ (the CLI's final
                    JSON result)
      transcript.jsonl   the session transcript, copied out and scrubbed
      state.json    the card, branch, base, and, once terminal, the result

Nothing is shared between two sessions: no directory, no mount, no
configuration (AE3).

Worktree checkout (U26). A git worktree is a second working folder of one
repository: it has its own checked out branch and files, and shares the
repository's commits. A laptop builder made with checkout worktree and the
path of the laptop's shared checkout (a clone other sessions also use) does
not clone. Before the session it fetches the card's branch into the shared
checkout and adds a worktree at the base commit as the session's repo/; the
session switches to the card's branch there, and its git reads the remote
and the branch's upstream from its own environment, so nothing is written
to the shared checkout's configuration. Every git call on the shared
checkout runs as the session user, so the files it makes are the session's
to write and remove. On release git removes the worktree, its files go, and
the local branch the session made is deleted; a branch of that name the
shared checkout already had is never reset or deleted. The shared
checkout's own branch, files, and status are never touched. The session
user needs write access to the shared checkout's .git folder, which the
laptop's install gives through the worker's group. clone stays the
default, and a container always clones.

Credentials. The account token (CLAUDE_CODE_OAUTH_TOKEN for a subscription,
ANTHROPIC_API_KEY for an API key) reaches the session by environment only:
the container's command line names the variable and never carries its value.
The git credential is never in a remote URL: git asks a credential helper,
a small shell function that prints the contents of run/token, and the
adapter rewrites that file when the Credential is due (ten minutes before a
one hour token expires, so on a fifty minute cadence). A credential with no
expiry, such as a static repository access token, is never refreshed
(follow up #54). A refresh that fails ends the build as a halt with the
error as its reason, once: the laptop's refresh thread hands the error to
start, and the container's poll records the halt before it raises, so a
later poll answers from the record instead of raising again (follow up #67).

Redaction (KTD24). Every string that leaves the session passes through
redact.scrub with every value the adapter injected: the transcript when it
is copied out, every string field of every shape record, and the action log
read back from the side reference.

The side reference (follow up #56). The adapter is the one writer of the
commit that closes a build on refs/slipway/log/<key>. push_side_reference
writes trace.jsonl and transcript.jsonl, carries over the builder's own
actions.jsonl when the builder pushed one, and makes that commit the child
of the reference's current tip, so any commit the builder pushed stays in
the history.

KTD18. Slipway's control plane runs no project code. The control plane
here only runs docker and git plumbing (ls-remote, hash-object, mktree,
commit-tree, push) on a bare repository with hooks pointed at nothing; the
clone, the agent, and the project's tests run inside the container or as the
laptop's session user.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from slipway.builders.base import (
    BUILDER_FAILED,
    BuilderResult,
    Credential,
    CredentialError,
    Schedule,
    SessionStatus,
)
from slipway.builders.handoff import Handoff
from slipway.config.bindings import BindingInvalid, ResolvedBinding
from slipway.config.operator import AccountKind
from slipway.config.project import Checkout
from slipway.redact import scrub
from slipway.trace.shape import (
    ACTIONS_FILE,
    TRACE_FILE,
    TRANSCRIPT_FILE,
    Command,
    Connection,
    FileChange,
    ModelRequest,
    Record,
    ToolCall,
    dump_jsonl,
    log_commit_message,
    side_reference,
)

BUILDER_NAME = "cli"

# The variable the CLI reads its account token from, by account kind (R14).
TOKEN_VARIABLE = {
    AccountKind.SUBSCRIPTION: "CLAUDE_CODE_OAUTH_TOKEN",
    AccountKind.API_KEY: "ANTHROPIC_API_KEY",
}

# The permission mode for an unattended run: no prompt waits for a person.
# The container or the session user is the boundary, not the prompt.
PERMISSION_MODE = "bypassPermissions"

# A git credential helper as a shell function. Git calls it with "get" and
# reads username and password lines. It prints the token file's contents at
# the moment git asks, so a rewritten file is picked up by the next push.
CREDENTIAL_HELPER = (
    '!f() { test "$1" = get || exit 0; '
    "printf 'username=%s\\npassword=%s\\n' "
    '"$SLIPWAY_GIT_USERNAME" "$(cat "$SLIPWAY_TOKEN_FILE")"; }; f'
)

# The session itself, the same script in the container and on the laptop.
# Every value comes from the environment; the handoff comes on standard
# input, so neither it nor any secret is ever on a command line. Never
# `--bare`: the session reads the repository's own CLAUDE.md and settings.
SESSION_SCRIPT = """set -eu
mkdir -p "$HOME" "$CLAUDE_CONFIG_DIR" "$SLIPWAY_OUT"
git config --global credential.helper "$SLIPWAY_CREDENTIAL_HELPER"
git config --global user.name "$SLIPWAY_GIT_NAME"
git config --global user.email "$SLIPWAY_GIT_EMAIL"
git clone --quiet --branch "$SLIPWAY_BRANCH" "$SLIPWAY_REMOTE" "$SLIPWAY_REPO"
cd "$SLIPWAY_REPO"
exec claude -p --session-id "$SLIPWAY_SESSION_ID" --model "$SLIPWAY_MODEL" \\
  --permission-mode "$SLIPWAY_PERMISSION_MODE" --output-format json \\
  < "$SLIPWAY_HANDOFF" > "$SLIPWAY_OUT/result.json"
"""

# The worktree variant: the worker added repo/ as a worktree at the base, so
# the session marks both folders safe for its own git (they belong to other
# users), switches to the card's branch, and sets the branch's remote and
# upstream through git's environment config, which only this session reads.
WORKTREE_SCRIPT = SESSION_SCRIPT.replace(
    'git clone --quiet --branch "$SLIPWAY_BRANCH" "$SLIPWAY_REMOTE" "$SLIPWAY_REPO"\n'
    'cd "$SLIPWAY_REPO"\n',
    'git config --global --add safe.directory "$SLIPWAY_SHARED"\n'
    'git config --global --add safe.directory "$SLIPWAY_REPO"\n'
    'export GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_0=remote.slipway.url \\\n'
    '  GIT_CONFIG_VALUE_0="$SLIPWAY_REMOTE" GIT_CONFIG_KEY_1="branch.$SLIPWAY_BRANCH.remote" \\\n'
    '  GIT_CONFIG_VALUE_1=slipway GIT_CONFIG_KEY_2="branch.$SLIPWAY_BRANCH.merge" \\\n'
    '  GIT_CONFIG_VALUE_2="refs/heads/$SLIPWAY_BRANCH"\n'
    'cd "$SLIPWAY_REPO"\n'
    'git switch --quiet --create "$SLIPWAY_BRANCH"\n',
)

# Variables a session may inherit from the process that launches it. Every
# other variable, a database connection, a Hatchet token, SLIPWAY_HOME, is
# left behind (KTD12).
PASS_THROUGH = ("PATH", "LANG", "TZ")
# Variables the docker client itself needs to reach the engine.
DOCKER_CLIENT = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")

# Claude Code's file tools and how a result names a new file.
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash",)
FETCH_TOOLS = ("WebFetch",)

Run = Callable[..., subprocess.CompletedProcess]


def default_run(argv: Sequence[str], check: bool = True, env: Mapping[str, str] | None = None,
                input: str | None = None) -> subprocess.CompletedProcess:
    """The real run callable. Tests inject a fake instead."""
    return subprocess.run(list(argv), check=check, env=None if env is None else dict(env),
                          input=input, capture_output=True, text=True)


def now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class GitAccess:
    """Where the project's repository is and how the machine identity signs in.

    remote is the clone URL with no credential in it. username is the name
    the host expects beside a token (each host names its own). name and email
    sign the side reference commit.
    """

    remote: str
    username: str
    name: str = "slipway"
    email: str = "slipway@localhost"

    def __post_init__(self):
        if urlsplit(self.remote).username or "@" in urlsplit(self.remote).netloc:
            raise ValueError("a remote URL never carries a credential; the helper supplies it")


# The transcript mapper. A Claude Code transcript is JSON lines: each line is
# one entry with a "type" (user, assistant, system, summary), a "timestamp",
# and a "message". An assistant message's content holds tool_use blocks; the
# next user entry holds a tool_result block naming the same id. Joining the
# two by id gives one tool call with its arguments and its result.

@dataclass(frozen=True)
class Mapped:
    """The shape records a transcript gives, and how many lines were not JSON."""

    records: tuple[Record, ...]
    skipped: int


def _ns(stamp) -> int:
    if not isinstance(stamp, str):
        return 0
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1_000_000) * 1000


def _blocks(message) -> list[dict]:
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(content) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return json.dumps(content, sort_keys=True)


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _tool(block: dict, start: int, joined) -> list[Record]:
    name = str(block.get("name") or "unknown")
    call_id = str(block.get("id") or "")
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    if joined:
        end, result_block, extra = joined
        result = _text(result_block.get("content"))
        error = (result or "tool error") if result_block.get("is_error") else None
    else:
        end, result, error, extra = start, None, "no tool result in the transcript", None
    extra = extra if isinstance(extra, dict) else {}
    out: list[Record] = [ToolCall(name=name, call_id=call_id, start_ns=start,
                                  end_ns=max(end, start),
                                  arguments=json.dumps(args, sort_keys=True), result=result,
                                  error=error)]
    if name in SHELL_TOOLS and isinstance(args.get("command"), str):
        code = _int(extra.get("exitCode"))
        if code is None and joined:
            code = None if error else 0
        out.append(Command(command_line=args["command"], time_ns=start, exit_code=code,
                           tool_call_id=call_id))
    path = args.get("file_path") or args.get("notebook_path")
    if name in FILE_TOOLS and isinstance(path, str):
        action = "created" if extra.get("type") == "create" else "modified"
        out.append(FileChange(path=path, action=action, time_ns=start, tool_call_id=call_id))
    url = args.get("url")
    if name in FETCH_TOOLS and isinstance(url, str) and urlsplit(url).hostname:
        parts = urlsplit(url)
        port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
        out.append(Connection(address=parts.hostname, port=port, url=url, time_ns=start,
                              tool_call_id=call_id))
    return out


def map_transcript(text: str) -> Mapped:
    """Map a CLI transcript to shape records.

    A line that is not a JSON object is skipped and counted, never fatal.
    One assistant message is split over several lines that share its id;
    it becomes one ModelRequest from the line before it to its last line.
    """
    entries, skipped = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(entry, dict):
            skipped += 1
            continue
        entries.append(entry)

    results: dict[str, tuple] = {}
    last_line: dict[str, int] = {}
    for entry in entries:
        at = _ns(entry.get("timestamp"))
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        if entry.get("type") == "assistant" and message.get("id"):
            last_line[message["id"]] = at
        if entry.get("type") == "user":
            for block in _blocks(message):
                if block.get("type") == "tool_result" and block.get("tool_use_id"):
                    results[block["tool_use_id"]] = (at, block, entry.get("toolUseResult"))

    records: list[Record] = []
    seen: set[str] = set()
    previous = 0
    for entry in entries:
        at = _ns(entry.get("timestamp")) or previous
        if entry.get("type") == "assistant":
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            message_id = message.get("id")
            model = message.get("model")
            if message_id and message_id not in seen and model and model != "<synthetic>":
                seen.add(message_id)
                usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                records.append(ModelRequest(
                    model=str(model), start_ns=previous or at,
                    end_ns=max(last_line.get(message_id, at), at),
                    input_tokens=_int(usage.get("input_tokens")) or 0,
                    output_tokens=_int(usage.get("output_tokens")) or 0,
                    response_id=str(message_id),
                    cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
                    cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
                    finish_reason=message.get("stop_reason") or None,
                ))
            for block in _blocks(message):
                if block.get("type") == "tool_use":
                    records += _tool(block, at, results.get(block.get("id")))
        previous = at
    return Mapped(tuple(records), skipped)


def scrub_record(record: Record, injected: Iterable[str]) -> Record:
    """The record with every string field passed through redact.scrub."""
    injected = tuple(injected)
    changes = {f.name: scrub(value, injected) for f in fields(record)
               if isinstance(value := getattr(record, f.name), str)}
    return replace(record, **changes)


# Git on the control plane: plumbing only, on a bare repository, no hooks.

def _write_secret(path: Path, value: str, mode: int = 0o600) -> None:
    """Write value to path with mode, replacing the file in one step.

    The replace is atomic, so a credential helper reading the file mid
    refresh sees the old token or the new one, never half of either.
    """
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def git_env(token_file: Path, git: GitAccess, home: Path,
            environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a control plane git call: no user config, no prompt."""
    env = os.environ if environ is None else environ
    return {
        **{k: env[k] for k in PASS_THROUGH if k in env},
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": git.name,
        "GIT_AUTHOR_EMAIL": git.email,
        "GIT_COMMITTER_NAME": git.name,
        "GIT_COMMITTER_EMAIL": git.email,
        "SLIPWAY_TOKEN_FILE": str(token_file),
        "SLIPWAY_GIT_USERNAME": git.username,
    }


def _git(run: Run, env: Mapping[str, str], *args: str, git_dir: Path | None = None,
         check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    argv = ["git"]
    if git_dir is not None:
        argv += ["--git-dir", str(git_dir)]
    argv += ["-c", "core.hooksPath=/dev/null", "-c", f"credential.helper={CREDENTIAL_HELPER}",
             *args]
    return run(argv, check=check, env=env, input=input)


def remote_tip(run: Run, env: Mapping[str, str], remote: str, ref: str) -> str | None:
    """The commit ref points at on the remote, or None when it does not exist."""
    out = _git(run, env, "ls-remote", remote, ref).stdout.split()
    return out[0] if out else None


def push_side_reference(run: Run, workdir: Path, git: GitAccess, credential: Credential,
                        card_key: str, build_id: int | str, records: Iterable[Record],
                        transcript: str, injected: Iterable[str] = (),
                        environ: Mapping[str, str] | None = None) -> str:
    """Commit the build's log files on refs/slipway/log/<key> and push them.

    The commit holds trace.jsonl (records), transcript.jsonl, and the
    builder's own actions.jsonl when the reference's tip carries one, all
    scrubbed of the credential and every injected value. Its parent is the
    reference's current tip, so the history is the attempt list. Returns the
    new commit. The workflow calls this before release (KTD17), on the VPS,
    for a container build and for a laptop build alike.
    """
    reference = side_reference(card_key)
    workdir.mkdir(parents=True, exist_ok=True)
    token = workdir / "token"
    _write_secret(token, credential.current())
    values = (credential.current(), *injected)
    env = git_env(token, git, workdir, environ)
    git_dir = workdir / "log.git"
    try:
        if not git_dir.exists():
            run(["git", "init", "--bare", "--quiet", str(git_dir)], check=True, env=env)
        tip = remote_tip(run, env, git.remote, reference)
        files = {
            TRACE_FILE: dump_jsonl(scrub_record(r, values) for r in records),
            TRANSCRIPT_FILE: scrub(transcript, values),
        }
        if tip:
            _git(run, env, "fetch", "--quiet", git.remote, f"+{reference}:{reference}",
                 git_dir=git_dir)
            shown = _git(run, env, "show", f"{tip}:{ACTIONS_FILE}", git_dir=git_dir,
                         check=False)
            if shown.returncode == 0:
                files[ACTIONS_FILE] = scrub(shown.stdout, values)
        entries = []
        for name in sorted(files):
            blob = _git(run, env, "hash-object", "-w", "--stdin", git_dir=git_dir,
                        input=files[name]).stdout.strip()
            entries.append(f"100644 blob {blob}\t{name}\n")
        tree = _git(run, env, "mktree", git_dir=git_dir, input="".join(entries)).stdout.strip()
        argv = ["commit-tree", tree, "-m", log_commit_message(card_key, build_id)]
        if tip:
            argv += ["-p", tip]
        commit = _git(run, env, *argv, git_dir=git_dir).stdout.strip()
        _git(run, env, "push", "--quiet", git.remote, f"{commit}:{reference}", git_dir=git_dir)
        return commit
    finally:
        token.unlink(missing_ok=True)


# Sessions.

def session_name(card_key: str) -> str:
    """A fresh session reference: a container name unique per card and attempt."""
    slug = re.sub(r"[^a-z0-9]+", "-", card_key.lower()).strip("-") or "card"
    return f"slipway-{slug}-{secrets.token_hex(4)}"


def account_token(binding: ResolvedBinding, environ: Mapping[str, str]) -> tuple[str, str]:
    """The CLI's token variable and its value for the binding's account."""
    account = binding.account
    value = environ.get(account.env, "")
    if not value:
        raise BindingInvalid(f"accounts.{account.name}.env",
                             f"environment variable {account.env} is unset")
    return TOKEN_VARIABLE[AccountKind(account.kind)], value


@dataclass(frozen=True)
class Paths:
    """Where a session's directories are, as the session sees them."""

    run: str
    workspace: str

    @property
    def home(self) -> str:
        return f"{self.workspace}/home"

    @property
    def config(self) -> str:
        return f"{self.workspace}/config"

    @property
    def repo(self) -> str:
        return f"{self.workspace}/repo"

    @property
    def out(self) -> str:
        return f"{self.workspace}/out"


def session_env(paths: Paths, handoff: Handoff, binding: ResolvedBinding, git: GitAccess,
                session_id: str) -> dict[str, str]:
    """The session's variables, none of them secret. The token is added by name."""
    return {
        "HOME": paths.home,
        "CLAUDE_CONFIG_DIR": paths.config,
        "SLIPWAY_REPO": paths.repo,
        "SLIPWAY_OUT": paths.out,
        "SLIPWAY_TOKEN_FILE": f"{paths.run}/token",
        "SLIPWAY_HANDOFF": f"{paths.run}/handoff.txt",
        "SLIPWAY_CREDENTIAL_HELPER": CREDENTIAL_HELPER,
        "SLIPWAY_GIT_USERNAME": git.username,
        "SLIPWAY_GIT_NAME": git.name,
        "SLIPWAY_GIT_EMAIL": git.email,
        "SLIPWAY_REMOTE": git.remote,
        "SLIPWAY_BRANCH": handoff.branch,
        "SLIPWAY_SESSION_ID": session_id,
        "SLIPWAY_MODEL": binding.model,
        "SLIPWAY_PERMISSION_MODE": PERMISSION_MODE,
    }


class _Sessions:
    """What both adapters share: the session directory, refresh, copy out, events."""

    schedule: Schedule
    # The token file's mode. A container runs as the uid that owns the file;
    # a laptop session runs as a second user in the worker's group.
    TOKEN_MODE = 0o600

    def __init__(self, root: Path, git: GitAccess, run: Run = default_run,
                 environ: Mapping[str, str] | None = None,
                 now: Callable[[], datetime] = now_utc):
        self.root = Path(root)
        self.git = git
        self.run = run
        self.environ = os.environ if environ is None else environ
        self.now = now
        # In memory only: the credential object and every value injected into
        # a session, so a transcript can be scrubbed of a token since rotated.
        self._credentials: dict[str, Credential] = {}
        self._injected: dict[str, list[str]] = {}

    # Directories and state.

    def dir(self, session_ref: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", session_ref):
            raise ValueError(f"not a session reference: {session_ref!r}")
        return self.root / session_ref

    def _state(self, session_ref: str) -> dict:
        return json.loads((self.dir(session_ref) / "state.json").read_text())

    def _save(self, session_ref: str, state: dict) -> None:
        (self.dir(session_ref) / "state.json").write_text(json.dumps(state, sort_keys=True))

    def _prepare(self, handoff: Handoff, binding: ResolvedBinding,
                 credential: Credential) -> tuple[str, Path, str, str]:
        variable, value = account_token(binding, self.environ)
        session_ref = session_name(handoff.card_key)
        top = self.dir(session_ref)
        workspace = top / "workspace"
        for path, mode in ((top, 0o750), (top / "run", 0o750), (workspace, 0o770),
                           *((workspace / n, 0o770) for n in ("home", "config", "repo", "out"))):
            path.mkdir(parents=True)
            path.chmod(mode)  # chmod, because mkdir's mode is cut by the umask
        (top / "run" / "handoff.txt").write_text(handoff.text)
        _write_secret(top / "run" / "token", credential.current(), self.TOKEN_MODE)
        self._credentials[session_ref] = credential
        self._injected[session_ref] = [value, credential.current()]
        session_id = str(uuid.uuid4())
        self._save(session_ref, {
            "card_key": handoff.card_key, "branch": handoff.branch, "base": handoff.base,
            "session_id": session_id, "model": binding.model, "account": binding.account.name,
            "token_variable": variable, "account_env": binding.account.env,
        })
        return session_ref, top, session_id, value

    def injected(self, session_ref: str) -> tuple[str, ...]:
        """Every value injected into the session that this process still knows."""
        values = list(self._injected.get(session_ref, ()))
        token = self.dir(session_ref) / "run" / "token"
        if token.exists():
            values.append(token.read_text())
        account_env = self._state(session_ref).get("account_env")
        if account_env:
            values.append(self.environ.get(account_env, ""))
        return tuple(v for v in values if v)

    # The credential (KTD6, follow up #54).

    def refresh_if_due(self, session_ref: str) -> bool:
        """Rewrite the token file when the credential is near expiry.

        A credential with no expiry is static and is never refreshed, so a
        static token cannot raise in the middle of a build.
        """
        credential = self._credentials.get(session_ref)
        if credential is None or credential.expires_at is None:
            return False
        if not credential.due(self.now()):
            return False
        value = credential.refresh()
        _write_secret(self.dir(session_ref) / "run" / "token", value, self.TOKEN_MODE)
        self._injected.setdefault(session_ref, []).append(value)
        return True

    # The end of a session.

    def _git_env(self, session_ref: str) -> dict[str, str]:
        top = self.dir(session_ref)
        return git_env(top / "run" / "token", self.git, top, self.environ)

    def _finish(self, session_ref: str, exit_code: int | None, detail: str = "",
                halt: CredentialError | None = None) -> BuilderResult:
        """Copy the transcript out, read the pushed tip, and record the result.

        halt is a refresh that failed mid build. The result is then a halt
        with the error as its reason whatever the branch holds, because a
        session that lost its credential cannot be trusted to have finished.
        exit_code is None when the session was still running.
        """
        top = self.dir(session_ref)
        state = self._state(session_ref)
        found = sorted((top / "workspace" / "config" / "projects").glob(
            f"*/{state['session_id']}.jsonl"))
        raw = found[0].read_text(errors="replace") if found else ""
        (top / TRANSCRIPT_FILE).write_text(scrub(raw, self.injected(session_ref)))
        cost, stop = None, None
        result_file = top / "workspace" / "out" / "result.json"
        try:
            final = json.loads(result_file.read_text())
            if isinstance(final, dict):
                cost = final.get("total_cost_usd")
                cost = float(cost) if isinstance(cost, (int, float)) else None
                stop = final.get("subtype")
        except (OSError, ValueError):
            pass
        if halt is not None:
            commit = None
            reason = f"credential refresh failed: {halt}"
            if exit_code is not None:
                reason += f"; session exited with code {exit_code}"
        else:
            tip = remote_tip(self.run, self._git_env(session_ref), self.git.remote,
                             f"refs/heads/{state['branch']}")
            commit = tip if tip and tip != state["base"] else None
            reason = f"session exited with code {exit_code}"
            if stop:
                reason += f", {stop}"
            if detail:
                reason += f", {detail}"
            if commit is None:
                reason += f"; nothing was pushed to {state['branch']}"
        result = BuilderResult(
            branch=state["branch"], commit=commit,
            reason=scrub(reason, self.injected(session_ref)), provider_ref=session_ref,
            cost=cost, model=state["model"], account=state["account"],
            halt_class=None if commit else BUILDER_FAILED,
        )
        state["result"] = asdict(result)
        state["skipped"] = map_transcript((top / TRANSCRIPT_FILE).read_text()).skipped
        self._save(session_ref, state)
        return result

    def _terminal(self, session_ref: str) -> SessionStatus | None:
        state = self._state(session_ref)
        if "result" not in state:
            return None
        return SessionStatus(True, BuilderResult(**state["result"]))

    def transcript(self, session_ref: str) -> str | None:
        """The scrubbed transcript once the session is terminal."""
        path = self.dir(session_ref) / TRANSCRIPT_FILE
        return path.read_text() if path.exists() else None

    def events(self, session_ref: str) -> Iterator[Record] | None:
        text = self.transcript(session_ref)
        if text is None:
            return None
        values = self.injected(session_ref)
        return iter([scrub_record(r, values) for r in map_transcript(text).records])

    def push_log(self, session_ref: str, build_id: int | str) -> str:
        """Write this session's records to the card's side reference (KTD17)."""
        state = self._state(session_ref)
        text = self.transcript(session_ref) or ""
        return push_side_reference(
            self.run, self.dir(session_ref) / "log", self.git,
            self._credentials[session_ref], state["card_key"], build_id,
            map_transcript(text).records, text, self.injected(session_ref), self.environ)

    def _forget(self, session_ref: str) -> None:
        self._credentials.pop(session_ref, None)
        self._injected.pop(session_ref, None)

    def delete(self, session_ref: str) -> None:
        """A CLI session leaves no provider record; release removed everything."""
        self._forget(session_ref)


class ContainerBuilder(_Sessions):
    """One Docker container per card on the VPS (KTD12).

    image is the tag built from deploy/builder.Dockerfile, network the
    isolated builder network, user the non root uid:gid the container runs
    as. The worker writes the session directory, so user must be the uid
    that owns it; the token file is mode 600.
    """

    schedule = Schedule.REMOTE_SESSION
    WORKSPACE = "/workspace"
    RUN = "/run/slipway"

    def __init__(self, root: Path, git: GitAccess, image: str, network: str, user: str,
                 run: Run = default_run, environ: Mapping[str, str] | None = None,
                 now: Callable[[], datetime] = now_utc):
        super().__init__(root, git, run, environ, now)
        uid = user.split(":")[0]
        if uid in ("", "0", "root"):
            raise ValueError("a builder container never runs as root")
        if network in ("host", "bridge", "default", ""):
            raise ValueError(f"network {network!r} is not an isolated builder network")
        self.image, self.network, self.user = image, network, user

    def invocation(self, session_ref: str, top: Path, variables: Mapping[str, str],
                   token_variable: str) -> list[str]:
        """The docker run command line. It names the token variable, never its value."""
        argv = [
            "docker", "run", "--detach", "--name", session_ref,
            "--user", self.user,
            "--read-only",
            "--network", self.network,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp",
            "--mount", f"type=bind,source={top / 'workspace'},target={self.WORKSPACE}",
            "--mount", f"type=bind,source={top / 'run'},target={self.RUN},readonly",
            "--workdir", self.WORKSPACE,
        ]
        for name, value in variables.items():
            argv += ["--env", f"{name}={value}"]
        argv += ["--env", token_variable]
        argv += [self.image, "/bin/sh", "-c", SESSION_SCRIPT]
        return argv

    def start(self, handoff: Handoff, binding: ResolvedBinding, credential: Credential) -> str:
        session_ref, top, session_id, value = self._prepare(handoff, binding, credential)
        variables = session_env(Paths(self.RUN, self.WORKSPACE), handoff, binding, self.git,
                                session_id)
        token_variable = self._state(session_ref)["token_variable"]
        client = {k: self.environ[k] for k in DOCKER_CLIENT if k in self.environ}
        client[token_variable] = value
        self.run(self.invocation(session_ref, top, variables, token_variable), check=True,
                 env=client)
        return session_ref

    def poll(self, session_ref: str) -> SessionStatus:
        done = self._terminal(session_ref)
        if done:
            return done
        inspected = self.run(["docker", "inspect", "--format", "{{json .State}}", session_ref],
                             check=True)
        state = json.loads(inspected.stdout)
        if state.get("Running") or state.get("Status") in ("created", "running", "restarting"):
            try:
                self.refresh_if_due(session_ref)
            except CredentialError as error:
                # Record the halt first, so the next poll answers from it and
                # the error is raised once, not on every tick (follow up #67).
                self._finish(session_ref, None, halt=error)
                raise
            return SessionStatus(False, detail=state.get("Status") or "running")
        detail = "out of memory" if state.get("OOMKilled") else (state.get("Error") or "")
        result = self._finish(session_ref, int(state.get("ExitCode") or 0), detail)
        return SessionStatus(True, result)

    def release(self, session_ref: str) -> None:
        """Remove the container and the session directory."""
        self.run(["docker", "rm", "--force", "--volumes", session_ref], check=False)
        shutil.rmtree(self.dir(session_ref), ignore_errors=True)


class LaptopBuilder(_Sessions):
    """A session on the laptop, run by its Hatchet worker as a second user (KTD12).

    The worker runs as a dedicated user and holds its worker token, the
    subscription token, and the git credential. session_user is the second
    user each session runs as, through sudo with only the session's own
    variables kept. path is the PATH the session gets. start blocks until
    the session exits, which is what worker_process means.
    """

    schedule = Schedule.WORKER_PROCESS
    TOKEN_MODE = 0o640

    def __init__(self, root: Path, git: GitAccess, session_user: str, path: str,
                 run: Run = default_run, environ: Mapping[str, str] | None = None,
                 now: Callable[[], datetime] = now_utc, refresh_seconds: float = 60.0,
                 checkout: Checkout | str = Checkout.CLONE, shared: Path | None = None):
        super().__init__(root, git, run, environ, now)
        if session_user in ("", "root"):
            raise ValueError("a laptop session never runs as root")
        self.session_user, self.path, self.refresh_seconds = session_user, path, refresh_seconds
        self.checkout = Checkout(checkout)
        if self.checkout is Checkout.WORKTREE and shared is None:
            raise ValueError("a worktree checkout needs the path of the shared checkout")
        self.shared = Path(shared) if shared is not None else None

    def environment(self, session_ref: str, handoff: Handoff, binding: ResolvedBinding,
                    session_id: str, token_value: str) -> dict[str, str]:
        """The session's whole environment, built from nothing (KTD12)."""
        top = self.dir(session_ref)
        variables = session_env(Paths(str(top / "run"), str(top / "workspace")), handoff,
                                binding, self.git, session_id)
        token_variable = self._state(session_ref)["token_variable"]
        if self.checkout is Checkout.WORKTREE:
            variables["SLIPWAY_SHARED"] = str(self.shared)
        return {"PATH": self.path, **variables, token_variable: token_value}

    def invocation(self, env: Mapping[str, str]) -> list[str]:
        keep = ",".join(sorted(env))
        script = WORKTREE_SCRIPT if self.checkout is Checkout.WORKTREE else SESSION_SCRIPT
        return ["sudo", "--non-interactive", "--user", self.session_user,
                f"--preserve-env={keep}", "--", "/bin/sh", "-c", script]

    def _shared_git(self, session_ref: str, *args: str, check: bool = True):
        """A git call on the shared checkout, as the session user, with no hook.

        The session user runs every git call that writes the worktree, its
        record under the shared .git, or its local branch, so the session's
        own git and the release can write and remove all of it. The worker's
        umask never decides who may write a file the session needs.
        """
        env = self._git_env(session_ref)
        keep = ",".join(sorted(env))
        argv = ["git", "-c", "core.hooksPath=/dev/null", "-c",
                f"credential.helper={CREDENTIAL_HELPER}", "-c", f"safe.directory={self.shared}",
                "-C", str(self.shared), *args]
        return self.run(["sudo", "--non-interactive", "--user", self.session_user,
                         f"--preserve-env={keep}", "--", *argv], check=check, env=env)

    def _add_worktree(self, session_ref: str, handoff: Handoff) -> None:
        """Fetch the card's branch into the shared checkout, add repo/ at the base."""
        repo = self.dir(session_ref) / "workspace" / "repo"
        # A branch of that name already in the shared checkout is someone
        # else's: the session's switch then fails rather than reset it, and
        # release leaves it alone.
        found = self._shared_git(session_ref, "rev-parse", "--verify", "--quiet",
                                 f"refs/heads/{handoff.branch}", check=False)
        state = self._state(session_ref)
        state["shared_branch_existed"] = found.returncode == 0
        self._save(session_ref, state)
        self._shared_git(session_ref, "fetch", "--quiet", self.git.remote,
                         f"refs/heads/{handoff.branch}")
        self._shared_git(session_ref, "worktree", "add", "--quiet", "--detach", str(repo),
                         handoff.base)

    def start(self, handoff: Handoff, binding: ResolvedBinding, credential: Credential) -> str:
        session_ref, top, session_id, value = self._prepare(handoff, binding, credential)
        if self.checkout is Checkout.WORKTREE:
            self._add_worktree(session_ref, handoff)
        env = self.environment(session_ref, handoff, binding, session_id, value)
        stop = threading.Event()
        failed: list[CredentialError] = []

        def keep_fresh():
            # A refresh that fails ends this thread, never silently: start
            # reads the error and ends the build as a halt (follow up #67).
            while not stop.wait(self.refresh_seconds):
                try:
                    self.refresh_if_due(session_ref)
                except CredentialError as error:
                    failed.append(error)
                    return

        keeper = threading.Thread(target=keep_fresh, daemon=True)
        keeper.start()
        try:
            done = self.run(self.invocation(env), check=False, env=env)
        finally:
            stop.set()
            keeper.join()
        self._finish(session_ref, done.returncode, halt=failed[0] if failed else None)
        return session_ref

    def poll(self, session_ref: str) -> SessionStatus:
        done = self._terminal(session_ref)
        return done or SessionStatus(False, detail="running")

    def release(self, session_ref: str) -> None:
        """Remove the session's files, the workspace as the user that wrote it."""
        top = self.dir(session_ref)
        if self.checkout is Checkout.WORKTREE:
            # git removes the worktree and its record together; the rm and the
            # prune below cover a worktree git could not remove.
            self._shared_git(session_ref, "worktree", "remove", "--force",
                             str(top / "workspace" / "repo"), check=False)
        self.run(["sudo", "--non-interactive", "--user", self.session_user, "--",
                  "rm", "-rf", str(top / "workspace")], check=False)
        if self.checkout is Checkout.WORKTREE:
            self._shared_git(session_ref, "worktree", "prune", check=False)
            state = self._state(session_ref)
            if state.get("shared_branch_existed") is False:
                self._shared_git(session_ref, "branch", "--quiet", "-D", state["branch"],
                                 check=False)
        shutil.rmtree(top, ignore_errors=True)


@dataclass(frozen=True)
class LaptopReturn:
    """What the laptop_build child returns to the parent on the VPS.

    Plain JSON values only, because Hatchet carries it as the task output.
    The transcript is already scrubbed; the parent maps it and writes every
    record and the side reference.
    """

    result: BuilderResult
    transcript: str
    skipped: int

    def to_json(self) -> dict:
        return {"result": asdict(self.result), "transcript": self.transcript,
                "skipped": self.skipped}

    @classmethod
    def from_json(cls, data: Mapping) -> "LaptopReturn":
        return cls(BuilderResult(**data["result"]), data["transcript"], int(data["skipped"]))


def laptop_build(builder: LaptopBuilder, handoff: Handoff, binding: ResolvedBinding,
                 credential: Credential) -> LaptopReturn:
    """The body of the laptop_build child: run one session, return, clean up."""
    session_ref = builder.start(handoff, binding, credential)
    try:
        status = builder.poll(session_ref)
        text = builder.transcript(session_ref) or ""
        return LaptopReturn(status.result, text, map_transcript(text).skipped)
    finally:
        builder.release(session_ref)
        builder.delete(session_ref)


def worker_label(project, operator) -> str:
    """The Hatchet worker label a laptop bound project's build is routed to.

    project.builder_worker names an entry in the operator file's
    builder_workers; that entry's label is what the build child declares, so
    only the worker started under it picks the task up. The worker connects
    out, so a closed laptop leaves the task queued.
    """
    name = project.builder_worker
    if not name:
        raise BindingInvalid("builder_worker", "the project names no builder worker")
    worker = operator.builder_workers.get(name)
    if worker is None:
        raise BindingInvalid("builder_worker",
                             f"{name!r} is not in the operator file's builder_workers")
    return worker.label
