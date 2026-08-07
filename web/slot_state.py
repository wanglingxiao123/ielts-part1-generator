"""Read-only view of the Runtime's ``_slots/`` request state and ``_questions/`` delivered sets.

## Why the web tier reads this at all

``web/fanout.py`` used to resolve a slot with no terminal event to ``failed``, and said why: a card
still spinning is worse than a card that says it failed. That reasoning was correct while a slot could
not outlive one SSE stream. ``action=generate_sets`` changed it -- a slot is now a persistent record
(``backend/orchestration/slot_store.py``), and a request that ran out of clock is *resumable*, so
"nothing arrived" has three possible meanings instead of one:

* the slot finished and the frame was lost -- it is ``complete`` on disk;
* the slot stopped on the clock or is mid-retry -- resumable, and drawing it as failed tells the user
  their material is gone when the next invocation would have finished it;
* nothing was ever recorded -- which is the original case, and still ``failed``.

The child reports its own state in ``request_completed``, so this module is only consulted for a child
that never answered. That keeps the common path free of S3 reads entirely.

## Why the layout is duplicated rather than imported

``web/`` is deployed as its own image and does not ship ``backend/`` (see ``web/Dockerfile``), so
``from backend.orchestration.slot_store import ...`` is an ImportError in the container. Same problem
``web/batch_history.py`` has with the candidate TTL, and the same answer: duplicate the few constants
and pin them with a test that imports both (``test_slot_state.py``). ``audio_storage`` IS in the image,
which is why the object store itself can be shared.

**Read-only, and that is a boundary rather than an omission.** The Runtime owns slot state: it is the
process that knows whether a material qualified. A web tier that could write here would be a second
writer of the record resumption depends on, with no arbitration between them.

## ``_questions/`` shares this module, and this reader

The delivered question sets sit under a sibling prefix in the same bucket
(``backend/orchestration/slot_store.QUESTION_PREFIX``), so a second module would mean a second S3
client and a second copy of the same "answers None on every failure" policy. What the 题目预览 tab
needs is exactly what this reader already does: one GET, or an honest "not there yet".

The reader deliberately does NOT filter the package down to the candidate-visible block. Redacting
here would put the answer key's absence at the mercy of a server-side flag, and this endpoint's
consumer is an internal review page whose default is answers ON -- so the honest design is to ship
the whole package to a reviewer who is already authenticated and let the UI decide what to draw. If
this ever serves candidates, the redaction belongs in a separate endpoint, not in a flag on this one.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

__all__ = [
    "SLOT_PREFIX",
    "QUESTION_PREFIX",
    "COMPLETE",
    "EXHAUSTED",
    "TERMINAL_SLOT_STATES",
    "SlotStateReader",
    "build_reader",
    "describe_reader",
]

LOG = logging.getLogger(__name__)

# Mirrors `backend/orchestration/slot_store.SLOT_PREFIX` and its key layout. Pinned by a test.
SLOT_PREFIX = "_slots/"

# Mirrors `backend/orchestration/slot_store.QUESTION_PREFIX`. Also pinned, and for a sharper reason
# than the slot prefix: a wrong key here answers "no questions yet" for a set that was delivered, and
# the 题目预览 tab draws that as 暂无题目 -- a silent empty state, not an error anyone would chase.
QUESTION_PREFIX = "_questions/"

# Mirrors the two terminal slot states. Only these two are needed here: everything else is, by
# definition, work the next invocation can still pick up, and enumerating the intermediate states
# would mean this module had to be edited every time one is added.
COMPLETE = "complete"
EXHAUSTED = "exhausted"
TERMINAL_SLOT_STATES = (COMPLETE, EXHAUSTED)


class SlotStateReader(object):
    """Loads one request document, or answers None. Never raises."""

    def __init__(self, store: Optional[Any]) -> None:
        self._store = store

    @property
    def available(self) -> bool:
        """False when no bucket is configured, so a caller can say "unknown" instead of "failed"."""
        return self._store is not None

    def load_slots(self, batch_id: str) -> Optional[List[Dict[str, Any]]]:
        """The slot rows the Runtime last recorded for this request, or None if there are none.

        None and ``[]`` are different answers and both are real: None means nothing was ever recorded
        for this id (the child never got as far as its first write), and an empty list means a document
        exists that names no slots. The caller resolves the first to ``failed`` and must not resolve the
        second the same way by accident.

        Every failure -- no bucket, absent key, malformed JSON, S3 refusing -- answers None after one
        log line. A history-style swallow rather than a raise: this is consulted while framing the
        terminal event of a batch that has already delivered its materials, and an exception here would
        replace a summary with a stream error.
        """
        document = self._read("%s%s/request.json" % (SLOT_PREFIX, batch_id))
        if document is None:
            return None
        rows = document.get("slots")
        if not isinstance(rows, list):
            return None
        return [row for row in rows if isinstance(row, dict) and row.get("slot_id")]

    def find_slot(self, batch_id: str, material_id: str) -> Optional[Dict[str, Any]]:
        """The slot row that holds this material, plus its request's status, or None.

        This is what makes the 题目预览 tab able to say *why* there are no questions -- paused on the
        clock, exhausted, the whole request a system failure -- instead of only 暂无题目. After a
        reload the SSE stream is gone, so storage is the only place that answer still exists.

        **Searched under ``_slots/{batch_id}-``, not over the whole prefix.** A fanned-out child's
        request id is ``{web batch id}-{slot id}`` (``fanout.plan_children``), so that prefix matches
        exactly this batch's children and nothing else: one LIST plus one GET per child, rather than
        the one-GET-per-batch-in-the-bucket a search by material id alone would cost.

        Returns ``{"slot": row, "request_status": str|None}``. The status is the request document's
        own, so ``incomplete`` / ``system_failure`` are read rather than re-derived -- the web tier
        must not compute a second opinion about a status the Runtime already wrote down.
        """
        if self._store is None or not batch_id or not material_id:
            return None
        try:
            keys = self._store.list_keys("%s%s-" % (SLOT_PREFIX, batch_id))
        except Exception as exc:  # noqa: BLE001 - same policy as _read
            LOG.warning("slot listing failed for %s: %s: %s",
                        batch_id, type(exc).__name__, str(exc)[:200])
            return None
        for key in keys:
            if not key.endswith("/request.json"):
                continue
            document = self._read(key)
            if not isinstance(document, dict):
                continue
            for row in document.get("slots") or []:
                if isinstance(row, dict) and row.get("material_id") == material_id:
                    return {"slot": row, "request_status": document.get("status")}
        return None

    def load_questions(self, material_id: str) -> Optional[Dict[str, Any]]:
        """The delivered question set for this material, or None if none was delivered.

        None covers every reason there is nothing to show, and they are deliberately not
        distinguished here: no bucket, no such key, unparseable body. All three mean "this material
        has no delivered question set", which is what the tab renders as 暂无题目. The one thing this
        must never do is invent an empty package -- a caller cannot tell an empty face from a real
        one, and 「十道题都空着」 looks like a generation bug rather than a missing artifact.

        Only sets that cleared every gate are ever written here (``slot_store.save_questions``), so a
        payload found at this key needs no quality interpretation: it is deliverable by construction.
        """
        return self._read("%s%s.json" % (QUESTION_PREFIX, material_id))

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        """One GET, parsed, or None after at most one log line. Never raises -- see ``load_slots``."""
        if self._store is None:
            return None
        try:
            raw = self._store.get(key)
            document = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception as exc:  # noqa: BLE001 - see load_slots' docstring
            if type(exc).__name__ != "ObjectNotFound":
                LOG.warning("unreadable at %s: %s: %s", key, type(exc).__name__, str(exc)[:200])
            return None
        return document if isinstance(document, dict) else None


def build_reader() -> SlotStateReader:
    """S3 when a bucket is configured, a reader that answers None when none is.

    The no-bucket case is a local run, and answering None there reproduces exactly the behaviour the
    fan-out had before this module existed. That is the right default for a *reader*: a missing bucket
    must not make an unknown slot look complete.
    """
    import os

    bucket = (os.environ.get("IELTS_AUDIO_BUCKET") or "").strip()
    if not bucket:
        return SlotStateReader(None)
    try:
        from audio_storage.object_store import S3ObjectStore

        return SlotStateReader(S3ObjectStore(bucket))
    except Exception:  # noqa: BLE001 - a reader that cannot be built is a reader that answers None
        LOG.warning("slot state reader unavailable", exc_info=True)
        return SlotStateReader(None)


def describe_reader(reader: SlotStateReader) -> str:
    return "S3ObjectStore" if reader.available else "unavailable"
