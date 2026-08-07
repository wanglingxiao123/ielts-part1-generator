"""Read-only view of the Runtime's ``_slots/`` request state, for the fan-out's terminal summary.

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
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

__all__ = [
    "SLOT_PREFIX",
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
        if self._store is None:
            return None
        key = "%s%s/request.json" % (SLOT_PREFIX, batch_id)
        try:
            raw = self._store.get(key)
            document = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception as exc:  # noqa: BLE001 - see docstring
            if type(exc).__name__ != "ObjectNotFound":
                LOG.warning("slot state unreadable at %s: %s: %s",
                            key, type(exc).__name__, str(exc)[:200])
            return None
        if not isinstance(document, dict):
            return None
        rows = document.get("slots")
        if not isinstance(rows, list):
            return None
        return [row for row in rows if isinstance(row, dict) and row.get("slot_id")]


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
