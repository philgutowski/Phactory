"""The trace shape every builder adapter maps to, and the action log side reference (KTD17).

A trace is the record of one build: what the model was asked, which tools it
called, which files it changed, which commands it ran, and where it
connected. OpenTelemetry is the standard wire format for sending traces; in
it a trace is a tree of spans (one timed operation each, with attributes,
which are key and value pairs) and events (one instant inside a span). OTLP
is OpenTelemetry's own protocol, and its JSON form is what this module
writes. The GenAI semantic conventions are OpenTelemetry's agreed attribute
names for model calls and tool calls (gen_ai.request.model,
gen_ai.usage.input_tokens, gen_ai.tool.name, and so on), so Langfuse and any
other OpenTelemetry viewer reads the spans without a Slipway plugin.

An adapter never builds spans itself. Its own mapper turns the provider's
events (a Managed Agents event list, a CLI transcript) into shape records,
the five small types below, and yields them from Builder.events. Trace
builds the spans from those records:

    record        becomes
    ModelRequest  a span "chat <model>", operation chat, with token counts
    ToolCall      a span "execute_tool <name>", operation execute_tool
    FileChange    an event slipway.file_change on its tool call span, else the root
    Command       an event slipway.command on its tool call span, else the root
    Connection    an event slipway.connection on its tool call span, else the root

and one root span "invoke_agent <builder>" holds them all, carrying the card
key, the branch, the base and pushed commits, the diff counts, and the
build's cost. finish takes the session's start and end, so the root has real
times even when the session left no records.

The action log side reference. Every build's records also live in git, on
a reference on the project's repository, so the trace lasts as long as the
project (R22) even after Langfuse drops it. Records there are JSON lines (one
JSON object per line, a record's type under "type"). The convention, fixed
here so the handoff, both adapters, and the exporter agree:

    reference  refs/slipway/log/<card key>, outside refs/heads, so it is no
               branch and never shows on the board; the host ruleset (KTD6)
               names it beside refs/heads/slipway/** for the machine identity
    commits    one per build attempt and writer, each the child of the
               reference's previous tip, so the history is the attempt list
    tree       actions.jsonl, the builder's own action log, which the handoff
               asks the builder to keep and push; trace.jsonl, the records the
               adapter mapped from the provider's event log, written before the
               session is released; transcript.jsonl, the raw transcript when
               the builder has one (the CLI builder does)
    message    "slipway log <card key> build <build id>"

When a provider returns no event log, the trace is built from actions.jsonl
(AE5). Records are never part of the card's branch.

Redaction (KTD24). Tool arguments, tool results, and command lines are
written verbatim by dump_jsonl and otlp, so records are scrubbed before
either is called. scrubbed(records, injected_values) is the one place that
happens: it passes every string field of every record type through
redact.scrub, so the adapters and the exporter (U12, U13, U15) share one seam
instead of each remembering every field.
"""

import hashlib
import json
import re
import secrets
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields, replace

from slipway.redact import scrub

SIDE_REFERENCE_PREFIX = "refs/slipway/log/"
ACTIONS_FILE = "actions.jsonl"
TRACE_FILE = "trace.jsonl"
TRANSCRIPT_FILE = "transcript.jsonl"

# A card key becomes one path component of a git reference. Tracker keys are
# issue numbers or Jira style keys, so this is wide enough and refuses the
# characters and sequences git does not allow in a reference name.
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# OTLP span kinds and status codes, as the protobuf enums number them.
SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3
STATUS_UNSET = 0
STATUS_ERROR = 2

SCOPE_NAME = "slipway.trace"


def side_reference(key: str) -> str:
    """The action log reference for one card."""
    if not _KEY.fullmatch(key) or ".." in key or key.endswith((".lock", ".")):
        raise ValueError(f"card key {key!r} cannot name a git reference")
    return SIDE_REFERENCE_PREFIX + key


def log_commit_message(key: str, build_id: int | str) -> str:
    """The message of one attempt's commit on the side reference."""
    return f"slipway log {key} build {build_id}"


# Shape records. Times are nanoseconds since the Unix epoch, which is what
# OTLP carries. tool_call_id ties an event to the tool call that caused it.

@dataclass(frozen=True)
class ModelRequest:
    model: str
    start_ns: int
    end_ns: int
    input_tokens: int
    output_tokens: int
    provider: str = "anthropic"
    response_id: str | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    call_id: str
    start_ns: int
    end_ns: int
    arguments: str | None = None
    result: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class FileChange:
    path: str
    action: str  # created, modified, deleted, renamed
    time_ns: int
    tool_call_id: str | None = None


@dataclass(frozen=True)
class Command:
    command_line: str
    time_ns: int
    exit_code: int | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class Connection:
    """A connection the session made outside its sandbox, such as a package registry."""

    address: str
    time_ns: int
    port: int | None = None
    url: str | None = None
    tool_call_id: str | None = None


Record = ModelRequest | ToolCall | FileChange | Command | Connection

RECORD_TYPES: dict[str, type] = {
    "model_request": ModelRequest,
    "tool_call": ToolCall,
    "file_change": FileChange,
    "command": Command,
    "connection": Connection,
}
_TYPE_NAME = {cls: name for name, cls in RECORD_TYPES.items()}


def to_json_line(record: Record) -> str:
    data = {"type": _TYPE_NAME[type(record)], **asdict(record)}
    return json.dumps({k: v for k, v in data.items() if v is not None}, sort_keys=True)


def from_json_line(line: str) -> Record:
    data = json.loads(line)
    cls = RECORD_TYPES.get(data.pop("type", None))
    if cls is None:
        raise ValueError(f"not a shape record: {line[:80]!r}")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown {_TYPE_NAME[cls]} fields: {sorted(unknown)}")
    return cls(**data)


def scrubbed(records: Iterable[Record], injected_values: Iterable[str] = ()) -> list[Record]:
    """The records with every string field passed through redact.scrub.

    injected_values are the exact credential values Slipway put into the
    session; scrub also removes every known token shape. The fields are found
    by type at run time, not listed, so a string field added to a record type
    later is scrubbed without a change here. Call this before dump_jsonl and
    before Trace.add.
    """
    values = tuple(injected_values)
    out = []
    for record in records:
        changes = {f.name: scrub(value, values) for f in fields(record)
                   if isinstance(value := getattr(record, f.name), str)}
        out.append(replace(record, **changes))
    return out


def dump_jsonl(records: Iterable[Record]) -> str:
    """The trace.jsonl text for the side reference."""
    return "".join(to_json_line(r) + "\n" for r in records)


def load_jsonl(text: str) -> list[Record]:
    return [from_json_line(line) for line in text.splitlines() if line.strip()]


# OTLP JSON. Attribute values are typed wrappers; 64 bit integers are strings
# in the JSON mapping, as are the nanosecond timestamps.

def _value(value) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _attributes(attrs: dict) -> list[dict]:
    return [{"key": k, "value": _value(v)} for k, v in attrs.items() if v is not None]


@dataclass
class Event:
    name: str
    time_ns: int
    attributes: dict = field(default_factory=dict)

    def otlp(self) -> dict:
        return {"timeUnixNano": str(self.time_ns), "name": self.name,
                "attributes": _attributes(self.attributes)}


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: int
    start_ns: int
    end_ns: int
    attributes: dict = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    error: str | None = None

    def otlp(self) -> dict:
        span = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(self.end_ns),
            "attributes": _attributes(self.attributes),
            "events": [e.otlp() for e in self.events],
            "status": ({"code": STATUS_ERROR, "message": self.error} if self.error
                       else {"code": STATUS_UNSET}),
        }
        if self.parent_span_id:
            span["parentSpanId"] = self.parent_span_id
        return span


class Trace:
    """Builds one build's spans from shape records.

    builder names the adapter (managed_agents, cli). card_key, branch, and
    base come from the handoff. Call add for every record in the order the
    adapter yields them, finish with the session's start and end, the pushed
    commit, the diff counts, and the cost, then otlp for the export body.
    trace_id is 32 hex characters; a random one is drawn when none is given.
    Span ids are derived from the trace id and a counter, so the same records
    always give the same spans.
    """

    def __init__(self, builder: str, card_key: str, branch: str, base: str,
                 trace_id: str | None = None, conversation_id: str | None = None):
        self.trace_id = trace_id or secrets.token_hex(16)
        if not re.fullmatch(r"[0-9a-f]{32}", self.trace_id) or set(self.trace_id) == {"0"}:
            raise ValueError("trace_id must be 32 lower case hex characters, not all zero")
        self._count = 0
        self.root = Span(
            self.trace_id, self._next_id(), None, f"invoke_agent {builder}",
            SPAN_KIND_INTERNAL, 0, 0,
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": builder,
                "gen_ai.conversation.id": conversation_id,
                "slipway.card.key": card_key,
                "vcs.ref.head.name": branch,
                "vcs.ref.base.revision": base,
            },
        )
        self.spans: list[Span] = [self.root]
        self._tools: dict[str, Span] = {}

    def _next_id(self) -> str:
        self._count += 1
        digest = hashlib.sha256(f"{self.trace_id}:{self._count}".encode()).hexdigest()
        return digest[:16]

    def _child(self, name: str, kind: int, start: int, end: int, attrs: dict) -> Span:
        span = Span(self.trace_id, self._next_id(), self.root.span_id, name, kind, start, end,
                    attrs)
        self.spans.append(span)
        return span

    def _event(self, tool_call_id: str | None, event: Event) -> None:
        owner = self._tools.get(tool_call_id, self.root)
        owner.events.append(event)

    def add(self, record: Record) -> None:
        match record:
            case ModelRequest():
                self._child(f"chat {record.model}", SPAN_KIND_CLIENT, record.start_ns,
                            record.end_ns, {
                                "gen_ai.operation.name": "chat",
                                "gen_ai.provider.name": record.provider,
                                "gen_ai.request.model": record.model,
                                "gen_ai.response.model": record.model,
                                "gen_ai.response.id": record.response_id,
                                "gen_ai.response.finish_reasons": (
                                    [record.finish_reason] if record.finish_reason else None),
                                "gen_ai.usage.input_tokens": record.input_tokens,
                                "gen_ai.usage.output_tokens": record.output_tokens,
                                "gen_ai.usage.cache_read.input_tokens": record.cache_read_tokens,
                                "gen_ai.usage.cache_creation.input_tokens":
                                    record.cache_creation_tokens,
                            })
            case ToolCall():
                span = self._child(f"execute_tool {record.name}", SPAN_KIND_INTERNAL,
                                   record.start_ns, record.end_ns, {
                                       "gen_ai.operation.name": "execute_tool",
                                       "gen_ai.tool.name": record.name,
                                       "gen_ai.tool.call.id": record.call_id,
                                       "gen_ai.tool.type": "function",
                                       "gen_ai.tool.call.arguments": record.arguments,
                                       "gen_ai.tool.call.result": record.result,
                                   })
                span.error = record.error
                self._tools[record.call_id] = span
            case FileChange():
                self._event(record.tool_call_id, Event("slipway.file_change", record.time_ns, {
                    "slipway.file.path": record.path,
                    "slipway.file.action": record.action,
                }))
            case Command():
                self._event(record.tool_call_id, Event("slipway.command", record.time_ns, {
                    "process.command_line": record.command_line,
                    "process.exit.code": record.exit_code,
                }))
            case Connection():
                self._event(record.tool_call_id, Event("slipway.connection", record.time_ns, {
                    "server.address": record.address,
                    "server.port": record.port,
                    "url.full": record.url,
                }))
            case _:
                raise TypeError(f"not a shape record: {type(record).__name__}")

    def finish(self, commit: str | None = None, files_changed: int | None = None,
               insertions: int | None = None, deletions: int | None = None,
               error: str | None = None, start_ns: int | None = None,
               end_ns: int | None = None, cost: float | None = None) -> None:
        """Close the root span and set the diff, branch, and cost attributes.

        start_ns and end_ns are when the session started and ended. The root
        covers them and every child and event, so a session that left no
        records still gets a real span. cost is the provider's reported spend
        in US dollars (BuilderResult.cost), so the exported trace carries it
        beside the token counts.
        """
        if start_ns is not None and end_ns is not None and end_ns < start_ns:
            raise ValueError("a session cannot end before it starts")
        children = self.spans[1:]
        times = [t for s in children for t in (s.start_ns, s.end_ns)]
        times += [e.time_ns for s in self.spans for e in s.events]
        times += [t for t in (start_ns, end_ns) if t is not None]
        if times:
            self.root.start_ns, self.root.end_ns = min(times), max(times)
        self.root.attributes.update({
            "vcs.ref.head.revision": commit,
            "slipway.diff.files_changed": files_changed,
            "slipway.diff.insertions": insertions,
            "slipway.diff.deletions": deletions,
            "slipway.cost.usd": None if cost is None else float(cost),
        })
        self.root.error = error

    def otlp(self, service_name: str = "slipway", resource: dict | None = None) -> dict:
        """The OTLP JSON body of an ExportTraceServiceRequest for this trace."""
        return {"resourceSpans": [{
            "resource": {"attributes": _attributes({"service.name": service_name,
                                                    **(resource or {})})},
            "scopeSpans": [{
                "scope": {"name": SCOPE_NAME},
                "spans": [s.otlp() for s in self.spans],
            }],
        }]}


def trace_of(builder: str, card_key: str, branch: str, base: str, records: Iterable[Record],
             **finish) -> Trace:
    """Build and finish a trace from an adapter's records in one call."""
    trace = Trace(builder, card_key, branch, base, trace_id=finish.pop("trace_id", None),
                  conversation_id=finish.pop("conversation_id", None))
    for record in records:
        trace.add(record)
    trace.finish(**finish)
    return trace
