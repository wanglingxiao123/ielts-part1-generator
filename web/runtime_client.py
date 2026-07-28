"""The one AWS call this tier makes: ``bedrock-agentcore:InvokeAgentRuntime``.

Verified against the botocore model shipped in `.venv-backend` (service `bedrock-agentcore`,
operation `InvokeAgentRuntime`):

- required members are ``agentRuntimeArn`` and ``payload``; ``runtimeSessionId`` is optional but
  carries ``{'min': 33, 'max': 256}``. A uuid4 hex is 32 characters and is rejected -- one short.
  `new_session_id` prefixes it, which is why it exists at all.
- the output member ``response`` is a blob, delivered as a botocore ``StreamingBody``.
- ``contentType`` on the response tells streaming from unary. The Runtime SDK sets
  ``text/event-stream`` whenever the entrypoint is an async generator
  (`bedrock_agentcore/runtime/app.py`), i.e. exactly the ``generate`` action.

SigV4 needs no code: boto3 signs with whatever credentials the environment provides, and on
Fargate that is the task role via the container credential endpoint. There are no long-lived keys
anywhere in this tier, and the browser never sees a credential of any kind.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Iterator, Optional, Tuple

SSE_CONTENT_TYPE = "text/event-stream"

# Payload keys that must never leave this tier. The Runtime authenticates as its own execution
# role, so forwarded credentials would be ignored at best; at worst a caller uses the proxy to
# smuggle its own keys into a place they get logged. Neither is acceptable, so they are dropped.
_CREDENTIAL_KEYS = frozenset({
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "aws_security_token",
    "accesskeyid",
    "secretaccesskey",
    "sessiontoken",
    "credentials",
    "authorization",
    "x-amz-security-token",
    "role_arn",
    "profile",
    "aws_profile",
})


class RuntimeNotConfigured(RuntimeError):
    """No `AGENT_RUNTIME_ARN`. Raised on invoke, not at import, so `/healthz` still answers."""


def new_session_id(prefix: str = "ielts") -> str:
    """A session id that satisfies the 33-character minimum.

    ``ielts-`` + 32 hex = 38. Do not "simplify" this to `uuid4().hex`: the API rejects 32 with a
    validation error at call time, which is a runtime failure rather than a test failure.
    """
    candidate = "%s-%s" % (prefix, uuid.uuid4().hex)
    if len(candidate) < 33:  # only reachable if someone passes a tiny prefix
        candidate = candidate + uuid.uuid4().hex
    return candidate[:256]


def strip_credentials(payload: Any) -> Any:
    """Recursively drop credential-looking keys. Structure is otherwise preserved verbatim.

    Case- and separator-insensitive, because `AWS_SECRET_ACCESS_KEY`, `awsSecretAccessKey` and
    `aws-secret-access-key` are the same key as far as an attacker is concerned.
    """
    if isinstance(payload, dict):
        cleaned: Dict[str, Any] = {}
        for key, value in payload.items():
            flat = str(key).replace("-", "_").replace(" ", "_").lower()
            if flat in _CREDENTIAL_KEYS or flat.replace("_", "") in _CREDENTIAL_KEYS:
                continue
            cleaned[key] = strip_credentials(value)
        return cleaned
    if isinstance(payload, list):
        return [strip_credentials(item) for item in payload]
    return payload


class AgentCoreRuntimeClient:
    """Thin wrapper. Everything hard about this call is a constraint, not logic."""

    def __init__(self, runtime_arn: Optional[str] = None, *, region: Optional[str] = None,
                 qualifier: Optional[str] = None, client: Any = None) -> None:
        self.runtime_arn = runtime_arn or os.environ.get("AGENT_RUNTIME_ARN") or ""
        self.region = region or os.environ.get("AWS_REGION") or "us-east-1"
        self.qualifier = qualifier or os.environ.get("AGENT_RUNTIME_QUALIFIER") or ""
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.runtime_arn)

    def client(self) -> Any:
        """Built on first use and cached.

        Lazy so that importing this module -- and thus running the test suite -- needs no
        credentials, and so that a missing Runtime does not stop the container from booting.
        """
        if self._client is None:
            import boto3  # noqa: PLC0415 - lazy on purpose

            self._client = boto3.client("bedrock-agentcore", region_name=self.region)
        return self._client

    def invoke(self, payload: Dict[str, Any], *, session_id: Optional[str] = None
               ) -> Tuple[str, Any, str]:
        """Returns ``(content_type, streaming_body, session_id)``.

        The call itself is synchronous and returns as soon as headers arrive; the body is then
        read lazily. That is what makes progressive SSE relay possible at all -- see
        `iter_sse_payloads`.

        Reusing a `session_id` keeps the same warm microVM and resets its idle timer; a new one
        means a cold start. The caller decides, which is why it is a parameter.
        """
        if not self.configured:
            raise RuntimeNotConfigured(
                "AGENT_RUNTIME_ARN is not set; the web tier has no Runtime to call"
            )
        resolved = session_id or new_session_id()
        request: Dict[str, Any] = {
            "agentRuntimeArn": self.runtime_arn,
            "runtimeSessionId": resolved,
            "payload": json.dumps(strip_credentials(payload)).encode("utf-8"),
        }
        if self.qualifier:
            request["qualifier"] = self.qualifier
        response = self.client().invoke_agent_runtime(**request)
        return (
            str(response.get("contentType") or "application/json"),
            response.get("response"),
            str(response.get("runtimeSessionId") or resolved),
        )


def iter_sse_payloads(body: Any, chunk_size: int = 10) -> Iterator[str]:
    """Yield the JSON text behind each ``data:`` line of a Runtime SSE response.

    ``chunk_size=10`` is the documented value and it is not a performance knob: `iter_lines`
    only emits a line once it has read a chunk that completes it, so a large chunk size makes the
    reader wait for bytes the Runtime has not sent yet, and progressive delivery quietly becomes
    buffered delivery. Small chunks cost a few extra reads on a socket that is idle anyway.

    Comment frames (``:``) and blank separators are dropped; only payload text is yielded, so the
    caller owns re-framing.
    """
    for raw in body.iter_lines(chunk_size=chunk_size):
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            yield line[5:].lstrip()
            continue
        # Non-SSE line on an event-stream response: pass it through rather than discard it, so a
        # Runtime error body printed as plain text still reaches the browser.
        yield line


def read_json(body: Any) -> Any:
    """Whole-body read for the unary actions (`list_scenarios`, `select`, `audio_status`, ...).

    A non-JSON body is wrapped in the frontend's error shape instead of raising: the caller is a
    browser that expects `{"error": {...}}`, and a 500 with an HTML traceback tells it nothing.
    """
    raw = body.read() if hasattr(body, "read") else bytes(body or b"")
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {"error": {"code": "runtime_bad_payload",
                          "message": "Runtime returned a non-JSON body: %s" % text[:300]}}
