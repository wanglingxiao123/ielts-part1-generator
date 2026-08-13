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


def test_assessment_artifacts_use_one_adopted_material_version(versions):
    service, store = versions
    put(store, "_question_versions/mat-1/versions/v2.json", {
        "id": "v2",
        "created_at": "2026-08-13T10:00:00Z",
        "operation": "revise_material",
        "material": {"title": "version material"},
        "blueprint": {"title": "version blueprint"},
        "package": {"title": "version package"},
        "audio": {"status": "needs_synthesis"},
    })

    artifacts = service.assessment_artifacts("mat-1", "v2", {
        "material": {"title": "original material"},
        "blueprint": {"title": "original blueprint"},
    })

    assert artifacts["material"]["title"] == "version material"
    assert artifacts["blueprint"]["title"] == "version blueprint"
    assert artifacts["package"]["title"] == "version package"
    assert artifacts["base_version"]["id"] == "v2"


def test_incomplete_material_version_does_not_mix_original_artifacts(versions):
    service, store = versions
    put(store, "_question_versions/mat-1/versions/v2.json", {
        "id": "v2",
        "created_at": "2026-08-13T10:00:00Z",
        "operation": "revise_material",
        "material": {"title": "version material"},
        "package": {"title": "version package"},
        "audio": {"status": "needs_synthesis"},
    })

    with pytest.raises(QuestionVersionError) as found:
        service.assessment_artifacts("mat-1", "v2", {
            "material": {"title": "original material"},
            "blueprint": {"title": "original blueprint"},
        })

    assert found.value.code == "MATERIAL_ARTIFACTS_MISSING"


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


def test_revision_action_is_server_projected_and_expires_after_adoption(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 1},
        "text": "Change the group layout.",
    }], "reviewer")
    terminal = dict(
        source,
        status="replan_questions",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "replan_scope": "layout_only",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)
    put(store, "_question_revisions/mat-1/running.json", terminal)

    actionable = service.list("mat-1")
    assert actionable["available_action"] == "confirm_replan"
    assert actionable["action_source_request_id"] == source["request_id"]
    assert actionable["action_unavailable_reason"] is None
    assert "available_action" not in actionable["revision_request"]

    put(store, "_question_versions/mat-1/versions/v2.json", {
        "id": "v2",
        "created_at": "2026-08-13T10:00:00Z",
        "package": {"question_face": {}},
    })
    service.adopt("mat-1", "v2", "reviewer")

    expired = service.list("mat-1")
    assert expired["available_action"] is None
    assert expired["action_source_request_id"] is None
    assert "当前采用版本" in expired["action_unavailable_reason"]


def test_failed_material_execution_projects_retry_from_original_source(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 1},
        "text": "Change the listening script.",
    }], "reviewer")
    decision = dict(
        source,
        status="needs_material_revision",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "revise_material",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], decision)
    put(store, "_question_revisions/mat-1/running.json", decision)
    execution = service.reserve_material_revision(
        "mat-1", source["request_id"], "reviewer")
    service.fail_request("mat-1", execution, "model unavailable")

    projected = service.list("mat-1")
    assert projected["available_action"] == "retry_material"
    assert projected["action_source_request_id"] == source["request_id"]
    assert projected["action_unavailable_reason"] is None


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


def test_replan_reservation_reuses_actionable_source_snapshot_without_open_comments(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [
        {"id": "c1", "anchor": {"type": "question", "index": 1}, "text": "local"},
        {"id": "c2", "anchor": {"type": "question", "index": 2}, "text": "replan"},
        {"id": "c3", "anchor": {"type": "question", "index": 3}, "text": "wrong"},
    ], "reviewer")
    terminal = dict(
        source,
        status="replan_questions",
        comment_outcomes=[
            {"comment_id": "c1", "outcome": "question_only"},
            {
                "comment_id": "c2",
                "outcome": "replan_questions",
                "replan_scope": "layout_only",
            },
            {"comment_id": "c3", "outcome": "no_change"},
        ],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)
    put(store, "_question_revisions/mat-1/running.json", terminal)

    execution = service.reserve_replan("mat-1", source["request_id"], "reviewer")

    assert execution["operation"] == "replan_questions"
    assert execution["source_request_id"] == source["request_id"]
    assert [row["id"] for row in execution["source_comments"]] == ["c1", "c2"]
    assert execution["source_comments"][1]["replan_scope"] == "layout_only"


def test_replan_reservation_is_idempotent_for_one_source_decision(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 1},
        "text": "Change the group layout.",
    }], "reviewer")
    terminal = dict(
        source,
        status="replan_questions",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "reason": "new layout required",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)
    put(store, "_question_revisions/mat-1/running.json", terminal)

    first = service.reserve_replan("mat-1", source["request_id"], "reviewer")
    second = service.reserve_replan("mat-1", source["request_id"], "reviewer")

    assert second["request_id"] == first["request_id"]
    assert first["source_comments"][0]["replan_scope"] == "retarget"


def test_failed_replan_execution_can_retry_the_same_source_decision(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 1},
        "text": "Change the group layout.",
    }], "reviewer")
    terminal = dict(
        source,
        status="replan_questions",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "reason": "new layout required",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)
    put(store, "_question_revisions/mat-1/running.json", terminal)
    first = service.reserve_replan("mat-1", source["request_id"], "reviewer")
    service.fail_request("mat-1", first, "model unavailable")

    retry = service.reserve_replan("mat-1", source["request_id"], "reviewer")

    assert retry["request_id"] != first["request_id"]
    assert retry["source_request_id"] == source["request_id"]


def test_material_revision_reservation_is_idempotent(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 1},
        "text": "The script must change.",
    }], "reviewer")
    terminal = dict(
        source,
        status="needs_material_revision",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "revise_material",
            "reason": "script change required",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)
    put(store, "_question_revisions/mat-1/running.json", terminal)

    first = service.reserve_material_revision(
        "mat-1", source["request_id"], "reviewer")
    second = service.reserve_material_revision(
        "mat-1", source["request_id"], "reviewer")

    assert first["request_id"] == second["request_id"]
    assert first["operation"] == "revise_material"
    assert first["source_request_id"] == source["request_id"]


def test_material_revision_reservation_accepts_replan_escalation(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 6},
        "text": "Change the group layout.",
    }], "reviewer")
    classified = dict(
        source,
        status="replan_questions",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "replan_scope": "retarget",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], classified)
    put(store, "_question_revisions/mat-1/running.json", classified)
    execution = service.reserve_replan(
        "mat-1", source["request_id"], "reviewer")
    escalation = dict(
        execution,
        status="needs_material_revision",
        failure_phase="feasibility",
        failure_code="MATERIAL_INFEASIBLE",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "replan_scope": "retarget",
            "reason": "the unchanged script cannot support a valid question set",
        }],
    )
    put(
        store,
        "_question_revisions/mat-1/%s.json" % execution["request_id"],
        escalation,
    )
    put(store, "_question_revisions/mat-1/running.json", escalation)

    first = service.reserve_material_revision(
        "mat-1", execution["request_id"], "reviewer")
    second = service.reserve_material_revision(
        "mat-1", execution["request_id"], "reviewer")

    assert first["request_id"] == second["request_id"]
    assert first["operation"] == "revise_material"
    assert first["source_request_id"] == execution["request_id"]
    assert [row["id"] for row in first["source_comments"]] == ["c1"]


def test_material_revision_source_rejects_ordinary_replan_decision(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 6},
        "text": "Change the group layout.",
    }], "reviewer")
    terminal = dict(
        source,
        status="replan_questions",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)

    with pytest.raises(QuestionVersionError) as found:
        service.material_revision_source("mat-1", source["request_id"])

    assert found.value.code == "MATERIAL_REVISION_NOT_AVAILABLE"


def test_material_revision_source_rejects_unlinked_replan_escalation(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 6},
        "text": "Change the group layout.",
    }], "reviewer")
    escalation = dict(
        source,
        operation="replan_questions",
        status="needs_material_revision",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "revise_material",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], escalation)

    with pytest.raises(QuestionVersionError) as found:
        service.material_revision_source("mat-1", source["request_id"])

    assert found.value.code == "MATERIAL_REVISION_SOURCE_INVALID"


@pytest.mark.parametrize("operation", ["revise_material", "unknown_operation"])
def test_material_revision_source_rejects_non_source_operations(
    versions, operation,
):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 6},
        "text": "Change the listening script.",
    }], "reviewer")
    terminal = dict(
        source,
        operation=operation,
        status="needs_material_revision",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "revise_material",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)

    with pytest.raises(QuestionVersionError) as found:
        service.material_revision_source("mat-1", source["request_id"])

    assert found.value.code == "MATERIAL_REVISION_NOT_AVAILABLE"


def test_material_revision_source_rejects_replan_escalation_with_changed_snapshot(
    versions,
):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 6},
        "text": "Change the group layout.",
    }], "reviewer")
    classified = dict(
        source,
        status="replan_questions",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "replan_scope": "retarget",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], classified)
    put(store, "_question_revisions/mat-1/running.json", classified)
    execution = service.reserve_replan(
        "mat-1", source["request_id"], "reviewer")
    escalation = dict(
        execution,
        source_comments=[{
            "id": "c1",
            "anchor": {"type": "question", "index": 6},
            "text": "Change unrelated material content.",
            "replan_scope": "retarget",
        }],
        status="needs_material_revision",
        failure_phase="feasibility",
        failure_code="MATERIAL_INFEASIBLE",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "replan_scope": "retarget",
        }],
    )
    put(
        store,
        "_question_revisions/mat-1/%s.json" % execution["request_id"],
        escalation,
    )

    with pytest.raises(QuestionVersionError) as found:
        service.material_revision_source("mat-1", execution["request_id"])

    assert found.value.code == "MATERIAL_REVISION_SOURCE_INVALID"


def test_material_revision_source_rejects_inactive_baseline(versions):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 1},
        "text": "The script must change.",
    }], "reviewer")
    terminal = dict(
        source,
        status="needs_material_revision",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "revise_material",
        }],
    )
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)
    put(store, "_question_versions/mat-1/versions/v2.json", {
        "id": "v2", "created_at": "2026-08-12T10:00:00Z",
        "package": {"question_face": {}},
    })
    service.adopt("mat-1", "v2", "reviewer")

    with pytest.raises(QuestionVersionError) as found:
        service.material_revision_source("mat-1", source["request_id"])

    assert found.value.code == "BASE_VERSION_NOT_ACTIVE"


@pytest.mark.parametrize("change", [
    {"material_id": "other-material"},
    {"request_id": "other-request"},
    {"operation": "replan_questions"},
    {"comment_outcomes": []},
])
def test_replan_source_rejects_an_invalid_durable_decision(versions, change):
    service, store = versions
    source = service.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 1},
        "text": "Change the group layout.",
    }], "reviewer")
    terminal = dict(source)
    terminal.update({
        "status": "replan_questions",
        "comment_outcomes": [{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "reason": "new layout required",
        }],
    })
    terminal.update(change)
    put(store, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)

    with pytest.raises(QuestionVersionError):
        service.replan_source("mat-1", source["request_id"])


class _History:
    def get_material(self, material_id):
        if material_id != "mat-1":
            return None
        return {"material": {"content_kind": "listening_material"},
                "blueprint": {"blueprint_schema_version": 2}}


def test_replan_route_dispatches_from_durable_decision_without_new_comment(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True,
        "package": {"material_id": "mat-1", "question_face": {},
                    "answer_key": [], "evidence": []},
    })
    versions = QuestionVersionService(backing)
    source = versions.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 3},
        "severity": "major",
        "text": "Change the whole form group to notes.",
    }], "reviewer")
    terminal = dict(
        source,
        status="replan_questions",
        reasons=[{
            "comment_id": "c1", "question_number": 3,
            "outcome": "replan_questions", "replan_scope": "layout_only",
            "reason": "new layout required",
        }],
        comment_outcomes=[{
            "comment_id": "c1", "question_number": 3,
            "outcome": "replan_questions", "replan_scope": "layout_only",
            "reason": "new layout required",
        }],
    )
    put(backing, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)
    put(backing, "_question_revisions/mat-1/running.json", terminal)
    stream = FakeStreamingBody()
    stream.push_event({
        "type": "question_revision_needs_material",
        "request_id": "execution",
        "reasons": [],
    })
    stream.finish()
    runtime.stream = stream
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(),
        comments=CommentService(InMemoryCommentStore()),
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.post(
            "/api/material-question-replans/mat-1",
            json={"source_request_id": source["request_id"]},
        )

    assert response.status_code == 200
    payload = runtime.calls[-1]
    assert payload["action"] == "replan_questions_from_comments"
    assert payload["source_request_id"] == source["request_id"]
    assert payload["base_version_id"] == "original"
    assert [row["id"] for row in payload["comments"]] == ["c1"]
    assert payload["comments"][0]["replan_scope"] == "layout_only"


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


def test_revision_route_uses_adopted_material_version_artifacts(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True,
        "package": {"title": "original package"},
    })
    put(backing, "_question_versions/mat-1/versions/v2.json", {
        "id": "v2",
        "created_at": "2026-08-13T10:00:00Z",
        "operation": "revise_material",
        "material": {"title": "version material"},
        "blueprint": {"title": "version blueprint"},
        "package": {"title": "version package"},
        "audio": {"status": "needs_synthesis"},
    })
    versions = QuestionVersionService(backing)
    versions.adopt("mat-1", "v2", "reviewer")
    comments = CommentService(InMemoryCommentStore())
    comments.create("mat-1", {
        "anchor": {"type": "question", "index": 3},
        "severity": "major",
        "text": "Clarify the question.",
        "version_id": "v2",
    })
    stream = FakeStreamingBody()
    stream.push_event({
        "type": "question_revision_failed",
        "request_id": "execution",
        "message": "test stop",
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
            json={"base_version_id": "v2"},
        )

    assert response.status_code == 200
    payload = runtime.calls[-1]
    assert payload["material"]["title"] == "version material"
    assert payload["blueprint"]["title"] == "version blueprint"
    assert payload["package"]["title"] == "version package"
    assert payload["base_version"]["id"] == "v2"


def test_revision_route_excludes_handled_and_other_version_comments(
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
    open_comment = comments.create("mat-1", {
        "anchor": {"type": "question", "index": 1},
        "severity": "major", "text": "Open.",
    })["comments"][0]
    handled = comments.create("mat-1", {
        "anchor": {"type": "question", "index": 2},
        "severity": "major", "text": "Handled.",
    })["comments"][1]
    comments.settle_revision(
        "mat-1",
        comment_ids=[handled["id"]],
        base_version_id="original",
        request_id="old-request",
        outcome="needs_material",
    )
    comments.create("mat-1", {
        "anchor": {"type": "question", "index": 3},
        "severity": "major", "text": "Other version.",
        "version_id": "version-2",
    })
    put(backing, "_question_versions/mat-1/versions/new.json", {
        "id": "new", "created_at": "2026-08-11T10:00:00Z",
        "package": {"question_face": {}},
    })
    stream = FakeStreamingBody()
    stream.push_event({"type": "question_revision_completed", "version_id": "new"})
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
    assert [row["id"] for row in runtime.calls[-1]["comments"]] == [open_comment["id"]]


def test_versions_route_reconciles_completed_revision_comments(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True, "package": {"question_face": {}},
    })
    comments = CommentService(InMemoryCommentStore())
    source = comments.create("mat-1", {
        "anchor": {"type": "question", "index": 1},
        "severity": "major", "text": "Revise.",
    })["comments"][0]
    versions = QuestionVersionService(backing)
    revision = versions.reserve("mat-1", "original", [source], "reviewer")
    version_id = revision["request_id"]
    put(backing, "_question_versions/mat-1/versions/%s.json" % version_id, {
        "id": version_id,
        "created_at": "2026-08-12T10:00:00Z",
        "package": {"question_face": {}},
    })
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.get("/api/material-question-versions/mat-1")

    assert response.status_code == 200
    settled = comments.list("mat-1")["comments"][0]
    assert settled["status"] == "resolved"
    assert settled["resolved_by_version_id"] == version_id


@pytest.mark.parametrize(
    ("status", "expected"),
    [("no_change", "no_change"), ("replan_questions", "needs_replan")],
)
def test_versions_route_reconciles_non_version_terminal_comments(
    auth, runtime, static_dir, status, expected,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True, "package": {"question_face": {}},
    })
    comments = CommentService(InMemoryCommentStore())
    source = comments.create("mat-1", {
        "anchor": {"type": "question", "index": 1},
        "severity": "major", "text": "Review this.",
    })["comments"][0]
    versions = QuestionVersionService(backing)
    revision = versions.reserve("mat-1", "original", [source], "reviewer")
    terminal = dict(
        revision,
        status=status,
        reasons=[{
            "comment_id": source["id"], "question_number": 1,
            "reason": "decision", "references": ["face", "answer", "material"],
        }],
        completed_at="2026-08-12T10:00:00Z",
    )
    put(backing, "_question_revisions/mat-1/running.json", terminal)
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.get("/api/material-question-versions/mat-1")

    assert response.status_code == 200
    settled = comments.list("mat-1")["comments"][0]
    assert settled["status"] == expected
    assert settled["decision_reason"] == "decision"


def test_completed_replan_resolves_the_previous_needs_replan_comment(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True, "package": {"question_face": {}},
    })
    comments = CommentService(InMemoryCommentStore())
    source = comments.create("mat-1", {
        "anchor": {"type": "question", "index": 3},
        "severity": "major", "text": "Change the group layout.",
    })["comments"][0]
    comments.settle_revision(
        "mat-1",
        comment_ids=[source["id"]],
        base_version_id="original",
        request_id="classification",
        outcome="needs_replan",
    )
    execution = {
        "request_id": "replan-execution",
        "material_id": "mat-1",
        "operation": "replan_questions",
        "source_request_id": "classification",
        "status": "completed",
        "base_version_id": "original",
        "source_comments": [source],
        "version_id": "replan-execution",
        "comment_outcomes": [{
            "comment_id": source["id"],
            "question_number": 3,
            "outcome": "replan_questions",
            "reason": "resolved by full replan",
        }],
    }
    put(backing, "_question_revisions/mat-1/running.json", execution)
    put(backing, "_question_versions/mat-1/versions/replan-execution.json", {
        "id": "replan-execution",
        "created_at": "2026-08-12T10:00:00Z",
        "package": {"question_face": {}},
        "blueprint": {"blueprint_schema_version": 2},
    })
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=QuestionVersionService(backing),
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.get("/api/material-question-versions/mat-1")

    assert response.status_code == 200
    settled = comments.list("mat-1")["comments"][0]
    assert settled["status"] == "resolved"
    assert settled["resolved_by_version_id"] == "replan-execution"


def test_replan_material_escalation_projects_top_level_action_and_settles_comment(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True, "package": {"question_face": {}},
    })
    comments = CommentService(InMemoryCommentStore())
    source_comment = comments.create("mat-1", {
        "anchor": {"type": "question", "index": 3},
        "severity": "major",
        "text": "Change the target to information absent from the material.",
    })["comments"][0]
    versions = QuestionVersionService(backing)
    classification = versions.reserve(
        "mat-1", "original", [source_comment], "reviewer")
    classified = dict(
        classification,
        status="replan_questions",
        comment_outcomes=[{
            "comment_id": source_comment["id"],
            "question_number": 3,
            "outcome": "replan_questions",
            "replan_scope": "retarget",
            "reason": "a different information point is required",
        }],
    )
    put(
        backing,
        "_question_revisions/mat-1/%s.json" % classification["request_id"],
        classified,
    )
    put(backing, "_question_revisions/mat-1/running.json", classified)
    comments.settle_revision(
        "mat-1",
        comment_ids=[source_comment["id"]],
        base_version_id="original",
        request_id=classification["request_id"],
        outcome="needs_replan",
    )
    execution = versions.reserve_replan(
        "mat-1", classification["request_id"], "reviewer")
    escalation = dict(
        execution,
        status="needs_material_revision",
        failure_phase="feasibility",
        failure_code="MATERIAL_INFEASIBLE",
        comment_outcomes=[{
            "comment_id": source_comment["id"],
            "question_number": 3,
            "outcome": "replan_questions",
            "replan_scope": "retarget",
            "reason": "the unchanged material cannot support the requested target",
        }],
    )
    put(
        backing,
        "_question_revisions/mat-1/%s.json" % execution["request_id"],
        escalation,
    )
    put(backing, "_question_revisions/mat-1/running.json", escalation)
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.get("/api/material-question-versions/mat-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available_action"] == "confirm_material"
    assert payload["action_source_request_id"] == execution["request_id"]
    assert payload["action_unavailable_reason"] is None
    assert "available_action" not in payload["revision_request"]
    settled = comments.list("mat-1")["comments"][0]
    assert settled["status"] == "needs_material"
    assert settled["revision_request_id"] == execution["request_id"]


def test_versions_route_settles_mixed_snapshot_per_comment_without_false_success(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True, "package": {"question_face": {}},
    })
    comments = CommentService(InMemoryCommentStore())
    created = []
    for index in (1, 2, 3):
        created.append(comments.create("mat-1", {
            "anchor": {"type": "question", "index": index},
            "severity": "major", "text": "Review Q%d." % index,
        })["comments"][-1])
    versions = QuestionVersionService(backing)
    revision = versions.reserve("mat-1", "original", created, "reviewer")
    terminal = dict(
        revision,
        status="replan_questions",
        reasons=[{
            "comment_id": created[1]["id"], "question_number": 2,
            "outcome": "replan_questions", "reason": "new target required",
        }],
        comment_outcomes=[
            {
                "comment_id": created[0]["id"], "question_number": 1,
                "outcome": "question_only", "reason": "local fix not performed",
            },
            {
                "comment_id": created[1]["id"], "question_number": 2,
                "outcome": "replan_questions", "reason": "new target required",
            },
            {
                "comment_id": created[2]["id"], "question_number": 3,
                "outcome": "no_change", "reason": "already correct",
                "references": ["face", "answer", "material"],
            },
        ],
        completed_at="2026-08-12T10:00:00Z",
    )
    put(backing, "_question_revisions/mat-1/running.json", terminal)
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.get("/api/material-question-versions/mat-1")

    assert response.status_code == 200
    by_id = {
        row["id"]: row for row in comments.list("mat-1")["comments"]
    }
    assert by_id[created[0]["id"]]["status"] == "open"
    assert by_id[created[1]["id"]]["status"] == "needs_replan"
    assert by_id[created[1]["id"]]["decision_reason"] == "new target required"
    assert by_id[created[2]["id"]]["status"] == "no_change"
    assert by_id[created[2]["id"]]["decision_references"] == [
        "face", "answer", "material",
    ]


def test_comment_route_rejects_a_historical_question_version(
    auth, runtime, static_dir,
):
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
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.post("/api/material-comments/mat-1", json={
            "anchor": {"type": "question", "index": 1},
            "severity": "major",
            "text": "Historical comment.",
            "version_id": "original",
        })

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMENT_VERSION_NOT_ACTIVE"
    assert comments.list("mat-1")["comments"] == []


def test_comment_route_rejects_a_historical_turn_version(
    auth, runtime, static_dir,
):
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
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.post("/api/material-comments/mat-1", json={
            "anchor": {"type": "turn", "index": 1},
            "severity": "major",
            "text": "Historical material comment.",
            "version_id": "original",
        })

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMENT_VERSION_NOT_ACTIVE"


def test_comment_route_rejects_deleting_from_a_historical_version(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True, "package": {"question_face": {}},
    })
    put(backing, "_question_versions/mat-1/versions/v2.json", {
        "id": "v2", "created_at": "2026-08-11T10:00:00Z",
        "package": {"question_face": {}},
    })
    versions = QuestionVersionService(backing)
    comments = CommentService(InMemoryCommentStore())
    created = comments.create("mat-1", {
        "anchor": {"type": "turn", "index": 1},
        "severity": "major",
        "text": "Original-version comment.",
        "version_id": "original",
    })["comments"][0]
    versions.adopt("mat-1", "v2", "reviewer")
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(), comments=comments,
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.delete(
            "/api/material-comments/mat-1/%s" % created["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMENT_VERSION_NOT_ACTIVE"
    assert comments.list("mat-1")["comments"][0]["id"] == created["id"]


def test_material_reader_projects_selected_material_snapshot(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True, "package": {"question_face": {}},
    })
    put(backing, "_question_versions/mat-1/versions/v2.json", {
        "id": "v2",
        "created_at": "2026-08-12T10:00:00Z",
        "operation": "revise_material",
        "material": {"content_kind": "listening_material", "title": "revised"},
        "blueprint": {"blueprint_schema_version": 2, "items": []},
        "package": {"question_face": {}},
        "audio": {"status": "needs_synthesis", "version_key": "mat-1/v2"},
    })
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(),
        comments=CommentService(InMemoryCommentStore()),
        question_versions=QuestionVersionService(backing),
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.get(
            "/api/batch-history-material/mat-1?version_id=v2")

    assert response.status_code == 200
    body = response.json()
    assert body["material"]["title"] == "revised"
    assert body["assessment_version"]["id"] == "v2"
    assert body["assessment_version"]["is_active"] is False
    assert body["assessment_version"]["audio"]["status"] == "needs_synthesis"


def test_version_list_projects_current_isolated_audio_status(versions):
    service, backing = versions
    put(backing, "_question_versions/mat-1/versions/v2.json", {
        "id": "v2",
        "created_at": "2026-08-12T10:00:00Z",
        "operation": "revise_material",
        "material": {"material_id": "mat-1"},
        "blueprint": {"items": []},
        "package": {"question_face": {}},
        "audio": {"status": "needs_synthesis", "version_key": "mat-1/v2"},
    })
    put(backing, "_assessment_audio/mat-1/v2/status.json", {
        "material_id": "mat-1",
        "assessment_version_id": "v2",
        "version_key": "mat-1/v2",
        "status": "ready",
    })
    put(backing, "_assessment_audio/mat-1/v2/audio/manifest.json", {
        "clips": [],
    })

    document = service.list("mat-1")

    projected = next(row for row in document["versions"] if row["id"] == "v2")
    assert projected["audio"]["status"] == "ready"
    assert projected["audio"]["version_key"] == "mat-1/v2"


def test_material_revision_route_dispatches_durable_source(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True,
        "package": {
            "material_id": "mat-1",
            "question_face": {},
            "answer_key": [],
            "evidence": [],
        },
    })
    versions = QuestionVersionService(backing)
    source = versions.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 3},
        "text": "Change the listening script.",
    }], "reviewer")
    terminal = dict(
        source,
        status="needs_material_revision",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "revise_material",
            "reason": "script change required",
        }],
    )
    put(backing, "_question_revisions/mat-1/%s.json" % source["request_id"], terminal)
    put(backing, "_question_revisions/mat-1/running.json", terminal)
    stream = FakeStreamingBody()
    stream.push_event({
        "type": "question_revision_failed",
        "request_id": "execution",
        "message": "test stop",
    })
    stream.finish()
    runtime.stream = stream
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(),
        comments=CommentService(InMemoryCommentStore()),
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.post(
            "/api/material-revisions/mat-1",
            json={"source_request_id": source["request_id"]},
        )

    assert response.status_code == 200
    payload = runtime.calls[-1]
    assert payload["action"] == "revise_material_from_comments"
    assert payload["base_version_id"] == "original"
    assert payload["source_request_id"] == source["request_id"]


def test_material_revision_route_dispatches_replan_escalation(
    auth, runtime, static_dir,
):
    backing = InMemoryObjectStore()
    put(backing, "_questions/mat-1.json", {
        "ok": True,
        "package": {
            "material_id": "mat-1",
            "question_face": {},
            "answer_key": [],
            "evidence": [],
        },
    })
    versions = QuestionVersionService(backing)
    source = versions.reserve("mat-1", "original", [{
        "id": "c1",
        "anchor": {"type": "question", "index": 6},
        "text": "Change the group layout.",
    }], "reviewer")
    classified = dict(
        source,
        status="replan_questions",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "replan_scope": "retarget",
        }],
    )
    put(backing, "_question_revisions/mat-1/%s.json" % source["request_id"], classified)
    put(backing, "_question_revisions/mat-1/running.json", classified)
    execution = versions.reserve_replan(
        "mat-1", source["request_id"], "reviewer")
    escalation = dict(
        execution,
        status="needs_material_revision",
        failure_phase="feasibility",
        failure_code="MATERIAL_INFEASIBLE",
        comment_outcomes=[{
            "comment_id": "c1",
            "outcome": "replan_questions",
            "replan_scope": "retarget",
            "reason": "the unchanged script cannot support a valid question set",
        }],
    )
    put(
        backing,
        "_question_revisions/mat-1/%s.json" % execution["request_id"],
        escalation,
    )
    put(backing, "_question_revisions/mat-1/running.json", escalation)
    stream = FakeStreamingBody()
    stream.push_event({
        "type": "question_revision_failed",
        "request_id": "material-execution",
        "message": "test stop",
    })
    stream.finish()
    runtime.stream = stream
    tier = WebTier(
        auth, runtime, str(static_dir), history=_History(),
        comments=CommentService(InMemoryCommentStore()),
        question_versions=versions,
    )
    from fastapi.testclient import TestClient

    with TestClient(tier.app) as client:
        register(client)
        response = client.post(
            "/api/material-revisions/mat-1",
            json={"source_request_id": execution["request_id"]},
        )

    assert response.status_code == 200
    payload = runtime.calls[-1]
    assert payload["action"] == "revise_material_from_comments"
    assert payload["base_version_id"] == "original"
    assert payload["source_request_id"] == execution["request_id"]
    assert [row["id"] for row in payload["comments"]] == ["c1"]


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
