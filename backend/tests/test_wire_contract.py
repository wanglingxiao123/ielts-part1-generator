"""The wire contract the frontend reads. Pinned before the backend is rewritten.

The refactor's hard constraint is that the frontend must not break, and the frontend is not covered
by these tests -- it consumes an SSE stream in another repository directory with its own suite. So
the contract has to be asserted from this side, and asserted *first*: a rewrite that renames a stage
event or drops a field from `material_completed` produces no Python error at all. The page simply
stops updating, or renders an empty card, and the cause is a field name nobody noticed changing.

Every name here was read off the frontend source (`frontend/src`), not chosen. When one of these
tests fails, the correct response is to ask whether the frontend was changed to match -- not to
update the expected value.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend" / "src"

# Stage events the frontend switches on. Grepped out of the frontend, so this list is evidence about
# a consumer rather than a wish about a producer.
FRONTEND_STAGE_EVENTS = frozenset({
    "batch_started",
    "batch_completed",
    "generating",
    "validating",
    "regenerating",

    "auditing",
    "audited",
    "revising",
    "re_auditing",
    "anchors_repaired",
    "infra_retry",
    "refilling",
    "material_completed",
    "material_failed",
})

# Emitted by the backend but referenced nowhere in the frontend: observability only. Measured, not
# assumed -- `grep -rn validation_reported frontend/src` returns nothing. Listed here rather than
# left out so that the distinction is recorded: renaming one of these breaks nobody, renaming one
# above breaks the page silently.
# `feasibility_checked` belongs here and not above, and the choice is forced rather than stylistic:
# the list above is asserted in BOTH directions, so a name the frontend does not yet reference would
# fail `test_the_frontend_really_does_reference_these_names`. Displaying the verdict to a user is
# §6.1 stage 11; until then this event exists for an operator reading the stream.
BACKEND_ONLY_EVENTS = frozenset({"validation_reported", "feasibility_checked"})

# Keys on a successful `material_completed` payload. Present-but-empty is fine; absent is not,
# because the frontend reads one shape and an absent key cannot be distinguished from "clean".
MATERIAL_COMPLETED_KEYS = frozenset({
    "slot_id", "scenario", "ok", "material_id", "scenario_key", "group_key",
    "material", "blueprint", "audit", "cross_check", "selected_version", "route",
    "note", "degraded", "degraded_reason", "refill_rounds", "anchor_repairs",
    "warnings", "validation_findings", "timings",
})

FAILURE_KEYS = frozenset({"slot_id", "scenario", "ok", "reason", "detail", "refill_rounds",
                          "timings"})


def _frontend_text() -> str:
    parts = []
    for path in FRONTEND.rglob("*.ts"):
        parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    for path in FRONTEND.rglob("*.tsx"):
        parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def _backend_text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in (REPO / "backend").rglob("*.py")
        if "tests" not in p.parts and "__pycache__" not in p.parts
    )


class TestStageEvents:
    def test_every_event_the_frontend_handles_is_still_produced(self):
        """Grep both sides and compare.

        Source-level rather than behavioural because several of these events only fire on paths that
        need a live model (a regeneration, an infra retry). A test that could only see the happy path
        would let the rare ones be renamed silently -- and the rare ones are exactly the ones nobody
        exercises by hand before shipping.
        """
        backend_text = _backend_text()
        missing = sorted(name for name in FRONTEND_STAGE_EVENTS
                         if '"%s"' % name not in backend_text)
        assert missing == [], (
            "the frontend handles these events but the backend no longer emits them: %s" % missing
        )

    def test_the_frontend_really_does_reference_these_names(self):
        """Guards the list itself.

        Without this, someone could keep a stale name here forever and the test above would enforce
        a contract no consumer holds.
        """
        text = _frontend_text()
        absent = sorted(name for name in FRONTEND_STAGE_EVENTS if name not in text)
        assert absent == [], "these are pinned but the frontend never mentions them: %s" % absent

    def test_backend_only_events_really_are_unused_by_the_frontend(self):
        """The other half of the split, so the classification cannot rot unnoticed.

        If the frontend starts consuming one of these, this test fails and it should be promoted into
        FRONTEND_STAGE_EVENTS -- which is what stops a real contract from being treated as internal.
        """
        text = _frontend_text()
        now_used = sorted(name for name in BACKEND_ONLY_EVENTS if name in text)
        assert now_used == [], (
            "the frontend now consumes %s; move it into FRONTEND_STAGE_EVENTS" % now_used
        )

    def test_backend_only_events_are_actually_emitted(self):
        """The positive half, and without it this set is a permanently-green assertion.

        The test above only checks these names are ABSENT from the frontend, which a typo satisfies
        perfectly: `feasibilty_checked` is absent from the frontend too. So a renamed or deleted
        observability event would leave the set unchanged, still passing, still read as coverage --
        exactly the shape `guards.assert_carries_plan` was rewritten to avoid.
        """
        backend_text = _backend_text()
        missing = sorted(name for name in BACKEND_ONLY_EVENTS
                         if '"%s"' % name not in backend_text)
        assert missing == [], (
            "these are pinned as backend-only but nothing emits them: %s" % missing
        )


class TestMaterialCompletedShape:
    def test_success_payload_carries_every_key_the_frontend_reads(self):
        from backend.orchestration.loop import Candidate, MaterialResult

        class _Gen:
            material = {"listening_material_parts": []}
            blueprint = {"items": []}

        class _Cross:
            ok = True

            def as_dict(self):
                return {"unrecoverable": [], "unintended_target": [], "ambiguous": []}

        candidate = Candidate(_Gen(), {"verdict": "PASS", "score": {"total": 80}}, _Cross(),
                              "initial")
        result = MaterialResult("slot-1", "booking-hotel", True, candidate, "initial", "pending")
        result.material_id = "20260801-booking-hotel-abc123"
        result.scenario_key = "booking-hotel"
        result.group_key = "booking-hotel"

        payload = result.as_dict()
        missing = sorted(MATERIAL_COMPLETED_KEYS - set(payload))
        assert missing == [], "material_completed lost keys the frontend reads: %s" % missing

    def test_failure_payload_shape(self):
        from backend.orchestration.loop import MaterialResult

        result = MaterialResult("slot-2", "booking-hotel", False, reason="model_error",
                                detail="boom")
        payload = result.as_dict()
        missing = sorted(FAILURE_KEYS - set(payload))
        assert missing == [], "material_failed lost keys: %s" % missing
        assert payload["ok"] is False

    def test_validation_findings_is_always_present(self):
        """Emitted even when empty, on purpose.

        A conditionally-added key means the frontend cannot tell "no findings" from "an older
        backend that did not report them", and it would render the second as the first.
        """
        from backend.orchestration.loop import Candidate, MaterialResult

        class _Gen:
            material = {}
            blueprint = {}

        class _Cross:
            def as_dict(self):
                return {}

        candidate = Candidate(_Gen(), {}, _Cross(), "initial")
        payload = MaterialResult("s", "x", True, candidate, "initial", "pending").as_dict()
        assert payload["validation_findings"] == []
        assert payload["warnings"] == []


class TestS3AndStateContract:
    """Storage layout and state names, which the audio pipeline and the history API both index by."""

    def test_state_directory_names_are_unchanged(self):
        from audio_storage.state_store import APPROVED, PENDING, PRODUCTION, REJECTED, STATES

        assert (PENDING, APPROVED, REJECTED, PRODUCTION) == (
            "pending", "approved", "rejected", "production")
        assert STATES == ("pending", "approved", "rejected", "production")

    def test_batch_history_prefixes_are_unchanged(self):
        from web.batch_store import BATCH_PREFIX, index_key, material_key

        assert BATCH_PREFIX == "_batches/"
        assert index_key("b1") == "_batches/b1/index.json"
        assert material_key("b1", "m1") == "_batches/b1/materials/m1.json"

    def test_candidate_registry_prefixes_are_unchanged(self):
        from backend.orchestration.candidate_store import CANDIDATE_PREFIX, CLAIM_PREFIX

        assert CANDIDATE_PREFIX == "_candidates/"
        assert CLAIM_PREFIX == "_claims/"

    def test_manifest_is_still_the_completeness_sentinel(self):
        """A material is visible only once this file exists. Renaming it would publish half-built
        materials, because the read side treats its absence as "incomplete"."""
        from audio_storage.state_store import MANIFEST_NAME

        assert MANIFEST_NAME == "audio/manifest.json"


class TestActionNames:
    """The `action` values the frontend posts to `/api/invocations`."""

    def test_every_action_the_frontend_sends_is_still_handled(self):
        handler = (REPO / "backend" / "app.py").read_text(encoding="utf-8")
        for action in ("generate", "list_scenarios", "select", "preview_audio",
                       "audio_status", "list_candidates", "presign_audio"):
            assert '"%s"' % action in handler, action

    def test_the_frontend_sends_exactly_these(self):
        text = _frontend_text()
        for action in ("list_scenarios", "select", "preview_audio", "audio_status",
                       "presign_audio"):
            assert action in text, action
