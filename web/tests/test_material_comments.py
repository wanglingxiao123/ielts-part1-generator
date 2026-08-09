from __future__ import annotations

from fastapi.testclient import TestClient

from web.app import WebTier
from web.comment_store import CommentService, InMemoryCommentStore

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
