"""U11 test scenarios for the trace shape and the action log side reference."""

import json
import re
import typing
from dataclasses import fields

import pytest

from slipway.redact import MARK
from slipway.trace.shape import (
    RECORD_TYPES,
    SIDE_REFERENCE_PREFIX,
    SPAN_KIND_CLIENT,
    SPAN_KIND_INTERNAL,
    STATUS_ERROR,
    Command,
    Connection,
    FileChange,
    ModelRequest,
    Record,
    ToolCall,
    Trace,
    dump_jsonl,
    from_json_line,
    load_jsonl,
    log_commit_message,
    scrubbed,
    side_reference,
    trace_of,
)
from test_imports import SRC, edges_of, layer

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
S = 1_000_000_000  # one second in nanoseconds

RECORDS = [
    ModelRequest(model="model-large", start_ns=10 * S, end_ns=12 * S, input_tokens=1200,
                 output_tokens=340, response_id="msg_1", cache_read_tokens=800,
                 finish_reason="tool_use"),
    ToolCall(name="bash", call_id="tool_1", start_ns=13 * S, end_ns=14 * S,
             arguments='{"command": "pytest -q"}', result="3 passed"),
    Command(command_line="pytest -q", time_ns=13 * S + 5, exit_code=0, tool_call_id="tool_1"),
    ToolCall(name="edit", call_id="tool_2", start_ns=15 * S, end_ns=16 * S),
    FileChange(path="src/hello.py", action="modified", time_ns=15 * S + 7,
               tool_call_id="tool_2"),
    ToolCall(name="bash", call_id="tool_3", start_ns=17 * S, end_ns=18 * S,
             error="exit 1"),
    Connection(address="pypi.org", port=443, url="https://pypi.org/simple/", time_ns=17 * S + 9,
               tool_call_id="tool_3"),
    FileChange(path="README.md", action="created", time_ns=19 * S),
]


def build():
    return trace_of("cli", "EX-12", "slipway/EX-12-hello-page", "a" * 40, RECORDS,
                    trace_id=TRACE_ID, commit="b" * 40, files_changed=2, insertions=10,
                    deletions=1)


def attrs(span):
    out = {}
    for item in span["attributes"]:
        (kind, value), = item["value"].items()
        out[item["key"]] = int(value) if kind == "intValue" else value
    return out


# A structural check of the OTLP JSON mapping (opentelemetry-proto's
# ExportTraceServiceRequest as JSON): ids are lower case hex of the right
# length, 64 bit integers and timestamps are decimal strings, every attribute
# value is exactly one typed wrapper, and parents exist in the same trace.
HEX = {"traceId": 32, "spanId": 16, "parentSpanId": 16}
VALUE_KINDS = {"stringValue", "boolValue", "intValue", "doubleValue", "arrayValue",
               "kvlistValue", "bytesValue"}


def check_value(value):
    assert isinstance(value, dict) and len(value) == 1, value
    (kind, inner), = value.items()
    assert kind in VALUE_KINDS, kind
    if kind == "intValue":
        assert isinstance(inner, str) and re.fullmatch(r"-?\d+", inner)
    if kind == "stringValue":
        assert isinstance(inner, str)
    if kind == "arrayValue":
        for item in inner["values"]:
            check_value(item)


def check_attributes(items):
    keys = [item["key"] for item in items]
    assert len(keys) == len(set(keys)), keys
    for item in items:
        assert set(item) == {"key", "value"} and item["key"]
        check_value(item["value"])


def check_otlp(body):
    body = json.loads(json.dumps(body, allow_nan=False))  # it must survive JSON as is
    assert set(body) == {"resourceSpans"}
    for resource_spans in body["resourceSpans"]:
        check_attributes(resource_spans["resource"]["attributes"])
        for scope_spans in resource_spans["scopeSpans"]:
            assert scope_spans["scope"]["name"]
            spans = scope_spans["spans"]
            ids = {span["spanId"] for span in spans}
            assert len(ids) == len(spans)
            for span in spans:
                for key, length in HEX.items():
                    if key in span:
                        assert re.fullmatch(f"[0-9a-f]{{{length}}}", span[key]), key
                        assert set(span[key]) != {"0"}
                assert span.get("parentSpanId", next(iter(ids))) in ids
                assert span["kind"] in range(6)
                start, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
                assert isinstance(span["startTimeUnixNano"], str) and 0 < start <= end
                assert span["name"]
                check_attributes(span["attributes"])
                for event in span["events"]:
                    assert isinstance(event["timeUnixNano"], str) and event["name"]
                    check_attributes(event["attributes"])
                assert span["status"]["code"] in (0, 1, 2)
    return body


# The span builder.

def test_three_tool_calls_and_one_model_request_serialise_with_genai_names():
    body = check_otlp(build().otlp())
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root, *children = spans
    assert [s["name"] for s in children] == [
        "chat model-large", "execute_tool bash", "execute_tool edit", "execute_tool bash"]

    chat = attrs(children[0])
    assert chat["gen_ai.operation.name"] == "chat"
    assert chat["gen_ai.provider.name"] == "anthropic"
    assert chat["gen_ai.request.model"] == "model-large"
    assert chat["gen_ai.usage.input_tokens"] == 1200
    assert chat["gen_ai.usage.output_tokens"] == 340
    assert chat["gen_ai.usage.cache_read.input_tokens"] == 800
    assert "gen_ai.usage.cache_creation.input_tokens" not in chat
    assert children[0]["kind"] == SPAN_KIND_CLIENT

    for span, call_id in zip(children[1:], ("tool_1", "tool_2", "tool_3"), strict=True):
        tool = attrs(span)
        assert tool["gen_ai.operation.name"] == "execute_tool"
        assert tool["gen_ai.tool.call.id"] == call_id
        assert tool["gen_ai.tool.name"] in ("bash", "edit")
        assert span["kind"] == SPAN_KIND_INTERNAL
        assert span["parentSpanId"] == root["spanId"]
    assert attrs(children[1])["gen_ai.tool.call.arguments"] == '{"command": "pytest -q"}'
    assert children[3]["status"] == {"code": STATUS_ERROR, "message": "exit 1"}

    top = attrs(root)
    assert root["name"] == "invoke_agent cli" and "parentSpanId" not in root
    assert top["gen_ai.operation.name"] == "invoke_agent"
    assert top["slipway.card.key"] == "EX-12"
    assert top["vcs.ref.head.name"] == "slipway/EX-12-hello-page"
    assert top["vcs.ref.base.revision"] == "a" * 40
    assert top["vcs.ref.head.revision"] == "b" * 40
    assert top["slipway.diff.files_changed"] == 2
    assert (root["startTimeUnixNano"], root["endTimeUnixNano"]) == (str(10 * S), str(19 * S))


def test_file_command_and_connection_events_land_on_the_tool_call_that_caused_them():
    spans = build().otlp()["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[2]["events"][0]["name"] == "slipway.command"
    assert attrs(spans[2]["events"][0]) == {"process.command_line": "pytest -q",
                                            "process.exit.code": 0}
    assert spans[3]["events"][0]["name"] == "slipway.file_change"
    assert attrs(spans[4]["events"][0])["server.address"] == "pypi.org"
    assert [e["name"] for e in spans[0]["events"]] == ["slipway.file_change"]
    assert attrs(spans[0]["events"][0]) == {"slipway.file.path": "README.md",
                                            "slipway.file.action": "created"}


def test_the_same_records_give_the_same_spans():
    assert build().otlp() == build().otlp()


def test_a_trace_draws_a_random_id_when_none_is_given():
    a, b = Trace("cli", "1", "slipway/1-x", "a" * 40), Trace("cli", "1", "slipway/1-x", "a")
    assert a.trace_id != b.trace_id and re.fullmatch("[0-9a-f]{32}", a.trace_id)
    with pytest.raises(ValueError):
        Trace("cli", "1", "b", "a", trace_id="0" * 32)


def test_an_empty_session_gives_a_valid_root_from_its_start_and_end():
    trace = trace_of("managed_agents", "12", "slipway/12-x", "a" * 40, [],
                     start_ns=5 * S, end_ns=9 * S)
    root, = check_otlp(trace.otlp())["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert (root["startTimeUnixNano"], root["endTimeUnixNano"]) == (str(5 * S), str(9 * S))


def test_the_root_covers_the_session_and_every_child():
    trace = trace_of("cli", "EX-12", "b", "a" * 40, RECORDS, trace_id=TRACE_ID,
                     start_ns=5 * S, end_ns=25 * S)
    assert (trace.root.start_ns, trace.root.end_ns) == (5 * S, 25 * S)
    late = trace_of("cli", "EX-12", "b", "a" * 40, RECORDS, trace_id=TRACE_ID,
                    start_ns=11 * S, end_ns=12 * S)
    assert (late.root.start_ns, late.root.end_ns) == (10 * S, 19 * S)


def test_a_session_that_ends_before_it_starts_is_refused():
    with pytest.raises(ValueError):
        trace_of("cli", "1", "b", "a", [], start_ns=2 * S, end_ns=S)


def test_the_root_carries_the_cost():
    trace = trace_of("cli", "EX-12", "b", "a" * 40, RECORDS, trace_id=TRACE_ID, cost=0.42)
    root = check_otlp(trace.otlp())["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert attrs(root)["slipway.cost.usd"] == 0.42
    assert {"key": "slipway.cost.usd", "value": {"doubleValue": 0.42}} in root["attributes"]
    assert attrs(root)["gen_ai.usage.cost"] == 0.42
    whole = trace_of("cli", "EX-12", "b", "a" * 40, [], start_ns=1, end_ns=2, cost=3)
    assert attrs(whole.otlp()["resourceSpans"][0]["scopeSpans"][0]["spans"][0])[
        "slipway.cost.usd"] == 3.0
    assert "slipway.cost.usd" not in attrs(build().otlp()["resourceSpans"][0]
                                           ["scopeSpans"][0]["spans"][0])


def test_a_non_record_is_refused():
    with pytest.raises(TypeError):
        Trace("cli", "1", "b", "a", trace_id=TRACE_ID).add({"type": "tool_call"})


# The one scrub seam (KTD24).

INJECTED = "injected-credential-value-1234"
TOKEN = "ghp_" + "A" * 36


def _takes_str(hint) -> bool:
    return hint is str or str in typing.get_args(hint)


def _full_record(cls):
    """An instance of cls with every field set, each string carrying both secrets."""
    hints = typing.get_type_hints(cls)
    return cls(**{name: f"{name} {INJECTED} {TOKEN}" if _takes_str(hint) else 1
                  for name, hint in hints.items()})


@pytest.mark.parametrize("cls", list(RECORD_TYPES.values()), ids=list(RECORD_TYPES))
def test_record_fields_are_scalars_scrubbed_can_reach(cls):
    # scrubbed reaches top level str fields only. A list or dict field would
    # carry strings past it, so a new one must fail here until scrubbed recurses.
    for name, hint in typing.get_type_hints(cls).items():
        kinds = {a for a in (typing.get_args(hint) or (hint,)) if a is not type(None)}
        assert kinds <= {str, int, float, bool}, (cls.__name__, name, hint)


def test_every_record_type_is_in_the_record_union():
    assert set(RECORD_TYPES.values()) == set(typing.get_args(Record))


@pytest.mark.parametrize("cls", list(RECORD_TYPES.values()), ids=list(RECORD_TYPES))
def test_scrubbed_covers_every_string_field_of_every_record_type(cls):
    record = _full_record(cls)
    strings = [f.name for f in fields(cls) if _takes_str(typing.get_type_hints(cls)[f.name])]
    assert strings, cls
    clean, = scrubbed([record], [INJECTED])
    assert type(clean) is cls
    for name in strings:
        assert getattr(clean, name) == f"{name} {MARK} {MARK}", name
    for f in fields(cls):
        if f.name not in strings:
            assert getattr(clean, f.name) == getattr(record, f.name)
    text = dump_jsonl([clean])
    trace = trace_of("cli", "1", "b", "a" * 40, [clean], start_ns=1, end_ns=2)
    for out in (text, json.dumps(trace.otlp())):
        assert INJECTED not in out and TOKEN not in out


def test_scrubbed_leaves_clean_records_alone():
    assert scrubbed(RECORDS, [INJECTED]) == RECORDS
    assert scrubbed(iter(RECORDS)) == RECORDS


# JSON lines for the side reference.

def test_records_round_trip_through_json_lines():
    text = dump_jsonl(RECORDS)
    assert len(text.splitlines()) == len(RECORDS)
    assert json.loads(text.splitlines()[0])["type"] == "model_request"
    assert load_jsonl(text) == RECORDS


def test_a_line_of_unknown_type_or_field_is_refused():
    with pytest.raises(ValueError):
        from_json_line('{"type": "keystroke"}')
    with pytest.raises(ValueError):
        from_json_line('{"type": "command", "command_line": "ls", "time_ns": 1, "env": "x"}')


def test_the_side_reference_is_outside_refs_heads():
    assert side_reference("EX-12") == "refs/slipway/log/EX-12"
    assert side_reference("12") == SIDE_REFERENCE_PREFIX + "12"
    assert not SIDE_REFERENCE_PREFIX.startswith("refs/heads/")
    assert log_commit_message("EX-12", 7) == "slipway log EX-12 build 7"


@pytest.mark.parametrize("key", ["", "a b", "../x", "x..y", "x.lock", "-x", "x/y", "x~1", "x."])
def test_a_key_that_cannot_name_a_reference_is_refused(key):
    with pytest.raises(ValueError):
        side_reference(key)


# The import rule for this card's two packages.

def test_builders_and_trace_import_only_cards_trackers_config_redact_and_each_other():
    may = {"builders", "trace", "cards", "trackers", "config", "redact"}
    for package in ("builders", "trace"):
        for path in sorted((SRC / "slipway" / package).rglob("*.py")):
            for edge in edges_of(SRC, path):
                assert layer(edge.imported) in may, str(edge)
    for path in sorted((SRC / "slipway" / "trace").rglob("*.py")):
        assert all(layer(e.imported) in ("trace", "redact") for e in edges_of(SRC, path))


@pytest.mark.parametrize("times", [(0, 5), (-5, 5), (1, 0)])
def test_finish_refuses_a_start_or_end_at_or_before_the_epoch(times):
    with pytest.raises(ValueError):
        trace_of("cli", "EX-12", "b", "a" * 40, [], start_ns=times[0], end_ns=times[1])


@pytest.mark.parametrize("cost", [float("nan"), float("inf")])
def test_finish_refuses_a_cost_that_is_not_finite(cost):
    with pytest.raises(ValueError):
        trace_of("cli", "EX-12", "b", "a" * 40, [], start_ns=1, end_ns=2, cost=cost)
