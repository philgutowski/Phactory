"""Onboard as one state machine with three calls (U19, KTD15, R29 to R33).

    start(ports, slug, actor)                       begin, resume, or re enter
    answer(ports, session, qid, value, actor)       validate, then save one answer
    status(ports, session, actor=None)              where the session stands

A session is one project's onboarding, and its id is the project's slug, so
a dropped session resumes from status with every saved answer (R33). Each
call returns a plain mapping whose outcome says what comes next:

    question             ask question (one at a time, R29)
    refused              the answer was not taken; reason says why, and the
                         same question comes back
    needs_fix            a property is missing (R30): missing and next_step
                         say what and the one thing to do, and question is
                         what to answer once it is done
    file_card            every check passed: card is the trivial card to
                         file on the tracker (R31); Slipway never files it
    waiting              the trivial card is not done yet, or only the
                         operator's questions are left
    verified             the trivial card scored; the project is verified

What is saved, and where. Every accepted answer goes to the session's state
file, $SLIPWAY_HOME/onboard/<slug>.json, with who gave it and when, and sets
verified to false. Once every key the project file needs has an answer, the
project file itself, $SLIPWAY_HOME/projects/<slug>.toml, is written from the
answers, after parse_project and check_names (config.project) accept the
whole of it, and is rewritten after every later answer. Until then no file
sits under projects/, so watch and dispatch never meet half a project. The
operator never edits the project file by hand; onboard run again changes it.

The rules KTD15 sets, and where each is kept:

- An operator only question answered by any other subject is refused with
  "the operator sets this", and nothing is written. The same subject is
  shown no choices for it, so it never sees the operator's account names or
  Langfuse projects, and status hides those answers from it.
- An answer that looks like a token is refused, and the audit row holds the
  reason, its SHA 256 hash, and its length (redact.refusal_record), never
  the value (KTD24).
- The gate command is stored and never run (KTD18). The project's own host
  runs it on the trivial card; when that card's gate fails, onboard returns
  to the gate question with the host's output.
- Onboard will not finish while another system dispatches the board.

Re entry (R32): start on a project that already has a file loads it, re
reads what may have changed (the repository is reachable, the board exists,
each role's account is in the operator file), and asks only the questions
whose answers are missing or no longer true. The property checks run again,
so a restriction removed since is caught there.

Every start and answer writes one audit row through the records (KTD14).
"""

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from slipway.config import ConfigError, Reader, home, read_toml, strings
from slipway.config.operator import Operator, load_operator
from slipway.config.project import (
    Project,
    Tracker,
    check_names,
    parse_project,
    project_path,
    read_branch_template,
)
from slipway.onboard import checks, questions
from slipway.onboard.questions import (
    CHOICE,
    LIST,
    NO,
    NUMBER,
    QUESTIONS,
    RECHECK,
    TRIVIAL_CARD,
    YES,
    YES_NO,
    Question,
)
from slipway.redact import find_token, refusal_record, scrub

QUESTION, REFUSED, NEEDS_FIX = "question", "refused", "needs_fix"
FILE_CARD, WAITING, VERIFIED = "file_card", "waiting", "verified"

ONBOARD = "onboard"
OPERATOR_SETS_THIS = "the operator sets this"
FROM_FILE = "project file"
HIDDEN = "(set by the operator)"
# Build states of the trivial card that send onboard back a step.
GATE_RED = "gate_red"
SCORED = "scored"
ENDED = ("returned_empty", "abandoned")

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GITHUB_BOARD = re.compile(r"(?:[A-Za-z0-9-]+/)?[0-9]+")
JIRA_BOARD = re.compile(r"[A-Z][A-Z0-9_]*")
SITE = re.compile(r"[A-Za-z0-9.-]+")


@dataclass(frozen=True)
class Ports:
    """What the machine reaches: the config root, the operator file, the probe, the records.

    records is the service layer's Records (service.verbs): onboard writes
    its audit rows there and reads the trivial card's build from it.
    """

    root: Path
    operator: Callable[[], Operator]
    probe: checks.Probe
    records: object
    clock: Callable[[], datetime]


class Refused(ValueError):
    """An answer was not taken. record is set when the value looked like a secret."""

    def __init__(self, reason: str, record=None):
        self.reason = reason
        self.record = record
        super().__init__(reason)


# The state file.

def state_path(root: Path, slug: str) -> Path:
    # project_path checks the slug, so a session id can never name another folder.
    project_path(slug, root)
    return root / ONBOARD / f"{slug}.json"


def _load(root: Path, slug: str) -> dict:
    path = state_path(root, slug)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"slug": slug, "answers": {}, "by": {}, "stale": {}, "attest": {},
            "attested": {}, "base": {}, "checks": None, "trivial": None, "verified": False}


def _write(path: Path, text: str) -> None:
    """Write text whole or not at all, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


def _save(root: Path, state: dict) -> None:
    _write(state_path(root, state["slug"]), json.dumps(state, indent=2, sort_keys=True) + "\n")


# Who may do what.

def _operator_subject(ports: Ports) -> str:
    return ports.operator().operator_subject


def _is_operator(ports: Ports, actor: str) -> bool:
    return actor == _operator_subject(ports)


def _permitted(ports: Ports, state: dict, actor: str) -> bool:
    """The operator, or a subject on the project's allowed list."""
    return _is_operator(ports, actor) or actor in (state["answers"].get("allowed_subjects")
                                                   or ())


# Choices.

def choices(ports: Ports, question: Question, draft: Mapping, actor: str) -> tuple[str, ...]:
    """The choices question offers actor. An operator only question offers none to anyone else."""
    if question.operator_only and not _is_operator(ports, actor):
        return ()
    source = question.choices_from
    if source is None:
        return ()
    if source in questions.FIXED:
        return questions.FIXED[source]
    if source in questions.LIVE:
        probe = ports.probe
        if source == questions.BUILDERS:
            return tuple(probe.builders())
        return tuple(getattr(probe, source)(draft))
    operator = ports.operator()
    if source == questions.ACCOUNTS:
        return tuple(sorted(operator.accounts))
    if source == questions.LANGFUSE_PROJECTS:
        return tuple(sorted(operator.langfuse_projects))
    if source == questions.BUILDER_WORKERS:
        return tuple(sorted(operator.builder_workers))
    if source == questions.TRACKER_SITES:
        return tuple(sorted({c.site for c in operator.tracker_credentials.values()}))
    if source == questions.TRACKER_CREDENTIALS:
        site = draft.get("tracker_site")
        return tuple(sorted(n for n, c in operator.tracker_credentials.items()
                            if site is None or c.site == site))
    if source == questions.HOST_CREDENTIALS:
        host = draft.get("host")
        return tuple(sorted(n for n, c in operator.host_credentials.items()
                            if host is None or c.host == host))
    return ()


def asked(ports: Ports, question: Question, state: dict, actor: str, note: str | None = None):
    """A question as a caller sees it: data only, choices filtered for actor."""
    shown = {"id": question.id, "text": question.text, "why": question.why,
             "kind": question.kind,
             "choices": list(choices(ports, question, state["answers"], actor)),
             "free_text": question.free_text, "default": question.default,
             "operator_only": question.operator_only, "required": question.required}
    note = note or state["stale"].get(question.id)
    if note:
        shown["note"] = note
    return shown


# Reading one answer.

def _secret(value) -> None:
    for _, text in strings(value):
        kind = find_token(text)
        if kind:
            raise Refused(f"looks like a secret ({kind}); name it instead",
                          refusal_record(text, f"looks like a secret ({kind})"))


def coerce(ports: Ports, question: Question, value, draft: Mapping):
    """The answer as it is saved, or Refused naming why. Secrets are refused first."""
    _secret(value)
    if isinstance(value, str):
        value = value.strip()
    if value in (None, "", []):
        if question.default is not None:
            value = question.default
        elif not question.required:
            return None
        else:
            raise Refused("an answer is needed")
    if question.kind == NUMBER:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise Refused(f"{value!r} is not a whole number") from None
        if isinstance(value, bool) or number < 1:
            raise Refused("must be a whole number of at least 1")
        return number
    if question.kind == YES_NO:
        if not isinstance(value, (str, bool)):
            raise Refused("answer yes or no")
        word = {True: YES, False: NO}.get(value, str(value).strip().lower())
        word = {"y": YES, "n": NO, "true": YES, "false": NO}.get(word, word)
        if word not in (YES, NO):
            raise Refused("answer yes or no")
        return word
    if question.kind == LIST:
        items = value.split(",") if isinstance(value, str) else list(value)
        items = [str(item).strip() for item in items if str(item).strip()]
        offered = choices(ports, question, draft, _operator_subject(ports))
        if offered and not question.free_text:
            for item in items:
                if item not in offered:
                    raise Refused(f"{item!r} is not one of {', '.join(offered)}")
        if not items and question.required:
            raise Refused("list at least one")
        return items or None
    value = str(value)
    if question.kind == CHOICE and not question.free_text:
        offered = choices(ports, question, draft, _operator_subject(ports))
        if value not in offered:
            if not offered:
                raise Refused(f"the operator file lists no {question.choices_from}")
            raise Refused(f"{value!r} is not one of {', '.join(offered)}")
    _shape(question.id, value, draft, ports.operator())
    return value


def _shape(qid: str, value: str, draft: Mapping, operator: Operator) -> None:
    """Checks one answer can make on its own, before the whole file is parsed."""
    if qid == "repository" and not REPOSITORY.fullmatch(value):
        raise Refused(f"{value!r} is not owner/name")
    if qid == "tracker_site" and not SITE.fullmatch(value):
        raise Refused(f"{value!r} is not a host name, such as name.atlassian.net")
    # The credential questions offer only entries for the answered site and
    # host, so an answer no entry matches would leave them with no choice.
    if qid == "tracker_site" and not any(
            c.site == value for c in operator.tracker_credentials.values()):
        raise Refused(f"the operator file has no tracker credential for {value!r}; "
                      "add one there first")
    if qid == "host" and not any(
            c.host == value for c in operator.host_credentials.values()):
        raise Refused(f"the operator file has no {value} host credential; add one there first")
    if qid == "board":
        if draft.get("tracker") == Tracker.GITHUB_PROJECTS and not GITHUB_BOARD.fullmatch(value):
            raise Refused(f"{value!r} is not a GitHub Projects board, owner/number or number")
        if draft.get("tracker") == Tracker.JIRA and not JIRA_BOARD.fullmatch(value):
            raise Refused(f"{value!r} is not a Jira project key, such as ABC")
    if qid == "tracker_credential":
        entry = operator.tracker_credentials[value]
        site = draft.get("tracker_site")
        if site and entry.site != site:
            raise Refused(f"{value!r} is for site {entry.site!r}, the project's site is "
                          f"{site!r}")
    if qid == "host_credential":
        entry = operator.host_credentials[value]
        host = draft.get("host")
        if host and entry.host != host:
            raise Refused(f"{value!r} is a {entry.host} credential, the project's host is "
                          f"{host}")
    if qid == "branch_template":
        try:
            read_branch_template(Reader(Path("project"), {"branch_template": value}))
        except ConfigError as error:
            raise Refused(error.message) from None
    if qid in ("trivial_card_target", "evals_path"):
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise Refused(f"{value!r} must be a path inside the repository")


# The project file.

def _data(state: dict) -> dict | None:
    """The project file's data from the answers, or None while a required key is unanswered."""
    answers = state["answers"]
    data = json.loads(json.dumps(state["base"]))  # keys onboard does not ask, such as rules
    for question in QUESTIONS:
        if question.field is None:
            continue
        if question.id not in answers or question.id in state["stale"]:
            return None
        value = answers[question.id]
        if question.kind == YES_NO:
            value = value == YES
        head, _, leaf = question.field.rpartition(".")
        table = data
        for part in head.split(".") if head else ():
            table = table.setdefault(part, {})
        if value is None:
            table.pop(leaf, None)
        else:
            table[leaf] = value
    return data


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    # A JSON string is a valid TOML basic string for the text a project file holds.
    return json.dumps(str(value), ensure_ascii=False)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def to_toml(data: Mapping, actor: str) -> str:
    """The project file text: a header, the plain keys, then each table."""
    lines = ["# Written by Slipway's onboard. Change it by running onboard again, not by hand.",
             f"# Last answer by: {actor}", ""]
    order = [q.field for q in QUESTIONS if q.field and "." not in q.field]
    keys = [k for k in order if k in data] + sorted(k for k in data if k not in order)
    tables = []

    def walk(prefix: str, table: Mapping):
        plain = [k for k in table if not isinstance(table[k], Mapping)]
        nested = [k for k in table if isinstance(table[k], Mapping)]
        if prefix and plain:
            tables.append("")
            tables.append(f"[{prefix}]")
            tables.extend(f"{_toml_key(k)} = {_toml_value(table[k])}" for k in plain)
        for k in nested:
            walk(f"{prefix}.{_toml_key(k)}" if prefix else _toml_key(k), table[k])

    for key in keys:
        if not isinstance(data[key], Mapping):
            lines.append(f"{_toml_key(key)} = {_toml_value(data[key])}")
    for key in keys:
        if isinstance(data[key], Mapping):
            walk(_toml_key(key), data[key])
    return "\n".join(lines + tables) + "\n"


def _project(ports: Ports, state: dict) -> Project | None:
    data = _data(state)
    if data is None:
        return None
    return parse_project(project_path(state["slug"], ports.root), data)


def _write_project(ports: Ports, state: dict, actor: str) -> None:
    """Write the project file once it is whole and both config checks accept it.

    A failure marks the question that fills the key at fault as no longer
    true, with the reason, so it is asked next.
    """
    data = _data(state)
    if data is None:
        return
    path = project_path(state["slug"], ports.root)
    try:
        project = parse_project(path, data)
        check_names(project, ports.operator())
    except ConfigError as error:
        _mark(state, error)
        return
    _write(path, to_toml(data, actor))
    state["written"] = _data_print(data)


def _data_print(data: Mapping) -> str:
    """A fingerprint of a project file's data, to tell onboard's own file from an edited one."""
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _mark(state: dict, error: ConfigError, keep: bool = False) -> None:
    """Mark the question that fills the key at fault as no longer true.

    A key no question fills, such as a [rules] table whose file is missing,
    has no answer that could fix it, so the error is raised to the caller.
    """
    question = questions.by_field(error.field)
    if question is None:
        raise error
    if keep:
        state["stale"].setdefault(question.id, error.message)
    else:
        state["stale"][question.id] = error.message


# The three calls.

def start(ports: Ports, slug: str, actor: str) -> dict:
    """Begin onboarding slug, resume a session, or re enter a project that has a file (R32)."""
    try:
        state = _load(ports.root, slug)
    except ConfigError as error:
        return {"session": slug, "outcome": REFUSED, "reason": error.message}
    path = project_path(slug, ports.root)
    data = None
    if path.exists():
        try:
            data = read_toml(path)
        except ConfigError as error:
            return {"session": slug, "outcome": REFUSED, "reason": str(error)}
        # The file's list is the one re entry takes, so it decides who may start.
        state["answers"]["allowed_subjects"] = data.get("allowed_subjects")
    # Permission first, so a subject off the list causes no host or tracker read.
    if not _permitted(ports, state, actor):
        result = {"session": slug, "outcome": REFUSED,
                  "reason": f"{actor} is not on this project's allowed subjects"}
        _audit(ports, slug, actor, "onboard_start", {}, result)
        return result
    if data is not None:
        try:
            _reenter(ports, state, data)
        except ConfigError as error:
            return {"session": slug, "outcome": REFUSED, "reason": str(error)}
    state["checks"] = None  # a new start re reads the host
    result = _next(ports, state, actor)
    _save(ports.root, state)
    _audit(ports, slug, actor, "onboard_start", {}, _brief(result))
    return result


def _reenter(ports: Ports, state: dict, data: dict) -> None:
    """Take the file's values as the answers, then mark what is no longer true.

    When the file is the one onboard last wrote, the state holds the same
    answers or newer ones (an answer the whole file refused on another key
    is in the state but not the file), so the state's are kept with their
    marks. Any other file, edited or with no state beside it, is the truth.
    """
    if state.get("written") == _data_print(data):
        _restale(ports, state)
        return
    state["base"] = {k: v for k, v in data.items() if not questions.by_field(k)}
    for question in QUESTIONS:
        if not question.field:
            continue
        value = data
        for part in question.field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, bool):
            value = YES if value else NO
        if value is None and question.required:
            continue  # missing from the file, so it is asked
        if question.id not in state["answers"] or state["answers"][question.id] != value:
            state["answers"][question.id] = value
            state["by"][question.id] = {"actor": FROM_FILE, "at": None}
            if question.id == "gate_command":
                state.pop("gate_note", None)  # the gate command changed outside onboard
    state["stale"] = {}
    # A gate the host failed on the trivial card is no longer true either,
    # and only the host's output says so; the file cannot.
    if state.get("gate_note"):
        state["stale"]["gate_command"] = state["gate_note"]
    try:
        project = _project(ports, state)
    except ConfigError as error:
        _mark(state, error)
        return
    if project is None:
        return
    _restale(ports, state, project)


def _restale(ports: Ports, state: dict, project: Project | None = None) -> None:
    """Mark what the host, the tracker, or the operator file says is no longer true."""
    if project is None:
        try:
            project = _project(ports, state)
        except ConfigError as error:
            # A key no question fills, such as a rules file removed since the
            # write, is raised to start, which refuses with its reason.
            _mark(state, error, keep=True)
            return
    if project is None:
        return
    state["stale"].update(checks.stale(ports.probe, project, ports.operator().accounts))
    try:
        check_names(project, ports.operator())
    except ConfigError as error:
        _mark(state, error, keep=True)


def answer(ports: Ports, session: str, qid: str, value, actor: str) -> dict:
    """Validate one answer, then save it, or refuse it and leave everything as it was."""
    try:
        state = _load(ports.root, session)
    except ConfigError as error:
        return {"session": session, "outcome": REFUSED, "reason": error.message}
    question = questions.find(qid, state["attest"])

    def refuse(reason: str, shown_value=None) -> dict:
        result = {"session": session, "outcome": REFUSED, "question_id": qid,
                  "reason": reason}
        if question is not None and _permitted(ports, state, actor):
            result["question"] = asked(ports, question, state, actor)
        _audit(ports, session, actor, "onboard_answer",
               {"question": qid, "value": shown_value}, _brief(result))
        return result

    if not _permitted(ports, state, actor):
        return refuse(f"{actor} is not on this project's allowed subjects", _shown(value))
    if question is None:
        return refuse(f"no question {qid!r}", _shown(value))
    if question.operator_only and not _is_operator(ports, actor):
        return refuse(OPERATOR_SETS_THIS, _shown(value))
    try:
        saved = coerce(ports, question, value, state["answers"])
    except Refused as error:
        shown = (asdict(error.record) if error.record else _shown(value))
        if error.record:
            shown.pop("reason")
        return refuse(error.reason, shown)
    now = ports.clock().isoformat()
    if question is RECHECK:
        if saved == YES:
            state["checks"] = None
    elif question is TRIVIAL_CARD:
        state["trivial"] = saved
    elif qid.startswith(questions.ATTEST_PREFIX):
        name = qid[len(questions.ATTEST_PREFIX):]
        if saved == YES:
            state["attested"][name] = {"actor": actor, "at": now, "what": state["attest"][name]}
        else:
            state["attested"].pop(name, None)
        state["checks"] = None
        state["verified"] = False
    else:
        state["answers"][qid] = saved
        state["by"][qid] = {"actor": actor, "at": now}
        state["stale"].pop(qid, None)
        if qid == "gate_command":
            state.pop("gate_note", None)
        state["verified"] = False
        state["trivial"] = None
        try:
            _write_project(ports, state, actor)
        except ConfigError as error:
            return refuse(f"the project file has a fault no question fixes: {error}",
                          _shown(saved))
        if qid in state["stale"]:
            # The whole file refused this very answer, so it is refused, and
            # nothing is saved: the state file is written only below.
            return refuse(state["stale"].pop(qid), _shown(saved))
    result = _next(ports, state, actor)
    _save(ports.root, state)
    _audit(ports, session, actor, "onboard_answer",
           {"question": qid, "value": _shown(saved)}, _brief(result))
    return result


def status(ports: Ports, session: str, actor: str | None = None) -> dict:
    """Where the session stands, with its saved answers, so a dropped session resumes."""
    try:
        state = _load(ports.root, session)
    except ConfigError as error:
        return {"session": session, "outcome": REFUSED, "reason": error.message}
    actor = actor or _operator_subject(ports)
    if not _permitted(ports, state, actor):
        return {"session": session, "outcome": REFUSED,
                "reason": f"{actor} is not on this project's allowed subjects"}
    result = _next(ports, state, actor)
    _save(ports.root, state)
    return result


# What comes next.

def pending(state: dict) -> list[Question]:
    """The walk's questions that are unanswered or no longer true, in order."""
    return [q for q in QUESTIONS
            if q.id not in state["answers"] or q.id in state["stale"]]


def _fingerprint(state: dict) -> str:
    text = json.dumps([state["answers"], sorted(state["attested"])], sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _next(ports: Ports, state: dict, actor: str) -> dict:
    slug = state["slug"]
    out = {"session": slug, "answers": _answers(ports, state, actor),
           "verified": state["verified"]}
    operator = _is_operator(ports, actor)
    left = pending(state)
    if left:
        mine = [q for q in left if operator or not q.operator_only]
        if not mine:
            return {**out, "outcome": WAITING, "pending": [q.id for q in left],
                    "reason": "only the operator's questions are left"}
        return {**out, "outcome": QUESTION, "pending": [q.id for q in left],
                "question": asked(ports, mine[0], state, actor)}
    try:
        project = _project(ports, state)
    except ConfigError as error:  # a key no question fills, such as a rules file gone since
        return {**out, "outcome": REFUSED,
                "reason": f"the project file has a fault no question fixes: {error}"}
    if state["checks"] is None or state["checks"]["for"] != _fingerprint(state):
        results = checks.run(ports.probe, project, state["answers"], state["attested"])
        state["checks"] = {"for": _fingerprint(state),
                           "results": [asdict(r) for r in results]}
    last = state["checks"]["results"][-1]
    if not last["ok"]:
        state["attest"].update(last["attest"])
        question = questions.find(last["question"] or RECHECK.id, state["attest"])
        verdict = {**out, "outcome": NEEDS_FIX, "property": last["property"],
                  "missing": last["missing"], "next_step": last["next_step"]}
        if question.operator_only and not operator:
            return {**verdict, "reason": "the operator answers the next step"}
        return {**verdict, "question": asked(ports, question, state, actor)}
    if not state["trivial"]:
        return {**out, "outcome": FILE_CARD, "card": trivial_card(project),
                "question": asked(ports, TRIVIAL_CARD, state, actor)}
    return _follow(ports, state, project, out, actor)


def _follow(ports: Ports, state: dict, project: Project, out: dict, actor: str) -> dict:
    """Follow the trivial card's build until it scores (R31)."""
    key = state["trivial"]
    records = ports.records
    build = records.latest(records.project_id(project.slug), key)
    if build is None and not getattr(records, "follows_builds", True):
        return {**out, "outcome": WAITING, "card_key": key,
                "reason": f"no records database is named, so card {key}'s build cannot be "
                          "followed; set SLIPWAY_DSN and run onboard again"}
    if build is None:
        return {**out, "outcome": WAITING, "card_key": key,
                "reason": f"card {key} has no build yet; watch dispatches it once it is in "
                          f"{project.ready_status} with the label {project.lane_label}"}
    if build["state"] == SCORED:
        state["verified"] = True
        return {**out, "outcome": VERIFIED, "verified": True, "card_key": key,
                "build_id": build["build_id"]}
    if build["state"] == GATE_RED:
        evidence = records.entered(build["build_id"], GATE_RED) or {}
        output = ports.probe.gate_output(project, build.get("pr_number")) or str(
            evidence.get("reason") or "the gate failed")
        note = (f"the trivial card {key}'s gate failed on the host with this output; "
                f"correct the gate command:\n{scrub(output)}")
        state["stale"]["gate_command"] = note
        state["gate_note"] = note
        state["trivial"] = None
        state["verified"] = False
        question = questions.BY_ID["gate_command"]
        return {**out, "outcome": QUESTION, "verified": False, "card_key": key,
                "pending": ["gate_command"], "question": asked(ports, question, state, actor)}
    if build["state"] in ENDED:
        state["trivial"] = None
        return {**out, "outcome": FILE_CARD, "card": trivial_card(project),
                "reason": f"card {key}'s build ended {build['state']}; file the card again",
                "question": asked(ports, TRIVIAL_CARD, state, actor)}
    return {**out, "outcome": WAITING, "card_key": key, "state": build["state"],
            "reason": f"card {key} is {build['state'].replace('_', ' ')}"}


def trivial_card(project: Project) -> dict:
    """The card onboard ends with (R31). The caller files it; Slipway never writes the tracker."""
    target = project.trivial_card_target
    block = {"good": f"{target} exists on the default branch and holds one line saying the "
                     f"repository is built through Slipway as project {project.slug}.",
             "paths": [target], "lane": project.lane_label}
    body = (f"Add {target} with one line saying the repository is built through Slipway as "
            f"project {project.slug}. This card proves the path from card to landing before "
            "the first real card.\n\n```slipway\n" + json.dumps(block, indent=2) + "\n```\n")
    where = (f"board {project.board}" if project.tracker is Tracker.GITHUB_PROJECTS
             else f"the Jira project {project.board}")
    return {"label": project.lane_label, "title": f"Slipway onboarding: add {target}",
            "body": body, "status": project.ready_status,
            "instruction": f"File this card on {where} with the label {project.lane_label}, "
                           f"in {project.ready_status}. Slipway never writes the tracker, so "
                           "the filing is yours. Then answer trivial_card with the key the "
                           "tracker gives it."}


# What a caller is shown.

def _answers(ports: Ports, state: dict, actor: str) -> dict:
    """The saved answers; the operator's own are hidden from any other subject."""
    operator = _is_operator(ports, actor)
    shown = {}
    for qid, value in state["answers"].items():
        question = questions.BY_ID.get(qid)
        hide = question is not None and question.operator_only and not operator
        shown[qid] = HIDDEN if hide else value
    return shown


def _shown(value):
    """An answer as an audit row keeps it, free text scrubbed (KTD24)."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [scrub(v) if isinstance(v, str) else v for v in value]
    return value


def _brief(result: dict) -> dict:
    """The result as an audit row keeps it: the outcome and the reason, no answers."""
    keep = ("outcome", "reason", "property", "question_id")
    brief = {k: result[k] for k in keep if k in result}
    if "question" in result:
        brief["next"] = result["question"]["id"]
    return brief


def _audit(ports: Ports, slug: str, actor: str, tool: str, arguments: dict,
           result: dict) -> None:
    ports.records.audit(slug, actor, tool, arguments, result)


def real_ports(records, probe: checks.Probe, root: Path | None = None,
               clock: Callable[[], datetime] | None = None) -> Ports:
    """Ports over SLIPWAY_HOME, reading the operator file on every call."""
    root = root or home()
    return Ports(root=root, operator=lambda: load_operator(root), probe=probe,
                 records=records, clock=clock or (lambda: datetime.now(UTC)))
