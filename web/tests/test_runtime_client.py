"""The boto3 call's request shape, credential stripping, and SSE line parsing."""

from __future__ import annotations

import json

import pytest

from web.runtime_client import (
    CONNECT_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
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
    """A boto3 stand-in that records every request and replays one body.

    Deliberately hands the SAME body to every call. That is wrong for a fan-out test -- see
    `conftest.FanOutRuntimeClient`, which exists for that -- and right here, where the subject is
    the request shape one call produces.
    """

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


# ── the read timeout, which is the whole reason this client is configured ────


def test_the_read_timeout_bounds_a_whole_material_not_botocore_default():
    """The 60s default is what reported working materials as 未生成.

    Measured on the deployed tier: a child whose response headers took longer than 60s raised
    `ReadTimeoutError: Read timeout on endpoint URL: "None"`, `FanOut._pump` turned it into
    `material_failed` for that slot, and the page said 「有 1 套未能生成」 about a material nothing
    had refused. The same 60s also applied to every mid-stream read, and observed stage gaps reached
    50.8s -- so a slightly slower model call would kill a healthy child at any point in the batch.

    So the read timeout must bound the same thing the platform bounds: ONE material's invocation.
    Asserted against `fanout.PER_MATERIAL_WALL_SECONDS` rather than against 900 so the two cannot
    drift apart -- a read timeout below the wall means the web tier gives up before the platform
    does, which is the bug.
    """
    from web.fanout import PER_MATERIAL_WALL_SECONDS

    assert READ_TIMEOUT_SECONDS >= PER_MATERIAL_WALL_SECONDS
    assert READ_TIMEOUT_SECONDS > 60, "botocore's default is shorter than one material"
    # Connecting is a different question: a slow TCP connect is a network fault, not a slow model.
    assert CONNECT_TIMEOUT_SECONDS < READ_TIMEOUT_SECONDS


def test_the_boto_client_is_built_with_that_timeout_and_no_retries():
    """The constant is only worth anything if it reaches the client.

    Retries are pinned to 1 attempt in the same breath: botocore's default would re-POST `generate`
    after a timeout, and a retried invocation generates and BILLS a second material while the first
    is still running, with only one of them reaching the browser.
    """
    client = AgentCoreRuntimeClient(ARN, region="us-east-1").client()
    config = client.meta.config
    assert config.read_timeout == READ_TIMEOUT_SECONDS
    assert config.connect_timeout == CONNECT_TIMEOUT_SECONDS
    # `total_max_attempts`, because `max_attempts` counts RETRIES: botocore normalises
    # `max_attempts: 1` to `total_max_attempts: 2`, i.e. one retry -- a second billed material for
    # the same slot. This assertion is what catches that confusion.
    assert config.retries["total_max_attempts"] == 1


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
