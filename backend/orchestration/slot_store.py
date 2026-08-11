"""Persistent slot state under ``_slots/``, and delivered question sets under ``_questions/``.

A slot used to be an ``asyncio`` task name inside one ``run_batch`` call, so it existed for exactly
as long as the process did. Every requirement in §8.2 needs it to outlive that:

* a material that finished must not be regenerated because the question stage failed after it
  (§8.2(1)) -- which means "the material finished" has to be a *fact on disk*, not a local variable;
* a request that ran out of clock must be resumable by the next invocation (§8.2(4)) -- which means
  the next process has to be able to find out what the last one achieved;
* a request must never be reported successful with fewer than N complete sets (§8.2(3), §8.2(5)) --
  which means the count has to be recoverable from storage rather than from the summary the same
  process is about to write.

So a slot is a record here, and the runner in :mod:`delivery` is a state machine over these records.

**Two new key prefixes, and nothing else is touched.** ``_slots/`` and ``_questions/`` do not exist
in the bucket today, so no historical object changes shape and no reader of ``_candidates/`` or
``_batches/`` sees anything new (§6.2(2), risk 1). The blueprint compatibility problem is not solved
by these prefixes and is not meant to be -- it lives inside the candidate records.

**Layout.** Slots sit one directory below the request record rather than beside it::

    _slots/{batch_id}/request.json
    _slots/{batch_id}/slots/{slot_id}.json
    _questions/{material_id}.json

so ``list_slots`` is a prefix listing with no filename parsing. An earlier sketch put both in one
directory and filtered on a ``slot-`` prefix, which would have made the slot-id format load-bearing
for a listing -- and replacement slots invent ids (``slot-2r1``), so that is exactly the format most
likely to change.

**Persistence failure is an error, never a downgrade.** ``build_slot_store`` falls back to memory
only when no bucket is configured at all (local runs and tests); once a bucket exists, a failed write
raises :class:`SlotPersistenceError`. The runner turns that into a ``system_failure`` request rather
than continuing, because a request whose progress is not recorded cannot honour any of the three
requirements above -- and the failure mode it would otherwise produce is the one §8.2(5) singles
out: work that looks complete and is not.

There is no sweeper. Records are ~1 KB, nothing enumerates all requests, and a stale slot record that
is never listed is inert -- the same reasoning ``candidate_store`` applies to expiry, minus the read
path that made expiry necessary there.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

__all__ = [
    "COMPLETE",
    "EXHAUSTED",
    "INCOMPLETE",
    "MATERIAL_DONE",
    "MATERIAL_PENDING",
    "QUESTIONS_PENDING",
    "QUESTION_PREFIX",
    "QUESTION_REVISION_PREFIX",
    "QUESTION_VERSION_PREFIX",
    "RUNNING",
    "SLOT_PREFIX",
    "SLOT_STATES",
    "SUCCEEDED",
    "SYSTEM_FAILURE",
    "SlotPersistenceError",
    "SlotRecord",
    "SlotStore",
    "build_slot_store",
    "describe_slot_store",
]

SLOT_PREFIX = "_slots/"
QUESTION_PREFIX = "_questions/"
QUESTION_VERSION_PREFIX = "_question_versions/"
QUESTION_REVISION_PREFIX = "_question_revisions/"

# --- slot states (§8.2(1)) ------------------------------------------------------------------
# The order is the progression, and `material_done` exists as its own state for one reason: it is the
# boundary a question failure falls back to. Merging it into `questions_pending` would make "the
# material is finished" unobservable, which is the whole property §8.2(1) asks for.
MATERIAL_PENDING = "material_pending"
MATERIAL_DONE = "material_done"
QUESTIONS_PENDING = "questions_pending"
COMPLETE = "complete"
# The candidate-swap budget for this slot is spent. A terminal state for the *slot*, never for the
# request: the request answers it by opening a replacement slot (§8.2(3)), which is why this is not
# called "failed" -- nothing has been given up on at the point it is written.
EXHAUSTED = "exhausted"

SLOT_STATES = (MATERIAL_PENDING, MATERIAL_DONE, QUESTIONS_PENDING, COMPLETE, EXHAUSTED)

# --- request states (§8.2(5)) --------------------------------------------------------------
# Three of the four are not-success, and they are distinct because they call for different responses:
#
#   incomplete     -- the clock ran out; the work that remains is resumable by the next invocation.
#   system_failure -- something outside the material's quality stopped the request (storage refused a
#                     write, the question stage crashed twice on a material that had already
#                     qualified, feasibility could not be judged at all). Not resumable without
#                     someone looking at it.
#
# What is deliberately absent is a state for "fewer than N, but call it done". §8.2(3) removes that
# exit, and leaving the word out of the vocabulary is what stops it being reintroduced by a caller
# who needs *some* terminal status to report.
RUNNING = "running"
SUCCEEDED = "succeeded"
INCOMPLETE = "incomplete"
SYSTEM_FAILURE = "system_failure"


class SlotPersistenceError(RuntimeError):
    """A slot or request record could not be written to configured storage.

    Raised rather than swallowed. See the module docstring: a request that cannot record its own
    progress cannot make any of the promises this module exists to keep.
    """


class SlotRecord(object):
    """One slot's persistent state. Mutated by the runner, saved after every transition.

    ``attempts`` carries the two-level bound of §8.2(2) as data rather than as three separate
    counters, so a reader of the record can see both levels at once:

    * ``candidate_swaps`` -- the OUTER level. How many times this slot abandoned a material and drew
      another. Spent by a feasibility ``REGENERATE_MATERIAL``, by an unassessable material, by a
      material-stage failure with no content, and by the SECOND question-stage failure on one material.
    * ``material_repairs`` / ``question_repairs`` -- the INNER level, reported not enforced. Both
      caps live inside the loops that own them (``MAX_GENERATION_ATTEMPTS``,
      ``MAX_QUESTION_REVISIONS``); duplicating them here would be a second source of truth for a
      bound that is already enforced where the work happens.
    * ``question_restarts`` -- how many times the question stage was re-entered on the SAME qualified
      material, after a crash or after a not-deliverable verdict. Separate from ``candidate_swaps`` on
      purpose: neither is evidence against the material *the first time*, and charging either to the
      outer budget would throw away a material that §8.2(1) says must be kept. Counted per material,
      so a swap resets it -- see ``delivery._swap_candidate``.
    """

    __slots__ = ("batch_id", "slot_id", "scenario_id", "state", "attempts", "material_id",
                 "group_key", "last_failure", "checkpoint_at", "created_at", "updated_at",
                 "replaces", "replaced_by", "system_fault")

    def __init__(
        self,
        batch_id: str,
        slot_id: str,
        scenario_id: str,
        state: str = MATERIAL_PENDING,
        attempts: Optional[Dict[str, int]] = None,
        material_id: Optional[str] = None,
        group_key: Optional[str] = None,
        last_failure: Optional[Dict[str, Any]] = None,
        checkpoint_at: Optional[float] = None,
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
        replaces: Optional[str] = None,
        replaced_by: Optional[str] = None,
        system_fault: bool = False,
    ) -> None:
        self.batch_id = batch_id
        self.slot_id = slot_id
        self.scenario_id = scenario_id
        self.state = state
        self.attempts = {"candidate_swaps": 0, "material_repairs": 0,
                         "question_repairs": 0, "question_restarts": 0}
        for key, value in (attempts or {}).items():
            self.attempts[str(key)] = int(value)
        self.material_id = material_id
        self.group_key = group_key
        self.last_failure = last_failure
        self.checkpoint_at = checkpoint_at
        self.created_at = created_at if created_at is not None else _now()
        self.updated_at = updated_at if updated_at is not None else self.created_at
        # Which slot this one took over from, and which one took over from it. Both directions are
        # stored because the two questions have different askers: a summary walks forward to find the
        # live slot for a position, and an operator reading one record walks back to find out why it
        # exists at all.
        self.replaces = replaces
        self.replaced_by = replaced_by
        # True when this slot stopped for a reason that is not about material quality. Kept on the
        # record rather than inferred from `last_failure` prose, because the request's status depends
        # on it and a status must not depend on a string comparison against a message.
        self.system_fault = bool(system_fault)

    def bump(self, counter: str, by: int = 1) -> None:
        self.attempts[counter] = self.attempts.get(counter, 0) + by

    def as_record(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "slot_id": self.slot_id,
            "scenario_id": self.scenario_id,
            "state": self.state,
            "attempts": dict(self.attempts),
            "material_id": self.material_id,
            "group_key": self.group_key,
            "last_failure": self.last_failure,
            "checkpoint_at": self.checkpoint_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "replaces": self.replaces,
            "replaced_by": self.replaced_by,
            "system_fault": self.system_fault,
        }

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "SlotRecord":
        return cls(
            batch_id=str(record.get("batch_id") or ""),
            slot_id=str(record.get("slot_id") or ""),
            scenario_id=str(record.get("scenario_id") or ""),
            state=str(record.get("state") or MATERIAL_PENDING),
            attempts=record.get("attempts") or {},
            material_id=record.get("material_id"),
            group_key=record.get("group_key"),
            last_failure=record.get("last_failure"),
            checkpoint_at=record.get("checkpoint_at"),
            created_at=record.get("created_at"),
            updated_at=record.get("updated_at"),
            replaces=record.get("replaces"),
            replaced_by=record.get("replaced_by"),
            system_fault=bool(record.get("system_fault")),
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SlotRecord(%r, %r, %s)" % (self.batch_id, self.slot_id, self.state)


class SlotStore(object):
    """Slot records, request records and delivered question sets, over one object store.

    One class rather than three, because all three are the same operation -- a JSON document under a
    key -- and splitting them would give three places to get the error taxonomy wrong. The prefixes
    stay separate constants so the two data shapes remain independently greppable in the bucket.
    """

    def __init__(self, store: Any, persistent: bool = True) -> None:
        self._store = store
        # Whether a write failure is an error the caller must see. False only for the no-bucket
        # fallback, where there was never any persistence to lose. Reported by the runner in the
        # request record, so an in-memory run is visible as such instead of looking durable.
        self.persistent = persistent

    # --- request ---------------------------------------------------------------------------
    def save_request(self, record: Dict[str, Any]) -> None:
        self._put(_request_key(str(record.get("batch_id") or "")), record)

    def load_request(self, batch_id: str) -> Optional[Dict[str, Any]]:
        return self._read(_request_key(batch_id))

    # --- slots -----------------------------------------------------------------------------
    def save_slot(self, record: SlotRecord) -> None:
        record.updated_at = _now()
        self._put(_slot_key(record.batch_id, record.slot_id), record.as_record())

    def load_slot(self, batch_id: str, slot_id: str) -> Optional[SlotRecord]:
        found = self._read(_slot_key(batch_id, slot_id))
        return SlotRecord.from_record(found) if found else None

    def list_slots(self, batch_id: str) -> List[SlotRecord]:
        """Every slot record for one request, oldest first.

        Ordered by ``created_at`` rather than by key, so a replacement sorts after the slot it
        replaced regardless of how its id happens to collate.
        """
        out: List[SlotRecord] = []
        for key in self._store.list_keys(_slots_prefix(batch_id)):
            found = self._read(key)
            if isinstance(found, dict) and found.get("slot_id"):
                out.append(SlotRecord.from_record(found))
        out.sort(key=lambda r: (float(r.created_at or 0), r.slot_id))
        return out

    # --- delivered question sets -----------------------------------------------------------
    def save_questions(self, material_id: str, payload: Dict[str, Any]) -> None:
        """Persist a question set that cleared every gate.

        Only clean sets reach here: ``QuestionResult.as_dict()`` on a failure emits no ``package``
        key at all, so there is nothing under this prefix that a later reader could mistake for
        deliverable work. That property belongs to :mod:`question_loop` and is relied on rather than
        re-checked -- a second check here would be a second place to relax it.
        """
        self._put(_question_key(material_id), payload)

    def load_questions(self, material_id: str) -> Optional[Dict[str, Any]]:
        return self._read(_question_key(material_id))

    def save_question_version(
        self, material_id: str, version_id: str, payload: Dict[str, Any]
    ) -> None:
        key = _question_version_key(material_id, version_id)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self._store.put(key, body, if_none_match=True)
        except Exception as exc:  # noqa: BLE001 - object-store taxonomy is intentionally narrow
            if type(exc).__name__ == "PreconditionFailed":
                existing = self._read(key)
                if existing == payload:
                    return
                raise SlotPersistenceError(
                    "immutable question version already exists with different content: %s" % key
                )
            if not self.persistent:
                return
            raise SlotPersistenceError(
                "could not create %s: %s: %s" % (key, type(exc).__name__, str(exc)[:200])
            )

    def load_question_version(
        self, material_id: str, version_id: str
    ) -> Optional[Dict[str, Any]]:
        if version_id == "original":
            return self.load_questions(material_id)
        return self._read(_question_version_key(material_id, version_id))

    def save_question_revision(
        self, material_id: str, request_id: str, payload: Dict[str, Any]
    ) -> None:
        self._put(_question_revision_key(material_id, request_id), payload)
        # The Web tier reserves this pointer with a conditional write. Runtime owns its terminal
        # transition so a refresh can distinguish "still running" from a completed request without
        # relying on the original SSE connection.
        self._put(_question_revision_running_key(material_id), payload)

    def load_question_revision(
        self, material_id: str, request_id: str
    ) -> Optional[Dict[str, Any]]:
        return self._read(_question_revision_key(material_id, request_id))

    def claim_question_revision(self, material_id: str, request_id: str) -> bool:
        """Create-only execution claim; false means another Runtime invocation owns the work."""
        key = _question_revision_claim_key(material_id, request_id)
        try:
            self._store.put(key, b"{}", if_none_match=True)
            return True
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "PreconditionFailed":
                return False
            if not self.persistent:
                return True
            raise SlotPersistenceError(
                "could not claim %s: %s: %s" % (key, type(exc).__name__, str(exc)[:200])
            )

    # --- plumbing --------------------------------------------------------------------------
    def _put(self, key: str, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self._store.put(key, body)
        except Exception as exc:  # noqa: BLE001 - re-raised as this module's own error
            if not self.persistent:
                return
            raise SlotPersistenceError(
                "could not write %s: %s: %s" % (key, type(exc).__name__, str(exc)[:200])
            )

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            raw = self._store.get(key)
        except Exception as exc:  # noqa: BLE001 - ObjectNotFound lives in audio_storage
            if type(exc).__name__ == "ObjectNotFound":
                return None
            raise
        try:
            found = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError):
            # A record we cannot parse is not a record we may treat as absent: "no slot here" would
            # restart work that may already have finished. Surfaced as a persistence error so the
            # request fails honestly instead of silently redoing a material.
            raise SlotPersistenceError("slot record at %s is not readable JSON" % key)
        return found if isinstance(found, dict) else None


def _request_key(batch_id: str) -> str:
    return "%s%s/request.json" % (SLOT_PREFIX, batch_id)


def _slots_prefix(batch_id: str) -> str:
    return "%s%s/slots/" % (SLOT_PREFIX, batch_id)


def _slot_key(batch_id: str, slot_id: str) -> str:
    return "%s%s.json" % (_slots_prefix(batch_id), slot_id)


def _question_key(material_id: str) -> str:
    return "%s%s.json" % (QUESTION_PREFIX, material_id)


def _question_version_key(material_id: str, version_id: str) -> str:
    return "%s%s/versions/%s.json" % (QUESTION_VERSION_PREFIX, material_id, version_id)


def _question_revision_key(material_id: str, request_id: str) -> str:
    return "%s%s/%s.json" % (QUESTION_REVISION_PREFIX, material_id, request_id)


def _question_revision_running_key(material_id: str) -> str:
    return "%s%s/running.json" % (QUESTION_REVISION_PREFIX, material_id)


def _question_revision_claim_key(material_id: str, request_id: str) -> str:
    return "%s%s/%s.claim" % (QUESTION_REVISION_PREFIX, material_id, request_id)


def _now() -> float:
    return time.time()


def build_slot_store() -> SlotStore:
    """S3 when a bucket is configured, memory when none is -- and the difference is reported.

    The same shape as ``candidate_store.build_store``, including its lesson: the in-memory fallback
    is right locally and is a defect in the Runtime, and the way that defect used to present itself
    was an empty list minutes later in a different process. So ``persistent`` is carried on the
    returned store and the runner records it in the request document, which makes a non-durable run
    visible at the time it starts rather than at the time someone misses the data.
    """
    from audio_storage.object_store import InMemoryObjectStore

    from .. import audio as audio_config

    try:
        bucket = audio_config.bucket_name()
    except Exception:  # noqa: BLE001 - AudioNotConfigured and anything else mean "no bucket"
        return SlotStore(InMemoryObjectStore(), persistent=False)
    from audio_storage.object_store import S3ObjectStore

    return SlotStore(S3ObjectStore(bucket), persistent=True)


def describe_slot_store(store: SlotStore) -> str:
    return "%s(%s)" % (type(store._store).__name__,
                       "durable" if store.persistent else "memory")
