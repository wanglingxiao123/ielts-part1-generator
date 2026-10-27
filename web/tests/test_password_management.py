import time

import pytest

from web.auth import AuthService, InvalidSession, InvalidCredentials, SessionSigner, S3UserStore, SESSION_COOKIE
from .conftest import register
from .test_user_admin import ConditionalS3


def test_reset_forces_change_and_revokes_every_old_cookie(client, tier):
    register(client, "admin@example.com", "password123")
    tier.auth.register("teacher@example.com", "password123")
    old = tier.auth.issue_token("teacher@example.com")
    body = {"email": "teacher@example.com", "current_password": "wrong"}
    assert client.post("/api/admin/users/reset-password", json=body).status_code == 400
    body["current_password"] = "password123"
    response = client.post("/api/admin/users/reset-password", json=body)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    temporary = response.json()["temporary_password"]
    listing = client.get("/api/admin/users")
    assert temporary not in listing.text and "password_hash" not in listing.text
    assert listing.json()["history"][0]["kind"] == "password_reset"
    with pytest.raises(InvalidSession):
        tier.auth.identify(old)
    assert client.post("/api/auth/login", json={"email": body["email"], "password": "password123"}).status_code == 401
    login = client.post("/api/auth/login", json={"email": body["email"], "password": temporary})
    assert login.json()["user"]["must_change_password"]
    temporary_cookie = client.cookies.get(SESSION_COOKIE)
    assert client.get("/api/auth/me").status_code == 200
    assert client.get("/api/batch-history").status_code == 403
    assert client.post("/api/invocations", json={}).status_code == 403
    assert client.post("/api/admin/users/reset-password", json=body).status_code == 403
    assert client.post("/api/auth/password", json={"current_password": temporary, "new_password": "newpassword456"}).status_code == 200
    with pytest.raises(InvalidSession):
        tier.auth.identify(temporary_cookie)
    assert client.post("/api/auth/login", json={"email": body["email"], "password": "newpassword456"}).json()["user"]["must_change_password"] is False
    assert client.get("/api/batch-history").status_code == 200
    assert client.post("/api/admin/users/reset-password", json=body).status_code == 403


def test_self_change_validation_and_legacy_cookie(client, tier):
    register(client, "admin@example.com", "password123")
    legacy = tier.auth.signer.issue("admin@example.com")
    assert tier.auth.identify(legacy)["is_admin"]
    for value in (None, "short", "password123", 123):
        assert client.post("/api/auth/password", json={"current_password": "password123", "new_password": value}).status_code == 400
    assert client.post("/api/admin/users/reset-password", json={"email": "admin@example.com", "current_password": "password123"}).status_code == 409
    assert client.post("/api/auth/password", json={"current_password": "password123", "new_password": "anotherpassword"}).status_code == 200
    with pytest.raises(InvalidSession):
        tier.auth.identify(legacy)
    assert client.get("/api/auth/me").status_code == 401


def test_login_snapshot_cannot_mint_new_version_after_reset():
    auth = AuthService(S3UserStore("test", client=ConditionalS3()), SessionSigner(b"test"))
    auth.register("admin@example.com", "password123")
    auth.register("user@example.com", "password123")
    original = auth._authenticate
    def race(email, password):
        snapshot = original(email, password)
        auth.store.change_password("admin@example.com", email, "password123", "temporary123", 0, True)
        return snapshot
    auth._authenticate = race
    _, token = auth.login_session("user@example.com", "password123")
    with pytest.raises(InvalidSession):
        auth.identify(token)
    with pytest.raises(InvalidSession):
        auth.store.change_password("user@example.com", "user@example.com", "temporary123", "newpassword", 0)


def test_expired_temporary_password_cannot_login_or_change(tier):
    auth = tier.auth
    auth.register("admin@example.com", "password123")
    auth.register("user@example.com", "password123")
    auth.store.change_password("admin@example.com", "user@example.com", "password123", "temporary123", 0, True)
    _, token = auth.login_session("user@example.com", "temporary123")
    user = auth.store.get("user@example.com")
    user["temporary_password_expires_at"] = time.time() - 1
    auth.store.put(user)
    with pytest.raises(InvalidCredentials):
        auth.login_session("user@example.com", "temporary123")
    with pytest.raises(InvalidSession):
        auth.identify(token)


def test_concurrent_password_changes_commit_once_and_keep_audit_atomic():
    from concurrent.futures import ThreadPoolExecutor
    client = ConditionalS3()
    store = S3UserStore("test", client=client)
    auth = AuthService(store, SessionSigner(b"test"))
    auth.register("user@example.com", "password123")
    client.race()
    def change(password):
        try:
            S3UserStore("test", client=client).change_password("user@example.com", "user@example.com", "password123", password, 0)
            return "changed"
        except InvalidSession:
            return "stale"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, ["newpassword111", "newpassword222"]))
    assert sorted(outcomes) == ["changed", "stale"]
    record = store.get("user@example.com")
    assert record["session_version"] == 1
    assert len(record["security_history"]) == 1


def test_real_pre_upgrade_cookie_defaults_to_zero(tier):
    import base64
    import json
    tier.auth.register("user@example.com", "password123")
    payload = json.dumps({"sub": "user@example.com", "iat": int(time.time()), "exp": int(time.time()) + 300}).encode()
    token = base64.urlsafe_b64encode(payload).rstrip(b"=").decode() + "." + tier.auth.signer._sign(payload)
    assert tier.auth.identify(token)["email"] == "user@example.com"
    tier.auth.store.change_password("user@example.com", "user@example.com", "password123", "newpassword111", 0)
    with pytest.raises(InvalidSession):
        tier.auth.identify(token)
