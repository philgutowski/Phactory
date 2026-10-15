"""U11 test scenarios for the trace shape and the action log side reference."""

import json
import re

import pytest

from phactory.trace.shape import (
    SIDE_REFERENCE_PREFIX,
    SPAN_KIND_CLIENT,
    SPAN_KIND_INTERNAL,
    STATUS_ERROR,
    Command,
    Connection,
    FileChange,
    ModelRequest,
    ToolCall,
    Trace,
    dump_jsonl,
    from_json_line,
    load_jsonl,
    log_commit_message,
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
    return trace_of("cli", "EX-12", "phactory/EX-12-hello-page", "a" * 40, RECORDS,
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
    body = json.loads(json.dumps(body))  # it must survive JSON as is
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
    assert top["phactory.card.key"] == "EX-12"
    assert top["vcs.ref.head.name"] == "phactory/EX-12-hello-page"
    assert top["vcs.ref.base.revision"] == "a" * 40
    assert top["vcs.ref.head.revision"] == "b" * 40
    assert top["phactory.diff.files_changed"] == 2
    assert (root["startTimeUnixNano"], root["endTimeUnixNano"]) == (str(10 * S), str(19 * S))


def test_file_command_and_connection_events_land_on_the_tool_call_that_caused_them():
    spans = build().otlp()["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[2]["events"][0]["name"] == "phactory.command"
    assert attrs(spans[2]["events"][0]) == {"process.command_line": "pytest -q",
                                            "process.exit.code": 0}
    assert spans[3]["events"][0]["name"] == "phactory.file_change"
    assert attrs(spans[4]["events"][0])["server.address"] == "pypi.org"
    assert [e["name"] for e in spans[0]["events"]] == ["phactory.file_change"]
    assert attrs(spans[0]["events"][0]) == {"phactory.file.path": "README.md",
                                            "phactory.file.action": "created"}


def test_the_same_records_give_the_same_spans():
    assert build().otlp() == build().otlp()


def test_a_trace_draws_a_random_id_when_none_is_given():
    a, b = Trace("cli", "1", "phactory/1-x", "a" * 40), Trace("cli", "1", "phactory/1-x", "a")
    assert a.trace_id != b.trace_id and re.fullmatch("[0-9a-f]{32}", a.trace_id)
    with pytest.raises(ValueError):
        Trace("cli", "1", "b", "a", trace_id="0" * 32)


def test_an_empty_trace_is_still_valid_otlp():
    trace = trace_of("managed_agents", "12", "phactory/12-x", "a" * 40, [])
    trace.root.start_ns = trace.root.end_ns = 1
    check_otlp(trace.otlp())


def test_a_non_record_is_refused():
    with pytest.raises(TypeError):
        Trace("cli", "1", "b", "a", trace_id=TRACE_ID).add({"type": "tool_call"})


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
    assert side_reference("EX-12") == "refs/phactory/log/EX-12"
    assert side_reference("12") == SIDE_REFERENCE_PREFIX + "12"
    assert not SIDE_REFERENCE_PREFIX.startswith("refs/heads/")
    assert log_commit_message("EX-12", 7) == "phactory log EX-12 build 7"


@pytest.mark.parametrize("key", ["", "a b", "../x", "x..y", "x.lock", "-x", "x/y", "x~1", "x."])
def test_a_key_that_cannot_name_a_reference_is_refused(key):
    with pytest.raises(ValueError):
        side_reference(key)


# The import rule for this card's two packages.

def test_builders_and_trace_import_only_cards_trackers_config_redact_and_each_other():
    may = {"builders", "trace", "cards", "trackers", "config", "redact"}
    for package in ("builders", "trace"):
        for path in sorted((SRC / "phactory" / package).rglob("*.py")):
            for edge in edges_of(SRC, path):
                assert layer(edge.imported) in may, str(edge)
    for path in sorted((SRC / "phactory" / "trace").rglob("*.py")):
        assert all(layer(e.imported) == "trace" for e in edges_of(SRC, path))
