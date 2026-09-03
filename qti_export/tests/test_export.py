"""The exporter, from a stored question document to a content package.

Three fixtures under `fixtures/` are real delivered sets that between them cover every layout the
emitter implements (form / table / note). What is asserted here is what a downstream importer would
trip over, not the exact bytes: the XSD (when the mirror is present), package completeness, the
answer key landing in `correctResponse` and `mapping`, and determinism.
"""

from __future__ import annotations

import copy
import io
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from qti_export import (  # noqa: E402
    ExportRejected,
    export_document,
    normalize_document,
    summarize,
)
from qti_export import validate  # noqa: E402
from qti_export.emitter import CP_NS, QTI_NS, ncname  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_IDS = sorted(p.stem for p in FIXTURES.glob("*.json"))

# Layouts each fixture exercises. Pinned so a fixture swap cannot silently drop a layout from coverage.
EXPECTED_LAYOUTS = {
    "20260808-booking-hotel-45425df4": {"form"},
    "20260809-daily-driving-lessons-bba3fdb8": {"form", "table", "note"},
    "20260809-employment-vacancy-344afebc": {"table", "note"},
}


def load(material_id: str) -> dict:
    return json.loads((FIXTURES / f"{material_id}.json").read_text(encoding="utf-8"))


def q(tag: str) -> str:
    return f"{{{QTI_NS}}}{tag}"


@pytest.fixture(params=FIXTURE_IDS)
def material_id(request):
    return request.param


@pytest.fixture
def bundle(material_id):
    return export_document(load(material_id), material_id=material_id, version_ordinal=1)


# ── coverage of the fixtures themselves ───────────────────────────────────────


def test_fixtures_cover_every_implemented_layout():
    seen = set()
    for material_id in FIXTURE_IDS:
        layouts = {g["layout"] for g in load(material_id)["package"]["question_face"]["groups"]}
        assert layouts == EXPECTED_LAYOUTS[material_id], material_id
        seen |= layouts
    assert seen == {"form", "table", "note"}


# ── item structure ─────────────────────────────────────────────────────────────


def test_every_question_gets_a_response_declaration_with_its_canonical_answer(bundle, material_id):
    doc = load(material_id)
    root = ET.fromstring(bundle.item_xml)
    declared = {rd.get("identifier"): rd for rd in root.findall(q("responseDeclaration"))}
    assert sorted(declared, key=lambda k: int(k.rsplit("_", 1)[1])) == [f"RESPONSE_{n}" for n in range(1, 11)]
    for entry in doc["package"]["answer_key"]:
        rd = declared[f"RESPONSE_{entry['number']}"]
        correct = rd.find(q("correctResponse")).find(q("value")).text
        keys = {e.get("mapKey").casefold() for e in rd.iter(q("mapEntry"))}
        # The canonical answer may have a currency symbol stripped when the paper already prints it,
        # so compare after stripping; either way the correct response must be in its own accept set.
        assert correct.casefold() in keys
        assert entry["canonical"].lstrip("£$€").strip().casefold() == correct.casefold() or (
            entry["canonical"].casefold() == correct.casefold()
        )
        for e in rd.iter(q("mapEntry")):
            assert e.get("caseSensitive") == "false"
            assert e.get("mappedValue") == "1"


def test_every_declaration_has_exactly_one_interaction_and_one_map_response(bundle):
    root = ET.fromstring(bundle.item_xml)
    declared = [rd.get("identifier") for rd in root.findall(q("responseDeclaration"))]
    interactions = [i.get("responseIdentifier") for i in root.iter(q("textEntryInteraction"))]
    mapped = [m.get("identifier") for m in root.iter(q("mapResponse"))]
    assert sorted(interactions) == sorted(declared)
    assert sorted(mapped) == sorted(declared)
    assert root.find(q("outcomeDeclaration")).get("normalMaximum") == "10.0"


def test_identifier_carries_material_id_and_version(bundle, material_id):
    root = ET.fromstring(bundle.item_xml)
    assert root.get("identifier") == f"ielts-{material_id}-v1"
    assert root.get("title").endswith("(v1)")
    assert bundle.item_arcname == f"items/part1-{material_id}-v1.xml"
    assert bundle.zip_filename == f"ielts-{material_id}-v1.zip"


def test_version_ordinal_changes_identifier_and_filenames(material_id):
    v3 = export_document(load(material_id), material_id=material_id, version_ordinal=3)
    assert v3.item_identifier == f"ielts-{material_id}-v3"
    assert v3.item_arcname.endswith("-v3.xml")
    manifest = ET.fromstring(v3.manifest_xml)
    assert manifest.get("identifier") == f"MANIFEST-ielts-{material_id}-v3"


def test_no_audio_is_declared_because_none_is_produced(bundle):
    """A declared-but-absent resource passes the XSD and fails the import. The exporter declares only
    what it puts in the zip."""
    assert b"qh5:audio" not in bundle.item_xml
    assert b"audio pending" in bundle.item_xml
    assert all(href.startswith("items/") and href.endswith(".xml") for href in bundle.manifest_hrefs())


def test_answers_never_leak_into_the_item_body(bundle):
    """Evidence quotes and signposts carry the answer in context. They must not reach the paper."""
    doc_body = ET.fromstring(bundle.item_xml).find(q("itemBody"))
    body_text = " ".join(t for t in doc_body.itertext()).casefold()
    root = ET.fromstring(bundle.item_xml)
    for rd in root.findall(q("responseDeclaration")):
        target = rd.find(q("correctResponse")).find(q("value")).text
        # Single words like "3" or "Visa" can legitimately appear in carrier text; anything longer
        # than a bare token appearing in the body is a leak.
        if len(target.split()) >= 2:
            assert target.casefold() not in body_text, target


# ── the package ───────────────────────────────────────────────────────────────


def test_zip_contains_exactly_what_the_manifest_declares_plus_sidecars(bundle):
    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes())) as zf:
        names = set(zf.namelist())
        assert "imsmanifest.xml" in names
        for href in bundle.manifest_hrefs():
            assert href in names
        assert "reject_candidates.json" in names
        rejects = json.loads(zf.read("reject_candidates.json"))
        assert [row["number"] for row in rejects["items"]] == list(range(1, 11))
        assert all(
            {"canonical", "upstream_alternatives", "qti_added_alternatives"} <= set(row)
            for row in rejects["items"]
        )
        # Every timestamp pinned: the zip must be byte-identical across runs.
        assert {info.date_time for info in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
    assert bundle.missing_resources() == []


def test_upstream_and_qti_added_alternatives_keep_their_provenance():
    doc = load("20260808-booking-hotel-45425df4")
    answer = doc["package"]["answer_key"][1]
    answer["canonical"] = "14 June"
    answer["alternatives"] = ["14th June", "June 14", "14 Jun"]
    question = doc["package"]["question_face"]["questions"][1]
    question["answer_category"] = "date"
    bundle = export_document(doc, material_id="date-provenance")
    sidecar = json.loads(bundle.reject_candidates)
    row = sidecar["items"][1]

    assert row["canonical"] == "14 June"
    assert row["upstream_alternatives"] == ["14th June", "June 14", "14 Jun"]
    assert row["qti_added_alternatives"] == ["June 14th", "14/6"]
    assert row["accept"] == [
        "14 June", "14th June", "June 14", "June 14th", "14 Jun", "14/6",
    ]
    assert any("QTI 在上游 alternatives 之外" in note for note in bundle.review)


def test_two_groups_may_use_the_same_layout():
    doc = load("20260809-employment-vacancy-344afebc")
    groups = doc["package"]["question_face"]["groups"]
    assert len(groups) == 2
    for group in groups:
        numbers = [
            question["number"]
            for question in doc["package"]["question_face"]["questions"]
            if question["group_id"] == group["group_id"]
        ]
        group["layout"] = "form"
        group["structure"] = {"row_labels": [f"Field {number}" for number in numbers]}

    bundle = export_document(doc, material_id="same-layout-two-groups")
    root = ET.fromstring(bundle.item_xml)
    assert len(root.findall(f".//{{{QTI_NS}}}table[@class='ielts-form']")) == 2


def test_a_preprinted_currency_symbol_is_not_accepted_twice():
    doc = load("20260808-booking-hotel-45425df4")
    question = doc["package"]["question_face"]["questions"][2]
    answer = doc["package"]["answer_key"][2]
    question["carrier_before"] = "£"
    question["carrier_after"] = " per night"
    question["answer_category"] = "price"
    answer["canonical"] = "145"
    answer["alternatives"] = ["£145"]
    cross = doc["cross_check"]["items"][2]
    cross["writer_answer"] = "145"
    cross["auditor_answer"] = "145"

    bundle = export_document(doc, material_id="preprinted-currency")
    row = json.loads(bundle.reject_candidates)["items"][2]
    assert row["accept"] == ["145"]
    assert "£145" in row["reject"]
    assert "£145.00" in row["reject"]


def test_manifest_points_at_the_item(bundle, material_id):
    root = ET.fromstring(bundle.manifest_xml)
    resource = root.find(f"{{{CP_NS}}}resources").find(f"{{{CP_NS}}}resource")
    assert resource.get("type") == "imsqti_item_xmlv2p2"
    assert resource.get("href") == bundle.item_arcname
    assert [f.get("href") for f in resource.findall(f"{{{CP_NS}}}file")] == [bundle.item_arcname]
    entry = next(el for el in root.iter() if el.tag.endswith("}entry"))
    assert entry.text == material_id


def test_export_is_deterministic(material_id):
    a = export_document(load(material_id), material_id=material_id)
    b = export_document(load(material_id), material_id=material_id)
    assert a.item_xml == b.item_xml
    assert a.manifest_xml == b.manifest_xml
    assert a.zip_bytes() == b.zip_bytes()


def test_summary_counts_match_the_item(bundle, material_id):
    summary = summarize(bundle).as_dict()
    root = ET.fromstring(bundle.item_xml)
    assert summary["material_id"] == material_id
    assert summary["questions"] == 10
    assert summary["accept_entries"] == len(list(root.iter(q("mapEntry"))))
    assert summary["filename"] == bundle.zip_filename
    assert summary["review"] == bundle.review


# ── XSD (skipped without the local mirror) ────────────────────────────────────


@pytest.mark.skipif(not validate.available(), reason=validate.describe())
def test_item_and_manifest_validate_against_qti_2_2_4_xsd(bundle):
    assert validate.validate_bundle(bundle) == []


# ── admission gates ───────────────────────────────────────────────────────────


def test_a_failed_delivery_document_is_rejected_with_reasons():
    doc = load(FIXTURE_IDS[0])
    doc["ok"] = False
    with pytest.raises(ExportRejected) as info:
        export_document(doc, material_id=FIXTURE_IDS[0])
    assert any("G1" in r and "ok=False" in r for r in info.value.reasons)


def test_a_disagreeing_answer_is_rejected_not_exported_with_the_question_dropped():
    doc = load(FIXTURE_IDS[0])
    doc["cross_check"]["items"][0]["auditor_answer"] = "something else entirely"
    with pytest.raises(ExportRejected) as info:
        export_document(doc, material_id=FIXTURE_IDS[0])
    assert any("G2" in r and "两侧答案不一致" in r for r in info.value.reasons)


def test_a_missing_question_number_is_rejected():
    doc = load(FIXTURE_IDS[0])
    del doc["package"]["question_face"]["questions"][3]
    with pytest.raises(ExportRejected) as info:
        export_document(doc, material_id=FIXTURE_IDS[0])
    assert any("G3" in r for r in info.value.reasons)


def test_a_document_without_a_package_is_rejected():
    with pytest.raises(ExportRejected) as info:
        export_document({"ok": False, "rejected_candidate": {}}, material_id="x")
    assert info.value.reasons == ["这份文档里没有题目包（package）"]


# ── the revision-version shape ────────────────────────────────────────────────


def _as_version(doc: dict, version_id: str = "8d5a0f2e-1111-4222-8333-444455556666") -> dict:
    """What manual_question_revision.py stores: package at the top, the audit under `quality`."""
    return {
        "id": version_id,
        "material_id": "mat",
        "created_at": "2026-08-13T10:00:00Z",
        "status": "ready",
        "package": copy.deepcopy(doc["package"]),
        "quality": {
            "label": "revised-1",
            "package": copy.deepcopy(doc["package"]),
            "review": copy.deepcopy(doc["review"]),
            "cross_check": copy.deepcopy(doc["cross_check"]),
            "validation": copy.deepcopy(doc["validation"]),
            "status": doc["status"],
        },
        "baseline_advisories": ["carried over"],
    }


def test_a_revision_version_is_normalized_to_the_delivery_shape():
    doc = load(FIXTURE_IDS[0])
    normalized = normalize_document(_as_version(doc))
    assert normalized["ok"] is True
    assert normalized["status"] == doc["status"]
    assert normalized["review"] == doc["review"]
    assert normalized["cross_check"] == doc["cross_check"]
    assert normalized["advisories"] == ["carried over"]
    # The delivery shape passes through untouched.
    assert normalize_document(doc) is doc


def test_a_revision_version_exports_under_its_ordinal():
    doc = load(FIXTURE_IDS[0])
    bundle = export_document(_as_version(doc), material_id=FIXTURE_IDS[0], version_ordinal=2)
    assert bundle.item_identifier == f"ielts-{FIXTURE_IDS[0]}-v2"
    assert "carried over" in bundle.review


def test_a_version_not_marked_ready_fails_the_first_gate():
    version = _as_version(load(FIXTURE_IDS[0]))
    version["status"] = "running"
    with pytest.raises(ExportRejected) as info:
        export_document(version, material_id=FIXTURE_IDS[0], version_ordinal=2)
    assert any("G1: ok=False" in r for r in info.value.reasons)


# ── identifiers ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("ielts-20260808-booking-hotel-45425df4-v1", "ielts-20260808-booking-hotel-45425df4-v1"),
    ("ielts-Test 1 Part 1-v2", "ielts-Test-1-Part-1-v2"),
    ("20260808-x", "id-20260808-x"),
    ("", "id-"),
])
def test_ncname_sanitizes_without_colliding_valid_ids(raw, expected):
    assert ncname(raw) == expected
