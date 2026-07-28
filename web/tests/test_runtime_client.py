"""The boto3 call's request shape, credential stripping, and SSE line parsing."""

from __future__ import annotations

import json

import pytest

from web.runtime_client import (
    SSE_CONTENT_TYPE,
    AgentCoreRuntimeClient,
    RuntimeNotConfigured,
    iter_sse_payloads,
    new_session_id,
    read_json,
    strip_credentials,
)

from .conftest import FakeStreamingBody

ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/ielts_part1_runtime-abc"


class RecordingBotoClient:
    def __init__(self, content_type: str = "application/json", body=None) -> None:
        self.requests = []
        self._content_type = content_type
        self._body = body if body is not None else FakeStreamingBody()

    def invoke_agent_runtime(self, **kwargs):
        self.requests.append(kwargs)
        return {
            "contentType": self._content_type,
            "response": self._body,
            "runtimeSessionId": kwargs.get("runtimeSessionId"),
        }


# ── the 33-character session id floor ────────────────────────────────────────


def test_session_id_clears_the_documented_minimum():
    """botocore's model puts `{'min': 33}` on runtimeSessionId; uuid4().hex is 32."""
    import uuid

    assert len(uuid.uuid4().hex) == 32, "premise: a bare uuid4 hex is one character short"
    for _ in range(50):
        assert 33 <= len(new_session_id()) <= 256


def test_session_ids_are_unique():
    assert len({new_session_id() for _ in range(200)}) == 200


def test_a_tiny_prefix_still_clears_the_floor():
    assert len(new_session_id("x")) >= 33


# ── credential stripping ─────────────────────────────────────────────────────


def test_credentials_are_stripped_from_the_payload():
    cleaned = strip_credentials({
        "action": "generate",
        "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key": "secret",
        "aws_session_token": "token",
        "scenarios": ["accommodation-rental"],
    })
    assert cleaned == {"action": "generate", "scenarios": ["accommodation-rental"]}


def test_stripping_ignores_case_and_separators():
    cleaned = strip_credentials({
        "AWS_SECRET_ACCESS_KEY": "x",
        "aws-session-token": "x",
        "SecretAccessKey": "x",
        "Authorization": "Bearer x",
        "credentials": {"a": 1},
        "role_arn": "arn:aws:iam::1:role/x",
        "keep": "me",
    })
    assert cleaned == {"keep": "me"}


def test_stripping_recurses_into_nested_structures():
    cleaned = strip_credentials({
        "action": "select",
        "options": {"nested": {"aws_secret_access_key": "x", "ttl": 30}},
        "items": [{"aws_session_token": "x", "id": 1}, {"id": 2}],
    })
    assert cleaned == {
        "action": "select",
        "options": {"nested": {"ttl": 30}},
        "items": [{"id": 1}, {"id": 2}],
    }


def test_stripping_leaves_ordinary_payloads_untouched():
    payload = {"action": "generate", "scenarios": ["a", "b"], "counts": {"a": 2},
               "custom_scenario": {"prompt_hint": "x", "count": 1}, "n": None, "ok": True}
    assert strip_credentials(payload) == payload


def test_invoke_sends_the_cleaned_payload():
    """The end-to-end property: credentials never reach the wire."""
    boto = RecordingBotoClient()
    AgentCoreRuntimeClient(ARN, client=boto).invoke(
        {"action": "generate", "aws_secret_access_key": "leaked"}
    )
    sent = json.loads(boto.requests[0]["payload"].decode("utf-8"))
    assert sent == {"action": "generate"}
    assert b"leaked" not in boto.requests[0]["payload"]


# ── the request shape ────────────────────────────────────────────────────────


def test_invoke_request_members():
    boto = RecordingBotoClient()
    client = AgentCoreRuntimeClient(ARN, client=boto)
    _, _, session_id = client.invoke({"action": "list_scenarios"}, session_id="s" * 40)
    request = boto.requests[0]
    assert request["agentRuntimeArn"] == ARN
    assert request["runtimeSessionId"] == "s" * 40
    assert isinstance(request["payload"], bytes)
    assert "qualifier" not in request  # omitted unless configured
    assert session_id == "s" * 40


def test_qualifier_is_forwarded_when_set():
    boto = RecordingBotoClient()
    AgentCoreRuntimeClient(ARN, qualifier="DEFAULT", client=boto).invoke({"action": "x"})
    assert boto.requests[0]["qualifier"] == "DEFAULT"


def test_invoke_mints_a_valid_session_id_when_none_is_given():
    boto = RecordingBotoClient()
    AgentCoreRuntimeClient(ARN, client=boto).invoke({"action": "x"})
    assert len(boto.requests[0]["runtimeSessionId"]) >= 33


def test_unconfigured_client_raises_rather_than_calling_aws():
    client = AgentCoreRuntimeClient("")
    assert client.configured is False
    with pytest.raises(RuntimeNotConfigured):
        client.invoke({"action": "x"})


def test_content_type_is_reported():
    boto = RecordingBotoClient(content_type="text/event-stream; charset=utf-8")
    content_type, _, _ = AgentCoreRuntimeClient(ARN, client=boto).invoke({"action": "generate"})
    assert SSE_CONTENT_TYPE in content_type


# ── SSE line parsing ─────────────────────────────────────────────────────────


def test_iter_sse_payloads_extracts_data_lines():
    body = FakeStreamingBody()
    body.push_event({"type": "batch_started", "total": 2})
    body.push_event({"type": "stage", "slot_id": "slot-1"})
    body.finish()
    payloads = list(iter_sse_payloads(body))
    assert [json.loads(p)["type"] for p in payloads] == ["batch_started", "stage"]


def test_iter_sse_payloads_drops_comments_and_blanks():
    body = FakeStreamingBody()
    body.push_raw(b": keep-alive")
    body.push_raw(b"")
    body.push_raw(b'data: {"type":"stage"}')
    body.finish()
    assert list(iter_sse_payloads(body)) == ['{"type":"stage"}']


def test_iter_sse_payloads_uses_the_documented_chunk_size():
    """chunk_size=10 is what makes iter_lines emit a line as soon as it completes."""
    body = FakeStreamingBody()
    body.push_raw(b'data: {"a":1}')
    body.finish()
    list(iter_sse_payloads(body))
    assert body.chunk_sizes == [10]


def test_iter_sse_payloads_passes_through_a_non_sse_line():
    """A plain-text Runtime error on an event-stream response must not be swallowed."""
    body = FakeStreamingBody()
    body.push_raw(b"Internal Server Error")
    body.finish()
    assert list(iter_sse_payloads(body)) == ["Internal Server Error"]


# ── unary bodies ─────────────────────────────────────────────────────────────


def test_read_json_parses_a_whole_body():
    body = FakeStreamingBody()
    body.push_raw(b'{"scenarios": {"version": 1}}')
    body.finish()
    assert read_json(body) == {"scenarios": {"version": 1}}


def test_read_json_wraps_a_non_json_body_in_the_error_shape():
    body = FakeStreamingBody()
    body.push_raw(b"<html>502 Bad Gateway</html>")
    body.finish()
    assert read_json(body)["error"]["code"] == "runtime_bad_payload"


def test_read_json_of_an_empty_body():
    body = FakeStreamingBody()
    body.finish()
    assert read_json(body) == {}


def test_boto3_client_construction_matches_the_real_service_model():
    """Guards the member names this module hardcodes against a botocore update.

    Constructs a real client (offline -- no call is made) and reads the operation model, so a
    renamed member fails here rather than at the first deployment invoke.
    """
    boto3 = pytest.importorskip("boto3")
    model = (
        boto3.Session(region_name="us-east-1")
        .client("bedrock-agentcore")
        .meta.service_model.operation_model("InvokeAgentRuntime")
    )
    members = model.input_shape.members
    assert set(model.input_shape.required_members) <= {"agentRuntimeArn", "payload"}
    for name in ("agentRuntimeArn", "payload", "runtimeSessionId", "qualifier"):
        assert name in members, "botocore renamed %s" % name
    assert members["runtimeSessionId"].metadata["min"] == 33
    assert "response" in model.output_shape.members
    assert "contentType" in model.output_shape.members
