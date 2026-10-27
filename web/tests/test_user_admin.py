import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from botocore.exceptions import ClientError

from web.auth import AdminRequired, S3UserStore, SESSION_COOKIE
from .conftest import register


def test_roles_apply_to_existing_cookie_and_history_contains_no_secrets(client, tier):
    register(client, "admin@example.com")
    tier.auth.register("teacher@example.com", "password123")
    teacher_cookie = tier.auth.issue_token("teacher@example.com")
    assert client.get("/api/admin/users").status_code == 200
    assert client.post("/api/admin/users/role", json={
        "email": "admin@example.com", "is_admin": False, "expected_admin": True,
    }).status_code == 409
    body = {"email": "teacher@example.com", "is_admin": True, "expected_admin": False}
    assert client.post("/api/admin/users/role", json=body).json()["changed"]
    assert not client.post("/api/admin/users/role", json=body).json()["changed"]
    listing = client.get("/api/admin/users")
    assert listing.json()["history_total"] == 1
    assert "password_hash" not in listing.text
    assert "password123" not in listing.text
    admin_cookie = tier.auth.issue_token("admin@example.com")
    client.cookies.set(SESSION_COOKIE, teacher_cookie)
    assert client.get("/api/admin/prompts").status_code == 200
    assert client.get("/api/admin/users").status_code == 200
    client.cookies.set(SESSION_COOKIE, admin_cookie)
    assert client.post("/api/admin/users/role", json={
        "email": "teacher@example.com", "is_admin": False, "expected_admin": True,
    }).json()["changed"]
    client.cookies.set(SESSION_COOKIE, teacher_cookie)
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/prompts").status_code == 403
    assert client.post("/api/admin/users/role", json=body).status_code == 403
    assert client.get("/api/auth/me").json()["user"]["is_admin"] is False


def test_role_input_validation_and_unregistered_user(client, tier):
    register(client, "admin@example.com")
    tier.auth.register("teacher@example.com", "password123")
    assert client.post("/api/admin/users/role", json={
        "email": "missing@example.com", "is_admin": True, "expected_admin": False,
    }).status_code == 404
    for value in ("false", 1, None):
        assert client.post("/api/admin/users/role", json={
            "email": "teacher@example.com", "is_admin": value, "expected_admin": False,
        }).status_code == 400
    assert client.post("/api/admin/users/role", json={
        "email": "teacher@example.com", "is_admin": True, "expected_admin": True,
    }).status_code == 409
    assert client.get("/api/admin/users").json()["history_total"] == 0


class ConditionalS3:
    """Two clients can read the same revision; exactly one conditional write wins."""
    def __init__(self):
        self.raw = None
        self.version = 0
        self.lock = threading.Lock()
        self.barrier = None
        self.reads_to_race = 0

    def race(self):
        self.barrier = threading.Barrier(2)
        self.reads_to_race = 2

    def get_object(self, **kwargs):
        with self.lock:
            raw, version = self.raw, self.version
            wait = self.reads_to_race > 0
            if wait:
                self.reads_to_race -= 1
        if wait:
            self.barrier.wait(timeout=5)
        if raw is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(raw), "ETag": str(version)}

    def put_object(self, **kwargs):
        with self.lock:
            if ((kwargs.get("IfNoneMatch") == "*" and self.raw is not None)
                    or ("IfMatch" in kwargs and kwargs["IfMatch"] != str(self.version))):
                raise ClientError({"Error": {"Code": "PreconditionFailed"},
                                   "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
            self.raw = kwargs["Body"]
            self.version += 1
            return {"ETag": str(self.version)}


def test_concurrent_first_registration_creates_exactly_one_admin():
    client = ConditionalS3()
    stores = [S3UserStore("test", client=client) for _ in range(2)]
    client.race()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda i: stores[i].create({"email": f"{i}@example.com"}), range(2)))
    assert sum(user["is_admin"] for user in results) == 1
    assert stores[0].count() == 2


def test_two_admins_cannot_concurrently_demote_each_other():
    client = ConditionalS3()
    first, second = (S3UserStore("test", client=client) for _ in range(2))
    first.create({"email": "a@example.com"})
    first.create({"email": "b@example.com"})
    first.set_admin("a@example.com", "b@example.com", True, False)
    client.race()
    def demote(args):
        store, actor, target = args
        try:
            return store.set_admin(actor, target, False, True)
        except AdminRequired:
            return "revoked"
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(demote, [
            (first, "a@example.com", "b@example.com"),
            (second, "b@example.com", "a@example.com"),
        ]))
    assert results.count("revoked") == 1
    users = json.loads(client.raw)["users"]
    assert sum(user["is_admin"] for user in users.values()) == 1
    assert sum(len(user.get("role_history", [])) for user in users.values()) == 2


def test_failed_storage_write_never_changes_role_or_audit(monkeypatch):
    client = ConditionalS3()
    store = S3UserStore("test", client=client)
    store.create({"email": "a@example.com"})
    store.create({"email": "b@example.com"})
    before = client.raw
    def fail(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied"},
                           "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject")
    monkeypatch.setattr(client, "put_object", fail)
    with pytest.raises(ClientError):
        store.set_admin("a@example.com", "b@example.com", True, False)
    assert client.raw == before
