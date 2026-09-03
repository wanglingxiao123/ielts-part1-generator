"""`GET /api/material-qti/{id}`: the delivered set as a QTI 2.2.4 content package.

The converter itself is tested in qti_export/tests. What is pinned here is the route's contract:
which document it picks for which `version_id`, the three formats, and that every failure is a JSON
error a 命题人员 can read rather than a traceback or a half-built zip.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from audio_storage.object_store import InMemoryObjectStore
from web.app import WebTier
from web.question_versions import QuestionVersionService
from web.slot_state import SlotStateReader
from web.tests.conftest import register

FIXTURE = Path(__file__).resolve().parents[2] / "qti_export" / "tests" / "fixtures"
MATERIAL = "20260808-booking-hotel-45425df4"
VERSION = "8d5a0f2e-1111-4222-8333-444455556666"
QTI_NS = "http://www.imsglobal.org/xsd/imsqti_v2p2"


def delivery() -> dict:
    return json.loads((FIXTURE / f"{MATERIAL}.json").read_text(encoding="utf-8"))


def put(store, key, value):
    store.put(key, json.dumps(value).encode("utf-8"))


@pytest.fixture
def objects():
    """The bucket. Named `objects`, not `store`: conftest's `store` is the USER store the `auth`
    fixture builds on, and shadowing it hands AuthService an object store that has no users."""
    s = InMemoryObjectStore()
    put(s, f"_questions/{MATERIAL}.json", delivery())
    return s


def revision(doc: dict, *, changed_answer: str = "Morgan-Smith") -> dict:
    """A stored revision version: Q1's answer changed on both sides so the export is visibly different."""
    package = json.loads(json.dumps(doc["package"]))
    cross = json.loads(json.dumps(doc["cross_check"]))
    package["answer_key"][0]["canonical"] = changed_answer
    cross["items"][0]["writer_answer"] = changed_answer
    cross["items"][0]["auditor_answer"] = changed_answer
    return {
        "id": VERSION,
        "material_id": MATERIAL,
        "created_at": "2026-08-13T10:00:00Z",
        "status": "ready",
        "operation": "revise_questions",
        "package": package,
        "quality": {
            "label": "revised-1",
            "package": package,
            "review": doc["review"],
            "cross_check": cross,
            "validation": doc["validation"],
            "status": doc["status"],
        },
        "baseline_advisories": [],
    }


def client_for(objects, auth, runtime, static_dir, *, versions=True):
    from fastapi.testclient import TestClient

    tier = WebTier(
        auth, runtime, str(static_dir),
        slot_state=SlotStateReader(objects),
        question_versions=QuestionVersionService(objects) if versions else None,
    )
    return TestClient(tier.app)


def item_identifier(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert "imsmanifest.xml" in names
        item = next(n for n in names if n.startswith("items/"))
        root = ET.fromstring(zf.read(item))
    assert root.tag == f"{{{QTI_NS}}}assessmentItem"
    return root.get("identifier")


def test_the_default_export_is_a_zip_of_the_delivered_set(objects, auth, runtime, static_dir):
    with client_for(objects, auth, runtime, static_dir) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="ielts-{MATERIAL}-v1.zip"')
    assert response.headers["x-qti-item-identifier"] == f"ielts-{MATERIAL}-v1"
    assert int(response.headers["x-qti-review-count"]) > 0
    assert item_identifier(response.content) == f"ielts-{MATERIAL}-v1"


def test_format_item_is_the_bare_assessment_item(objects, auth, runtime, static_dir):
    with client_for(objects, auth, runtime, static_dir) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}?format=item")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="part1-{MATERIAL}-v1.xml"')
    root = ET.fromstring(response.content)
    assert root.get("identifier") == f"ielts-{MATERIAL}-v1"


def test_format_summary_describes_without_downloading(objects, auth, runtime, static_dir):
    with client_for(objects, auth, runtime, static_dir) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}?format=summary")
    assert response.status_code == 200
    body = response.json()
    assert body["material_id"] == MATERIAL
    assert body["version_ordinal"] == 1
    assert body["item_identifier"] == f"ielts-{MATERIAL}-v1"
    assert body["filename"] == f"ielts-{MATERIAL}-v1.zip"
    assert body["questions"] == 10
    assert isinstance(body["review"], list) and body["review"]


def test_an_unknown_format_is_a_400(objects, auth, runtime, static_dir):
    with client_for(objects, auth, runtime, static_dir) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}?format=pdf")
    assert response.status_code == 400


def test_the_adopted_version_is_exported_by_default(objects, auth, runtime, static_dir):
    """The reader page shows the adopted version; the export must be the same paper."""
    put(objects, f"_question_versions/{MATERIAL}/versions/{VERSION}.json", revision(delivery()))
    put(objects, f"_question_versions/{MATERIAL}/active.json", {"version_id": VERSION})
    with client_for(objects, auth, runtime, static_dir) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}?format=item")
    assert response.status_code == 200
    root = ET.fromstring(response.content)
    assert root.get("identifier") == f"ielts-{MATERIAL}-v2"
    q1 = root.find(f"{{{QTI_NS}}}responseDeclaration[@identifier='RESPONSE_1']")
    assert q1.find(f"{{{QTI_NS}}}correctResponse/{{{QTI_NS}}}value").text == "Morgan-Smith"


def test_version_id_selects_a_historical_version(objects, auth, runtime, static_dir):
    put(objects, f"_question_versions/{MATERIAL}/versions/{VERSION}.json", revision(delivery()))
    put(objects, f"_question_versions/{MATERIAL}/active.json", {"version_id": VERSION})
    with client_for(objects, auth, runtime, static_dir) as client:
        register(client)
        original = client.get(f"/api/material-qti/{MATERIAL}?version_id=original&format=summary")
        revised = client.get(f"/api/material-qti/{MATERIAL}?version_id={VERSION}&format=summary")
    assert original.json()["item_identifier"] == f"ielts-{MATERIAL}-v1"
    assert revised.json()["item_identifier"] == f"ielts-{MATERIAL}-v2"


def test_an_unknown_version_is_a_404(objects, auth, runtime, static_dir):
    with client_for(objects, auth, runtime, static_dir) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}?version_id=no-such-version")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "QUESTION_VERSION_NOT_FOUND"


def test_no_delivered_set_is_a_404_not_an_empty_zip(auth, runtime, static_dir):
    with client_for(InMemoryObjectStore(), auth, runtime, static_dir) as client:
        register(client)
        response = client.get("/api/material-qti/20260808-nothing-here-00000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "QUESTIONS_NOT_FOUND"


def test_a_set_that_fails_the_gates_is_a_422_listing_every_reason(auth, runtime, static_dir):
    """Never a nine-question package: the failing document is refused whole, with the reasons."""
    doc = delivery()
    doc["cross_check"]["items"][2]["auditor_answer"] = "not what the writer said"
    del doc["package"]["answer_key"][9]
    objects = InMemoryObjectStore()
    put(objects, f"_questions/{MATERIAL}.json", doc)
    with client_for(objects, auth, runtime, static_dir) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "QTI_EXPORT_REJECTED"
    reasons = error["detail"]["reasons"]
    assert any(r.startswith("G2") for r in reasons)
    assert any(r.startswith("G3") for r in reasons)


def test_a_stored_failure_has_no_package_and_is_a_422(auth, runtime, static_dir):
    objects = InMemoryObjectStore()
    put(objects, f"_questions/{MATERIAL}.json",
        {"ok": False, "reason": "audit", "rejected_candidate": {"package": {}}})
    with client_for(objects, auth, runtime, static_dir) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}")
    assert response.status_code == 422
    assert response.json()["error"]["detail"]["reasons"] == ["这份文档里没有题目包（package）"]


def test_without_a_version_service_the_original_is_exported_as_v1(objects, auth, runtime, static_dir):
    with client_for(objects, auth, runtime, static_dir, versions=False) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}?format=summary")
    assert response.status_code == 200
    assert response.json()["version_ordinal"] == 1


def test_an_unconfigured_store_is_a_503(auth, runtime, static_dir):
    from fastapi.testclient import TestClient

    tier = WebTier(auth, runtime, str(static_dir), slot_state=SlotStateReader(None))
    with TestClient(tier.app) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "QTI_EXPORT_UNAVAILABLE"


def test_a_refusing_store_is_a_502_in_chinese(auth, runtime, static_dir):
    class Broken:
        def get(self, key):
            raise RuntimeError("AccessDenied")

        def list_keys(self, prefix):
            raise RuntimeError("AccessDenied")

        def head(self, key):
            return None

    from fastapi.testclient import TestClient

    tier = WebTier(auth, runtime, str(static_dir),
                   slot_state=SlotStateReader(Broken()),
                   question_versions=QuestionVersionService(Broken()))
    with TestClient(tier.app) as client:
        register(client)
        response = client.get(f"/api/material-qti/{MATERIAL}")
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "QTI_EXPORT_UNAVAILABLE"
    assert "RuntimeError" not in error["message"]


def test_the_export_route_needs_a_session(client):
    """It hands out the answer key. Pins that it was not added to PUBLIC_API_PATHS."""
    assert client.get(f"/api/material-qti/{MATERIAL}").status_code == 401
