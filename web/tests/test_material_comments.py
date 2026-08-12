from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web.app import WebTier
from web.comment_store import CommentError, CommentService, InMemoryCommentStore

from .conftest import StubRuntimeClient, register


MATERIAL_ID = "20260809-booking-hotel-a1b2c3d4"


def payload(index: int = 3, *, severity: str = "major", text: str = "题面需要调整"):
    return {
        "anchor": {"type": "question", "index": index},
        "severity": severity,
        "text": text,
    }


def test_comments_require_authentication(client):
    assert client.get("/api/material-comments/%s" % MATERIAL_ID).status_code == 401
    assert client.post(
        "/api/material-comments/%s" % MATERIAL_ID, json=payload()
    ).status_code == 401
    assert client.delete(
        "/api/material-comments/%s/comment-1" % MATERIAL_ID
    ).status_code == 401


def test_create_list_and_delete_preserve_other_comments(client):
    register(client)

    first = client.post("/api/material-comments/%s" % MATERIAL_ID, json=payload())
    assert first.status_code == 201
    first_comment = first.json()["comments"][0]
    assert first_comment["id"]
    assert first_comment["created_at"].endswith("Z")
    assert first_comment["text"] == "题面需要调整"

    second = client.post(
        "/api/material-comments/%s" % MATERIAL_ID,
        json={
            "anchor": {"type": "turn", "index": 0},
            "severity": "minor",
            "text": "  开场稍长  ",
            "id": "client-cannot-choose",
            "created_at": "2000-01-01T00:00:00Z",
        },
    )
    assert second.status_code == 201
    assert [comment["text"] for comment in second.json()["comments"]] == [
        "题面需要调整",
        "开场稍长",
    ]

    listed = client.get("/api/material-comments/%s" % MATERIAL_ID)
    assert listed.json() == second.json()

    deleted = client.delete(
        "/api/material-comments/%s/%s" % (MATERIAL_ID, first_comment["id"])
    )
    assert deleted.status_code == 200
    assert [comment["text"] for comment in deleted.json()["comments"]] == ["开场稍长"]


def test_invalid_comment_inputs_are_rejected(client):
    register(client)
    path = "/api/material-comments/%s" % MATERIAL_ID

    cases = [
        ({**payload(), "severity": ""}, "INVALID_SEVERITY"),
        ({**payload(), "text": "   "}, "INVALID_COMMENT"),
        ({**payload(), "anchor": {"type": "question", "index": 11}}, "INVALID_ANCHOR"),
        ({**payload(), "anchor": {"type": "turn", "index": -1}}, "INVALID_ANCHOR"),
        ({**payload(), "anchor": {"type": "overall", "index": 0}}, "INVALID_ANCHOR"),
    ]
    for body, code in cases:
        response = client.post(path, json=body)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == code

    invalid_id = client.get("/api/material-comments/%20")
    assert invalid_id.status_code == 400
    assert invalid_id.json()["error"]["code"] == "INVALID_MATERIAL_ID"


def test_comment_document_survives_web_tier_rebuild(auth, static_dir):
    store = InMemoryCommentStore()
    first = WebTier(
        auth,
        StubRuntimeClient(),
        str(static_dir),
        comments=CommentService(store),
    )
    with TestClient(first.app) as client:
        register(client)
        created = client.post(
            "/api/material-comments/%s" % MATERIAL_ID, json=payload()
        ).json()

    second = WebTier(
        auth,
        StubRuntimeClient(),
        str(static_dir),
        comments=CommentService(store),
    )
    with TestClient(second.app) as client:
        client.post(
            "/api/auth/login",
            json={"email": "a@amazon.com", "password": "hunter2hunter2"},
        )
        assert client.get("/api/material-comments/%s" % MATERIAL_ID).json() == created


def test_legacy_question_comment_is_projected_as_original_and_open():
    store = InMemoryCommentStore()
    store.save(MATERIAL_ID, {
        "material_id": MATERIAL_ID,
        "comments": [{
            "id": "legacy",
            "created_at": "2026-08-09T14:30:00Z",
            "anchor": {"type": "question", "index": 2},
            "severity": "major",
            "text": "旧批注",
        }],
    })

    comment = CommentService(store).list(MATERIAL_ID)["comments"][0]

    assert comment["version_id"] == "original"
    assert comment["status"] == "open"


def test_question_comment_records_requested_version():
    service = CommentService(InMemoryCommentStore())

    document = service.create(MATERIAL_ID, {
        **payload(),
        "version_id": "version-2",
    })

    assert document["comments"][0]["version_id"] == "version-2"
    assert document["comments"][0]["status"] == "open"


def test_revision_settlement_only_updates_matching_snapshot():
    service = CommentService(InMemoryCommentStore())
    source = service.create(MATERIAL_ID, payload(text="用于本次修改"))["comments"][0]
    concurrent = service.create(MATERIAL_ID, payload(4, text="处理中新增"))["comments"][1]
    other_version = service.create(MATERIAL_ID, {
        **payload(5, text="另一版本"),
        "version_id": "version-2",
    })["comments"][2]

    document = service.settle_revision(
        MATERIAL_ID,
        comment_ids=[source["id"], other_version["id"]],
        base_version_id="original",
        request_id="request-1",
        outcome="resolved",
        resolved_by_version_id="version-3",
    )
    by_id = {comment["id"]: comment for comment in document["comments"]}

    assert by_id[source["id"]]["status"] == "resolved"
    assert by_id[source["id"]]["resolved_by_version_id"] == "version-3"
    assert by_id[source["id"]]["revision_request_id"] == "request-1"
    assert by_id[concurrent["id"]]["status"] == "open"
    assert by_id[other_version["id"]]["status"] == "open"


def test_needs_material_comment_is_read_only():
    service = CommentService(InMemoryCommentStore())
    comment = service.create(MATERIAL_ID, payload())["comments"][0]
    service.settle_revision(
        MATERIAL_ID,
        comment_ids=[comment["id"]],
        base_version_id="original",
        request_id="request-2",
        outcome="needs_material",
    )

    with pytest.raises(CommentError) as found:
        service.delete(MATERIAL_ID, comment["id"])

    assert found.value.code == "COMMENT_READ_ONLY"
    assert service.list(MATERIAL_ID)["comments"][0]["status"] == "needs_material"
