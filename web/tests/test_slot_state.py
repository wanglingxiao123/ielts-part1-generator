"""The web tier's read-only view of `_slots/`, and the constants it duplicates.

Two things here, and the second is the reason this file exists rather than the first.

**The reader must never raise.** It is consulted while framing the terminal event of a batch whose
materials the browser has already rendered, so an exception would replace a summary with a stream
error — turning a missing S3 object into a batch that appears to have died.

**The duplicated constants must not drift.** `web/` ships without `backend/` (see `web/Dockerfile`),
so `SLOT_PREFIX` and the terminal states are copied rather than imported. A copy nothing checks is a
copy that silently stops matching: change `SLOT_PREFIX` in `backend/orchestration/slot_store.py` and
the reader would look under a prefix the Runtime never writes, find nothing, and report every
resumable slot as failed — the exact misreport the reader was added to remove, arriving as a
successful read of an empty answer. Same arrangement, and same pin, as
`web/batch_history.CANDIDATE_TTL_SECONDS`.
"""

from __future__ import annotations

import json

import pytest

from backend.orchestration import slot_store
from web.slot_state import (COMPLETE, EXHAUSTED, SLOT_PREFIX, TERMINAL_SLOT_STATES,
                            SlotStateReader, build_reader, describe_reader)


class TestTheDuplicatedLayoutMatchesTheRuntimes:
    def test_the_prefix_is_the_one_the_runtime_writes_under(self):
        assert SLOT_PREFIX == slot_store.SLOT_PREFIX

    def test_the_request_key_is_the_one_the_runtime_writes(self):
        """Not just the prefix: the whole key. The reader builds it by hand, and a layout change
        inside `_slots/{batch_id}/…` would be as invisible as a prefix change."""
        expected = slot_store._request_key("b1")
        assert "%s%s/request.json" % (SLOT_PREFIX, "b1") == expected

    def test_the_two_terminal_states_are_the_runtimes_two_terminal_states(self):
        """`delivery._slot_row` computes `resumable` as `state not in (COMPLETE, EXHAUSTED)`. This
        module's fallback has to agree, because a row read from storage has no `resumable` field."""
        assert (COMPLETE, EXHAUSTED) == (slot_store.COMPLETE, slot_store.EXHAUSTED)
        assert TERMINAL_SLOT_STATES == (slot_store.COMPLETE, slot_store.EXHAUSTED)

    def test_every_other_slot_state_is_deliberately_not_listed(self):
        """The non-terminal states are resumable BY DEFAULT here, so adding one to the Runtime must
        not require editing this module. If this ever fails, a state was added that is terminal."""
        others = [s for s in slot_store.SLOT_STATES if s not in TERMINAL_SLOT_STATES]
        assert others and all(s not in TERMINAL_SLOT_STATES for s in others)


class FakeStore:
    """The two behaviours `load_slots` has to survive: an answer, and a raise."""

    class ObjectNotFound(Exception):
        pass

    def __init__(self, objects=None, raises=None):
        self._objects = objects or {}
        self._raises = raises
        self.gets = []

    def get(self, key):
        self.gets.append(key)
        if self._raises is not None:
            raise self._raises
        if key not in self._objects:
            raise FakeStore.ObjectNotFound(key)
        return self._objects[key]


def document(*rows) -> bytes:
    return json.dumps({"batch_id": "b1", "slots": list(rows)}).encode("utf-8")


class TestLoadSlots:
    def test_it_reads_the_rows_the_runtime_recorded(self):
        store = FakeStore({"_slots/b1/request.json": document(
            {"slot_id": "slot-1", "state": "complete", "material_id": "mat-1"})})
        rows = SlotStateReader(store).load_slots("b1")
        assert [row["slot_id"] for row in rows] == ["slot-1"]
        assert store.gets == ["_slots/b1/request.json"]

    def test_a_str_body_is_accepted_as_well_as_bytes(self):
        """`InMemoryObjectStore` and S3 do not agree on this, and the reader is given whichever the
        deployment has."""
        store = FakeStore({"_slots/b1/request.json":
                           document({"slot_id": "slot-1", "state": "complete"}).decode("utf-8")})
        assert SlotStateReader(store).load_slots("b1")[0]["state"] == "complete"

    def test_a_missing_object_is_None_not_an_error(self):
        assert SlotStateReader(FakeStore()).load_slots("b1") is None

    def test_malformed_json_is_None_not_an_error(self):
        store = FakeStore({"_slots/b1/request.json": b"{not json"})
        assert SlotStateReader(store).load_slots("b1") is None

    def test_s3_refusing_is_None_not_an_error(self):
        """AccessDenied, a throttle, a timeout: a summary must still be produced."""
        store = FakeStore(raises=RuntimeError("AccessDenied"))
        assert SlotStateReader(store).load_slots("b1") is None

    def test_a_document_with_no_slots_key_is_None(self):
        store = FakeStore({"_slots/b1/request.json": b'{"batch_id": "b1"}'})
        assert SlotStateReader(store).load_slots("b1") is None

    def test_an_empty_slot_list_is_an_empty_list_not_None(self):
        """The two answers mean different things -- "nothing recorded" vs "a record naming no slots"
        -- and the caller resolves only the first to `failed`."""
        store = FakeStore({"_slots/b1/request.json": document()})
        assert SlotStateReader(store).load_slots("b1") == []

    def test_rows_without_a_slot_id_are_dropped(self):
        """A row the merge cannot join on is worse than a missing row: it would be counted against a
        slot chosen by list position."""
        store = FakeStore({"_slots/b1/request.json": document(
            {"state": "complete"}, {"slot_id": "slot-1", "state": "complete"})})
        assert [r["slot_id"] for r in SlotStateReader(store).load_slots("b1")] == ["slot-1"]

    def test_no_store_reads_nothing_and_answers_None(self):
        reader = SlotStateReader(None)
        assert reader.available is False
        assert reader.load_slots("b1") is None


class TestBuildReader:
    def test_no_bucket_yields_an_unavailable_reader(self, monkeypatch):
        """A local run. Answering None there is exactly the behaviour the fan-out had before this
        module existed -- a missing bucket must not make an unknown slot look complete."""
        monkeypatch.delenv("IELTS_AUDIO_BUCKET", raising=False)
        reader = build_reader()
        assert reader.available is False
        assert describe_reader(reader) == "unavailable"

    def test_a_bucket_yields_a_reader_that_reads(self, monkeypatch):
        monkeypatch.setenv("IELTS_AUDIO_BUCKET", "some-bucket")
        assert describe_reader(build_reader()) == "S3ObjectStore"


class TestItIsReadOnly:
    def test_the_reader_exposes_no_way_to_write(self):
        """A boundary, not an omission: the Runtime owns slot state, and a web tier that could write
        here would be a second writer of the record resumption depends on."""
        public = [name for name in dir(SlotStateReader) if not name.startswith("_")]
        assert sorted(public) == ["available", "load_slots"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
