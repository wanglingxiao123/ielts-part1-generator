from __future__ import annotations

import json
import threading
import time

import pytest

from audio_storage.object_store import InMemoryObjectStore
from web import app as app_module
from web.app import WebTier
from web.comment_store import CommentService, InMemoryCommentStore
from web.question_versions import QuestionVersionError, QuestionVersionService
from web.tests.conftest import FakeStreamingBody, register


def put(store, key, value):
    store.put(key, json.dumps(value).encode("utf-8"))


@pytest.fixture
def versions():
    store = InMemoryObjectStore()
    put(store, "_questions/mat-1.json", {"ok": True, "package": {"question_face": {}}})
    return QuestionVersionService(store), store


def test_existing_delivered_questions_are_projected_as_v1_and_active(versions):
    service, _ = versions
    document = service.list("mat-1")
    assert document["active_version_id"] == "original"
    assert [(row["id"], row["ordinal"], row["is_active"])
            for row in document["versions"]] == [("original", 1, True)]


def test_immutable_versions_sort_after_original_and_can_be_adopted(versions):
    service, store = versions
    put(store, "_question_versions/mat-1/versions/later.json", {
        "id": "later", "created_at": "2026-08-11T11:00:00Z",
        "package": {"question_face": {"title": "later"}},
    })
    put(store, "_question_versions/mat-1/versions/earlier.json", {
        "id": "earlier", "created_at": "2026-08-11T10:00:00Z",
        "package": {"question_face": {"title": "earlier"}},
    })
    document = service.adopt("mat-1", "earlier", "reviewer@example.com")
    assert [row["id"] for row in document["versions"]] == [
        "original", "earlier", "later",
    ]
    assert document["active_version_id"] == "earlier"
    assert document["versions"][1]["is_active"] is True


def test_only_one_running_revision_is_reserved_per_material(versions):
    service, store = versions
    first = service.reserve("mat-1", "original", [{"id": "c1"}], "reviewer")
    assert first["status"] == "running"
    # A separate service stands in for another ECS task. The S3 conditional marker, not the
    # process-local lock, must reject it.
    other_task = QuestionVersionService(store)
    with pytest.raises(QuestionVersionError) as found:
        other_task.reserve("mat-1", "original", [{"id": "c2"}], "reviewer")
    assert found.value.code == "QUESTION_REVISION_IN_PROGRESS"


def test_terminal_running_pointer_does_not_block_the_next_request(versions):
    service, store = versions
    first = service.reserve("mat-1", "original", [{"id": "c1"}], "reviewer")
    terminal = dict(first, status="completed", version_id=first["request_id"])
    put(store, "_question_revisions/mat-1/running.json", terminal)
    put(store, "_question_versions/mat-1/versions/%s.json" % first["request_id"], {
        "id": first["request_id"],
        "created_at": "2026-08-11T10:00:00Z",
        "package": {"question_face": {}},
    })
    store.delete = lambda _keys: (_ for _ in ()).throw(
        AssertionError("terminal replacement must not require DeleteObject"))

    second = QuestionVersionService(store).reserve(
        "mat-1", "original", [{"id": "c2"}], "reviewer")
    assert second["request_id"] != first["request_id"]


def test_existing_version_reconciles_a_stale_running_pointer_as_completed(versions):
    service, store = versions
    first = service.reserve("mat-1", "original", [{"id": "c1"}], "reviewer")
    put(store, "_question_versions/mat-1/versions/%s.json" % first["request_id"], {
        "id": first["request_id"],
        "created_at": "2026-08-11T10:00:00Z",
        "baseline_advisories": ["Q9 audit variance"],
        "package": {"question_face": {}},
    })

    document = service.list("mat-1")

    assert document["running_request"] is None
    assert document["revision_request"]["status"] == "completed"
    assert document["revision_request"]["version_id"] == first["request_id"]
    assert document["revision_request"]["baseline_advisories"] == ["Q9 audit variance"]


def test_revision_relay_emits_heartbeats_while_runtime_is_silent(
    versions, monkeypatch,
):
    service, store = versions
    revision = service.reserve("mat-1", "original", [{"id": "c1"}], "reviewer")
    body = FakeStreamingBody()
    monkeypatch.setattr(app_module, "HEARTBEAT_SECONDS", 0.01)
    relay = app_module._relay_question_revision(body, service, "mat-1", revision)

    assert next(relay) == b": hb\n\n"

    version_id = revision["request_id"]
    put(store, "_question_versions/mat-1/versions/%s.json" % version_id, {
        "id": version_id,
        "created_at": "2026-08-12T10:00:00Z",
        "package": {"question_face": {}},
    })
    body.push_event({
        "type": "question_revision_completed",
        "request_id": version_id,
        "version_id": version_id,
    })
    body.finish()
    frame = next(relay)
    assert b"question_revision_completed" in frame
    relay.close()
    assert body.closed
    deadline = time.monotonic() + 1
    while (any(thread.name == "question-revision-sse" for thread in threading.enumerate())
           and time.monotonic() < deadline):
        time.sleep(0.01)
    assert not any(
        thread.name == "question-revision-sse" for thread in threading.enumerate())


def test_sidecar_failure_marks_pointer_failed_instead_of_leaving_a_ghost_lock(
    versions, monkeypatch,
):
    service, store = versions
    original_put = store.put

    def fail_sidecar(key, body, **kwargs):
        if key.startswith("_question_revisions/mat-1/") and key.endswith(".json") \
                and not key.endswith("/running.json"):
            raise RuntimeError("sidecar unavailable")
        return original_put(key, body, **kwargs)

    monkeypatch.setattr(store, "put", fail_sidecar)
    with pytest.raises(RuntimeError, match="sidecar unavailable"):
        service.reserve("mat-1", "original", [{"id": "c1"}], "reviewer")

    document = service.list("mat-1")
    assert document["running_request"] is None
    assert document["revision_request"]["status"] == "failed"


def test_unknown_base_or_adoption_target_is_rejected(versions):
    service, _ = versions
    with pytest.raises(QuestionVersionError) as found:
        service.load("mat-1", "missing")
    assert found.value.status == 404


class _History:
    def get_material(self, material_id):
        if material_id != "mat-1":
            return None
        return {"material": {"content_kind": "listening_material"},
                "blueprint": {"blueprint_schema_version": 2}}


def test_revision_route_snapshots_question_comments_and_relays_runtime(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True,
        "package": {"material_id": "mat-1", "question_face": {},
                    "answer_key": [], "evidence": []},
    })
    versions = QuestionVersionService(backing)
    comments = CommentService(InMemoryCommentStore())
    comments.create("mat-1", {
        "anchor": {"type": "question", "index": 3},
        "severity": "major",
        "text": "The carrier allows two answers.",
    })
    comments.create("mat-1", {
        "anchor": {"type": "turn", "index": 5},
        "severity": "minor",
        "text": "Material wording note.",
    })
    stream = FakeStreamingBody()
    put(backing, "_question_versions/mat-1/versions/new.json", {
        "id": "new",
        "created_at": "2026-08-11T10:00:00Z",
        "based_on_version_id": "original",
        "source_comment_ids": [],
        "status": "ready",
        "package": {"material_id": "mat-1", "question_face": {},
                    "answer_key": [], "evidence": []},
    })
    stream.push_event({"type": "question_revision_completed", "version_id": "new"})
    stream.finish()
    runtime.stream = stream
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        assert register(client).status_code == 200
        response = client.post(
            "/api/material-question-revisions/mat-1",
            json={"base_version_id": "original"},
        )
    assert response.status_code == 200
    assert "question_revision_completed" in response.text
    payload = runtime.calls[-1]
    assert payload["action"] == "revise_questions_from_comments"
    assert payload["base_version_id"] == "original"
    assert payload["base_version"]["id"] == "original"
    assert payload["base_version"]["package"] == payload["package"]
    assert len(payload["comments"]) == 1
    assert payload["comments"][0]["anchor"] == {"type": "question", "index": 3}


def test_revision_route_rejects_a_non_active_base(auth, runtime, static_dir):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True, "package": {"question_face": {}},
    })
    put(backing, "_question_versions/mat-1/versions/v2.json", {
        "id": "v2", "created_at": "2026-08-11T10:00:00Z",
        "package": {"question_face": {}},
    })
    versions = QuestionVersionService(backing)
    versions.adopt("mat-1", "v2", "reviewer")
    comments = CommentService(InMemoryCommentStore())
    comments.create("mat-1", {
        "anchor": {"type": "question", "index": 1},
        "severity": "major", "text": "Please revise.",
    })
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.post(
            "/api/material-question-revisions/mat-1",
            json={"base_version_id": "original"},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BASE_VERSION_NOT_ACTIVE"
    assert runtime.calls == []


def test_revision_stream_without_terminal_keeps_request_locked(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True,
        "package": {"material_id": "mat-1", "question_face": {},
                    "answer_key": [], "evidence": []},
    })
    versions = QuestionVersionService(backing)
    comments = CommentService(InMemoryCommentStore())
    comments.create("mat-1", {
        "anchor": {"type": "question", "index": 1},
        "severity": "major", "text": "Please revise.",
    })
    stream = FakeStreamingBody()
    stream.push_event({"type": "question_revision_validating", "request_id": "req"})
    stream.finish()
    runtime.stream = stream
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.post(
            "/api/material-question-revisions/mat-1",
            json={"base_version_id": "original"},
        )

    assert response.status_code == 200
    assert "question_revision_failed" in response.text
    document = versions.list("mat-1")
    assert document["running_request"] is not None
    assert document["revision_request"]["status"] == "running"


def test_completed_event_without_stored_version_is_downgraded_to_failure(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True,
        "package": {"material_id": "mat-1", "question_face": {},
                    "answer_key": [], "evidence": []},
    })
    versions = QuestionVersionService(backing)
    comments = CommentService(InMemoryCommentStore())
    comments.create("mat-1", {
        "anchor": {"type": "question", "index": 1},
        "severity": "major", "text": "Please revise.",
    })
    stream = FakeStreamingBody()
    stream.push_event({
        "type": "question_revision_completed",
        "request_id": "req",
        "version_id": "missing-version",
    })
    stream.finish()
    runtime.stream = stream
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.post(
            "/api/material-question-revisions/mat-1",
            json={"base_version_id": "original"},
        )

    assert "question_revision_completed" not in response.text
    assert "question_revision_failed" in response.text
    document = versions.list("mat-1")
    assert document["running_request"] is None
    assert document["revision_request"]["status"] == "failed"
