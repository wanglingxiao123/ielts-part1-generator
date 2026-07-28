"""The HTTP surface: the /api/* gate, the auth endpoints, the proxy, and static/SPA serving."""

from __future__ import annotations

import json

import pytest

from web.app import PUBLIC_API_PATHS, SESSION_REUSE_SECONDS, WebTier, build_tier
from web.auth import SESSION_COOKIE, MemoryUserStore, SessionSigner, SigningKeyUnavailable

from .conftest import FakeStreamingBody, StubRuntimeClient, register


# ── the unauthenticated gate ─────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "/api/invocations",
    "/api/anything",
    "/api/auth/../invocations",
    "/api/admin/users",
])
def test_api_requires_a_session(client, path):
    assert client.post(path, json={}).status_code == 401


def test_401_body_uses_the_frontend_error_shape(client):
    body = client.post("/api/invocations", json={}).json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert body["error"]["message"]


def test_unauthenticated_request_never_reaches_the_runtime(client, runtime):
    client.post("/api/invocations", json={"action": "generate"})
    assert runtime.calls == [], "the gate must short-circuit before the proxy"


def test_a_tampered_cookie_is_rejected_by_the_gate(client, tier):
    register(client)
    good = tier.auth.issue_token("a@amazon.com")
    payload, signature = good.rsplit(".", 1)
    client.cookies.set(SESSION_COOKIE, "%s.%sX" % (payload, signature[:-1]))
    assert client.post("/api/invocations", json={"action": "x"}).status_code == 401


def test_an_expired_cookie_is_rejected_by_the_gate(auth, runtime, static_dir):
    """Same request, same key -- only the clock differs."""
    auth.signer = SessionSigner(b"k" * 32, ttl_seconds=-1)
    tier = WebTier(auth, runtime, str(static_dir))
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        assert client.post("/api/invocations", json={"action": "x"}).status_code == 401


def test_a_cookie_signed_with_another_key_is_rejected(client, static_dir, runtime):
    """The multi-instance mismatch, at the HTTP layer."""
    register(client)
    other = SessionSigner(b"a-different-instances-key").issue("a@amazon.com")
    client.cookies.set(SESSION_COOKIE, other)
    assert client.post("/api/invocations", json={"action": "x"}).status_code == 401


def test_the_public_allowlist_is_only_the_auth_endpoints():
    assert PUBLIC_API_PATHS == {
        "/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/auth/me",
    }


def test_healthz_is_open(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["static"] is True


# ── register / login / logout / me ───────────────────────────────────────────


def test_register_then_use_the_api(client, runtime):
    assert register(client).status_code == 200
    assert client.post("/api/invocations", json={"action": "list_scenarios"}).status_code == 200
    assert runtime.calls == [{"action": "list_scenarios"}]


def test_register_sets_an_httponly_lax_cookie(client):
    response = register(client)
    header = response.headers["set-cookie"]
    assert SESSION_COOKIE in header
    assert "HttpOnly" in header
    assert "Max-Age=604800" in header  # 7 days
    assert "samesite=lax" in header.lower()
    # Not Secure: the deployment is plain HTTP, so a Secure cookie would never be stored.
    assert "secure" not in header.lower()


def test_cookie_secure_is_opt_in(auth, runtime, static_dir):
    tier = WebTier(auth, runtime, str(static_dir), cookie_secure=True)
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        assert "secure" in register(client).headers["set-cookie"].lower()


def test_register_returns_no_password_hash(client):
    assert "password_hash" not in json.dumps(register(client).json())


def test_first_registration_is_admin_over_http(client):
    assert register(client, "first@amazon.com").json()["user"]["is_admin"] is True
    client.cookies.clear()
    assert register(client, "second@amazon.com").json()["user"]["is_admin"] is False


def test_register_rejects_a_disallowed_domain(auth, runtime, static_dir):
    auth.allowed_domains = ["amazon.com"]
    tier = WebTier(auth, runtime, str(static_dir))
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        denied = register(client, "outsider@gmail.com")
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "EMAIL_DOMAIN_NOT_ALLOWED"
        assert register(client, "insider@amazon.com").status_code == 200


def test_star_domain_allows_anyone(client):
    assert register(client, "someone@wherever.test").status_code == 200


def test_duplicate_registration_is_409(client):
    register(client)
    assert register(client).status_code == 409


def test_short_password_is_400(client):
    assert register(client, "a@amazon.com", "short").status_code == 400


def test_login_flow(client):
    register(client)
    client.cookies.clear()
    assert client.post("/api/invocations", json={}).status_code == 401
    assert client.post(
        "/api/auth/login", json={"email": "a@amazon.com", "password": "hunter2hunter2"}
    ).status_code == 200
    assert client.post("/api/invocations", json={"action": "x"}).status_code == 200


def test_login_with_a_bad_password_is_401(client):
    register(client)
    client.cookies.clear()
    response = client.post(
        "/api/auth/login", json={"email": "a@amazon.com", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_login_with_an_unknown_email_is_401(client):
    assert client.post(
        "/api/auth/login", json={"email": "ghost@amazon.com", "password": "hunter2hunter2"}
    ).status_code == 401


def test_me_reports_the_current_user(client):
    register(client)
    assert client.get("/api/auth/me").json()["user"]["email"] == "a@amazon.com"


def test_me_without_a_session_is_401(client):
    assert client.get("/api/auth/me").status_code == 401


def test_logout_clears_the_cookie_and_closes_the_api(client):
    register(client)
    assert client.post("/api/auth/logout").status_code == 200
    assert client.post("/api/invocations", json={}).status_code == 401


def test_a_malformed_json_body_is_handled(client):
    response = client.post("/api/auth/login", content=b"{not json",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 401  # treated as empty credentials, not a 500


# ── the proxy ────────────────────────────────────────────────────────────────


def test_unary_action_returns_the_runtime_json(client, runtime):
    register(client)
    runtime.json_response = {"scenarios": {"version": 3, "max_batch": 6}}
    body = client.post("/api/invocations", json={"action": "list_scenarios"}).json()
    assert body == {"scenarios": {"version": 3, "max_batch": 6}}


def test_credentials_in_the_request_body_are_stripped_before_the_call(auth, static_dir):
    """Browser body -> the bytes handed to boto3, through the REAL runtime client.

    Deliberately not the stub: stripping lives in `AgentCoreRuntimeClient.invoke`, so a stub
    standing in for it would assert nothing about the actual wire payload. Only the boto3 client
    underneath is faked.
    """
    from web.runtime_client import AgentCoreRuntimeClient

    from .test_runtime_client import ARN, RecordingBotoClient

    boto = RecordingBotoClient()
    boto._body.push_raw(b"{}")
    boto._body.finish()
    tier = WebTier(auth, AgentCoreRuntimeClient(ARN, client=boto), str(static_dir))
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        client.post("/api/invocations", json={
            "action": "generate",
            "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "aws_secret_access_key": "should-not-travel",
            "aws_session_token": "also-not-this",
            "scenarios": ["accommodation-rental"],
        })

    wire = boto.requests[0]["payload"]
    assert json.loads(wire.decode()) == {
        "action": "generate", "scenarios": ["accommodation-rental"],
    }
    assert b"should-not-travel" not in wire
    assert b"AKIAIOSFODNN7EXAMPLE" not in wire


def test_the_same_user_reuses_one_runtime_session(client, runtime):
    """Same session id keeps the microVM warm; a new one is a cold start."""
    register(client)
    for _ in range(3):
        client.post("/api/invocations", json={"action": "list_scenarios"})
    assert len(set(runtime.session_ids)) == 1
    assert len(runtime.session_ids[0]) >= 33


def test_two_users_get_different_runtime_sessions(tier):
    assert tier.session_for("a@amazon.com") != tier.session_for("b@amazon.com")


def test_a_stale_runtime_session_is_rotated(tier):
    first = tier.session_for("a@amazon.com", now=1000.0)
    assert tier.session_for("a@amazon.com", now=1000.0 + SESSION_REUSE_SECONDS - 1) == first
    assert tier.session_for("a@amazon.com", now=1000.0 + SESSION_REUSE_SECONDS + 1) != first


def test_an_unconfigured_runtime_is_503_not_500(client, runtime):
    from web.runtime_client import RuntimeNotConfigured

    register(client)
    runtime.raise_on_invoke = RuntimeNotConfigured("AGENT_RUNTIME_ARN is not set")
    response = client.post("/api/invocations", json={"action": "generate"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RUNTIME_NOT_CONFIGURED"


def test_a_failing_invoke_is_502_with_the_error_shape(client, runtime):
    register(client)
    runtime.raise_on_invoke = RuntimeError("AccessDeniedException: not authorized")
    response = client.post("/api/invocations", json={"action": "generate"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "RUNTIME_INVOKE_FAILED"
    assert "AccessDenied" in response.json()["error"]["message"]


def test_a_non_object_payload_is_400(client):
    register(client)
    assert client.post("/api/invocations", json=[1, 2, 3]).status_code == 400


def test_sse_response_content_type_and_events(client, runtime):
    """Whole-stream correctness. Incremental delivery is asserted in test_sse_relay.py, which
    TestClient cannot show because it drains the body before returning a response."""
    register(client)
    stream = FakeStreamingBody()
    stream.push_event({"type": "batch_started", "total": 1})
    stream.push_event({"type": "batch_completed", "succeeded": 1})
    stream.finish()
    runtime.stream = stream
    response = client.post("/api/invocations", json={"action": "generate"})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    frames = [f for f in response.text.split("\n\n") if f.startswith("data: ")]
    assert [json.loads(f[6:])["type"] for f in frames] == ["batch_started", "batch_completed"]


# ── static files and SPA fallback ────────────────────────────────────────────


def test_index_is_served_to_a_logged_in_user(client):
    register(client)
    response = client.get("/")
    assert response.status_code == 200
    assert "spa" in response.text


def test_spa_fallback_serves_index_for_a_client_route(client):
    register(client)
    for route in ["/batches/abc", "/compare/accommodation-rental", "/quarantine", "/gallery"]:
        response = client.get(route)
        assert response.status_code == 200, route
        assert "spa" in response.text, route


def test_assets_are_served_without_a_session(client):
    """Gating assets would break the login page's own styling."""
    response = client.get("/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_an_anonymous_document_request_is_redirected_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_the_login_page_is_open_and_self_contained(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "/api/auth/login" in response.text
    assert "/api/auth/register" in response.text


def test_path_traversal_cannot_escape_the_static_root(client, static_dir):
    """`..` must not reach a file outside the build directory.

    The secret goes in the static dir's *parent*, which is where a successful traversal would
    land. Note that the HTTP client normalises most `/../` away before the request is even sent
    (measured: only `%2f`-encoded attempts arrive intact), so the test below exercises the
    containment check directly rather than relying on this one.
    """
    (static_dir.parent / "secret.txt").write_text("password=hunter2", encoding="utf-8")
    register(client)
    for attempt in ["/../secret.txt", "/..%2fsecret.txt", "/assets/../../secret.txt",
                    "/..%2F..%2Fsecret.txt"]:
        response = client.get(attempt)
        assert "hunter2" not in response.text, attempt


def test_serve_static_rejects_an_escaping_path(tier, static_dir):
    """The containment check itself, called with a path an HTTP client would have normalised."""
    (static_dir.parent / "secret.txt").write_text("password=hunter2", encoding="utf-8")

    class FakeRequest:
        cookies = {SESSION_COOKIE: ""}

    for escaping in ["../secret.txt", "../../secret.txt", "assets/../../secret.txt"]:
        result = tier._serve_static(escaping, FakeRequest())
        body = getattr(result, "path", "")
        assert "secret.txt" not in str(body), escaping


def test_a_missing_build_is_reported_clearly(auth, runtime, tmp_path):
    tier = WebTier(auth, runtime, str(tmp_path / "no-such-dir"))
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.get("/")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "STATIC_MISSING"


# ── construction from the environment ────────────────────────────────────────


def test_build_tier_refuses_a_shared_store_without_a_secret(monkeypatch):
    """The startup check, reached through the real construction path."""
    monkeypatch.setattr("web.auth.build_user_store",
                        lambda env=None: _SharedMemoryStore())
    with pytest.raises(SigningKeyUnavailable):
        build_tier({"ALLOWED_EMAIL_DOMAINS": "amazon.com"})


class _SharedMemoryStore(MemoryUserStore):
    shared = True


def test_build_tier_reads_the_allowlist_from_the_environment(tmp_path):
    tier = build_tier({
        "ALLOWED_EMAIL_DOMAINS": "amazon.com,qq.com",
        "USER_STORE_PATH": str(tmp_path / "users.json"),
        "WEB_STATIC_DIR": str(tmp_path),
    })
    assert tier.auth.allowed_domains == ["amazon.com", "qq.com"]


def test_build_tier_defaults_to_a_json_file_store(tmp_path):
    tier = build_tier({"USER_STORE_PATH": str(tmp_path / "users.json")})
    assert type(tier.auth.store).__name__ == "JsonFileUserStore"


def test_importing_the_module_constructs_no_aws_client(tmp_path):
    """`web.app` is imported by uvicorn at boot; it must not need credentials."""
    tier = build_tier({"USER_STORE_PATH": str(tmp_path / "u.json")})
    assert tier.runtime._client is None
    assert tier.runtime.configured is False
