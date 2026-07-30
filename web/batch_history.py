"""Batch records: what one is, who writes it, and how its status is derived.

## The record exists because nothing else knows a batch is a unit

`web/fanout.py` sends one Runtime invocation per material, so the Runtime only ever sees one
material at a time and has no name for the group. The frontend held that grouping in a browser-side
`Map`, which is why a reload lost it and why history did not exist. The web tier plans the fan-out,
so it is the only component that can record a batch -- and this module is where it does.

## When it is written, and why not once at the end

Three write points, and the middle one is the whole design:

1. **At batch start**, before any child answers. So a batch that dies immediately still leaves
   evidence that it was asked for.
2. **On every `material_completed`**, incrementally. This is what survives a web-task restart. The
   web tier is a single Fargate task and a redeploy replaces it; a record written only at
   `batch_completed` would mean a batch interrupted after five of six materials leaves *nothing* --
   the five delivered materials would be as lost as the sixth. Incremental writes mean the record
   always describes what has actually arrived.
3. **At `batch_completed`**, to stamp the final counts and flip `state` to `complete`.

A batch whose task died is therefore left with `state: "running"` and no `completed_at`, forever --
nothing will ever come back to finalise it. That is not papered over: `derive` reports
`interrupted: true` for it once `STALE_RUNNING_SECONDS` has passed, and the materials it did record
stay listed, readable and playable. The alternative -- inferring completion on read -- would report
a batch as finished when the only thing known is that nobody is working on it any more.

## Writes never touch the event loop

Every write is a blocking boto3 PUT, and this runs inside the SSE generator that feeds the browser.
So the recorder owns one worker thread and a coalescing slot: `on_event` only swaps the newest
snapshot into that slot and wakes the thread. Six materials completing during one in-flight PUT
collapse into one further PUT rather than six, so the write cost is bounded by latency instead of by
batch size, and the loop does no I/O at all. The threads are NOT taken from anyio's default pool
for the reason `web/fanout.py` gives at length: a token held here is a token `/healthz` cannot have.

Losing a history write is survivable and losing a batch is not, so every failure in the worker is
swallowed after being logged once. A batch that generated six materials must not fail because S3
refused a few hundred bytes of metadata.

## The three statuses, and which one had to be added

The client asked for 待选稿 / 已提交 / 已归档. Checked against what the backend can actually
substantiate:

* **待选稿** -- derived, from two facts that already exist: no submission is recorded against the
  batch, AND its candidates are still resolvable. "Still resolvable" is not a guess:
  `_candidates/` entries are hidden from `list_candidates` after `CANDIDATE_TTL_SECONDS`
  (`backend/orchestration/candidate_store.py`), so a batch past that age can no longer have a
  selection made against it through the normal path. While it is inside that window the batch is
  genuinely awaiting a choice, which is what 待选稿 says.

* **已提交** -- **added**, because it did not exist. Material states are
  `pending / approved / rejected / production` and none of them means "a reviewer submitted this
  for review"; the review queue was `localStorage` only
  (`frontend/src/stores/reviewQueueStore.ts`), so it could not be a status the backend reports.
  Rather than invent a state directory or a material transition, the submission is recorded where
  the batch already lives: `submitted_at` / `submitted_by` / `submitted_material_ids` on the batch
  record, written by the new `submit_batch` action. That is the smallest addition that makes the
  status a recorded fact instead of a browser's private opinion, and it keeps the material state
  machine (`audio_storage/state_store.py`) untouched.

* **已归档** -- derived: no submission was ever recorded and the candidates have expired. Nothing
  can be selected in that batch any more, so there is no decision left to make and the batch is
  history. This is deliberately a real boundary rather than an age threshold picked to fill the
  third chip: it is the same expiry that makes the batch read-only in the first place.

A submitted batch stays 已提交 however old it gets. The recorded fact is "someone submitted this",
and age does not unmake it.

## Read-only follows from the status, it is not a second rule

已提交 means the choice was made; 已归档 means the choice can no longer be made. Both are read-only
for exactly the reason their name gives, so `derive` returns `read_only` alongside the status rather
than leaving the frontend to re-derive a rule it could get subtly wrong.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .batch_store import BatchStore, build_store, describe_store

__all__ = [
    "PENDING_SELECTION",
    "SUBMITTED",
    "ARCHIVED",
    "CANDIDATE_TTL_SECONDS",
    "STALE_RUNNING_SECONDS",
    "BatchHistory",
    "BatchRecorder",
    "derive",
    "new_batch_id",
]

LOG = logging.getLogger(__name__)

# The three product statuses. Values are machine tokens; the Chinese labels live in the frontend
# (`frontend/src/domain/batchHistory.ts`), because copy is not the backend's to own.
PENDING_SELECTION = "pending_selection"  # 待选稿
SUBMITTED = "submitted"                  # 已提交
ARCHIVED = "archived"                    # 已归档

# Mirrors `backend/orchestration/candidate_store.CANDIDATE_TTL_SECONDS`. Duplicated rather than
# imported: `web/` is deployed as its own image and does not ship `backend/` (see web/Dockerfile),
# so importing it would be an ImportError in the container. `test_batch_history.py` asserts the two
# constants agree, which is what keeps the duplicate honest.
CANDIDATE_TTL_SECONDS = 24 * 3600

# After this long, a record still claiming `state: "running"` is a batch whose web task died --
# nothing else can leave one in that state. Deliberately far above any real batch: the per-material
# wall is 900s and a large batch runs several waves of those, so an hour-long batch is normal and
# must not be reported as interrupted while it is still streaming.
STALE_RUNNING_SECONDS = 2 * 3600

RECORD_VERSION = 1


def new_batch_id(counter: int, *, now: Optional[float] = None) -> str:
    """`web-<ms>-<counter>`. The id the fan-out already used, kept verbatim.

    It is not changed to something prettier because it is already load-bearing elsewhere: it
    namespaces the backend's candidate groups (`web/fanout.py`'s `plan_children`), so a batch record
    keyed on anything else could not be joined back to the candidates it produced.
    """
    moment = time.time() if now is None else now
    return "web-%d-%d" % (int(moment * 1000), counter)


def derive(record: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """The batch record as the history panel needs it: status, read-only, counts, scenarios.

    A pure function of the stored record plus the clock, so the status can be unit-tested without a
    store and cannot differ between the list route and the detail route.
    """
    moment = time.time() if now is None else now
    created_at = _as_float(record.get("created_at"))
    submitted_at = record.get("submitted_at")
    state = str(record.get("state") or "running")

    candidates_expired = (moment - created_at) >= CANDIDATE_TTL_SECONDS

    if submitted_at:
        status = SUBMITTED
    elif candidates_expired:
        status = ARCHIVED
    else:
        status = PENDING_SELECTION

    materials = [m for m in (record.get("materials") or []) if isinstance(m, dict)]

    return {
        "batch_id": str(record.get("batch_id") or ""),
        "created_at": created_at,
        "completed_at": record.get("completed_at"),
        "status": status,
        # 待选稿 is the only status in which a selection can still be made. The other two are
        # read-only for the reason their name gives -- see the module docstring.
        "read_only": status != PENDING_SELECTION,
        # A batch whose web task died mid-stream. Reported rather than smoothed over: the delivered
        # materials are genuinely usable and the missing ones are genuinely never coming.
        "interrupted": state == "running" and (moment - created_at) >= STALE_RUNNING_SECONDS,
        "state": state,
        "requested_total": int(_as_float(record.get("requested_total"))),
        "arrived": len(materials),
        "scenarios": [s for s in (record.get("scenarios") or []) if isinstance(s, dict)],
        "counts": record.get("counts") or {},
        "submitted_at": submitted_at,
        "submitted_by": record.get("submitted_by"),
        "submitted_material_ids": [
            str(m) for m in (record.get("submitted_material_ids") or [])
        ],
        "materials": materials,
    }


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class BatchHistory:
    """Read and mutate batch records. One instance per web tier; safe to share across requests."""

    def __init__(self, store: Optional[BatchStore] = None) -> None:
        self._store = store
        self._lock = threading.Lock()

    @property
    def store(self) -> BatchStore:
        """Built lazily so importing this module never constructs an AWS client."""
        if self._store is None:
            with self._lock:
                if self._store is None:
                    self._store = build_store()
                    # Logged once, at the only moment the choice is made. An in-memory store in the
                    # deployed tier is the defect this whole feature exists to fix, and its symptom
                    # -- an empty panel after a redeploy -- points nowhere near the cause.
                    LOG.info("batch history store backend: %s", describe_store(self._store))
        return self._store

    # ── reads ────────────────────────────────────────────────────────────────

    def list_batches(self, *, owner: Optional[str] = None, now: Optional[float] = None,
                     limit: int = 200) -> List[Dict[str, Any]]:
        """Every batch, newest first, without the artifacts.

        `materials` is reduced to its summary fields here: the panel renders a scenario tag and a
        set count, and shipping the scripts would make a 12-batch history a multi-megabyte response.
        The full artifacts come from `get_batch`, one batch at a time.

        Filtered by owner when one is given, so two reviewers do not see each other's batches. An
        older record with no `owner` is visible to everyone: it predates the field, and hiding a
        batch somebody generated would be a worse answer than showing it.
        """
        out: List[Dict[str, Any]] = []
        for record in self.store.load_all_indexes():
            if owner and record.get("owner") and record.get("owner") != owner:
                continue
            view = derive(record, now=now)
            view["materials"] = [
                {k: m.get(k) for k in ("material_id", "scenario_key", "index", "slot_id",
                                       "verdict", "degraded")}
                for m in view["materials"]
            ]
            out.append(view)
            if len(out) >= limit:
                break
        return out

    def get_batch(self, batch_id: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """One batch with every recorded material's full artifacts.

        Returns None when there is no such record, which the route turns into a 404 naming the
        batch -- distinguishable from a batch that exists and produced nothing.
        """
        record = self.store.load_index(batch_id)
        if record is None:
            return None
        view = derive(record, now=now)
        materials = []
        for summary in view["materials"]:
            material_id = str(summary.get("material_id") or "")
            full = self.store.load_material(batch_id, material_id) if material_id else None
            # The summary is the fallback rather than a reason to drop the row: a material whose
            # artifact write failed still happened, and a card that says so is better than a
            # silently shorter batch.
            materials.append(dict(summary, **(full or {})))
        view["materials"] = materials
        return view

    def get_material(self, material_id: str) -> Optional[Dict[str, Any]]:
        """One material's artifacts, by id alone. Powers the reader page for a historical batch.

        A reader-page URL is `/materials/{id}` and names no batch, so this cannot go through
        `get_batch`. Before batch history existed the frontend answered it from its in-session cache;
        a historical material would therefore 404, and 阅读全文 on last week's batch -- which is the
        client's "可看材料" -- would be a link to an error page.
        """
        found = self.store.find_material(material_id)
        if found is None:
            return None
        return dict(found, material_id=material_id)

    # ── the added transition ─────────────────────────────────────────────────

    def submit(self, batch_id: str, material_ids: List[str], *,
               actor: str, now: Optional[float] = None) -> Dict[str, Any]:
        """Record that a reviewer submitted this batch's picks. This is the 已提交 status.

        The transition the backend did not have -- see the module docstring. It writes only to the
        batch record and deliberately does NOT call `select`: those are different acts. `select`
        claims a candidate group, discards the losing siblings and pays Polly; submitting for review
        is a reviewer saying "these are my picks", which must not destroy anything and must not
        spend money. Coupling them would mean a reviewer could never revise a submission.

        Idempotent, and last-write-wins on the id list: re-submitting the same batch with a
        different set replaces the set rather than appending, because the second submission is the
        reviewer's current opinion. `submitted_at` keeps the FIRST submission's time, since that is
        when the batch stopped awaiting a decision.

        Raises KeyError when the batch is unknown, so the route can 404 rather than silently
        creating a record for a batch that never ran.
        """
        moment = time.time() if now is None else now
        record = self.store.load_index(batch_id)
        if record is None:
            raise KeyError(batch_id)
        record["submitted_at"] = record.get("submitted_at") or moment
        record["submitted_by"] = actor
        # De-duplicated while preserving order: the same material submitted twice is one pick, and
        # a set would make the stored order arbitrary between runs.
        seen, ordered = set(), []
        for material_id in material_ids:
            token = str(material_id)
            if token and token not in seen:
                seen.add(token)
                ordered.append(token)
        record["submitted_material_ids"] = ordered
        self.store.save_index(batch_id, record)
        return derive(record, now=moment)

    # ── writes used by the recorder ──────────────────────────────────────────

    def recorder(self, batch_id: str, *, owner: str, requested_total: int,
                 scenarios: List[Dict[str, Any]]) -> "BatchRecorder":
        return BatchRecorder(self, batch_id, owner=owner, requested_total=requested_total,
                             scenarios=scenarios)


class BatchRecorder:
    """Turns one batch's event stream into a batch record. One instance per request.

    Owns a single worker thread; `close()` stops it. Every public method is called from the event
    loop and none of them performs I/O -- see the module docstring on why that matters here.
    """

    def __init__(self, history: BatchHistory, batch_id: str, *, owner: str,
                 requested_total: int, scenarios: List[Dict[str, Any]]) -> None:
        self._history = history
        self._batch_id = batch_id
        self._lock = threading.Lock()
        self._record: Dict[str, Any] = {
            "record_version": RECORD_VERSION,
            "batch_id": batch_id,
            "owner": owner,
            "created_at": time.time(),
            "requested_total": requested_total,
            "scenarios": scenarios,
            "materials": [],
            "counts": {},
            # Flipped to "complete" by `batch_completed`. A record left saying "running" is a batch
            # whose task died; `derive` is what notices.
            "state": "running",
            "completed_at": None,
            "submitted_at": None,
            "submitted_by": None,
            "submitted_material_ids": [],
        }
        # (batch_id, material_id, artifacts) pending an artifact write. A list because each is a
        # distinct object and one cannot replace another -- unlike the index, which coalesces.
        self._artifacts: List[Tuple[str, Dict[str, Any]]] = []
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._drain, name="ielts-batch-history", daemon=True,
        )
        self._started = False

    def start(self) -> None:
        """Write the opening record. Called before the first child is invoked."""
        if self._started:
            return
        self._started = True
        self._thread.start()
        self._touch()

    def on_event(self, event: Dict[str, Any]) -> None:
        """Fold one merged event into the record. Never blocks, never raises.

        Only the two events that change what the record says are handled; `stage` frames arrive by
        the dozen per material and would cost a PUT each while changing nothing a history panel
        shows.
        """
        try:
            kind = str(event.get("type") or "")
            if kind == "material_completed":
                self._on_material(event)
            elif kind == "batch_completed":
                self._on_completed(event)
        except Exception:  # noqa: BLE001 - history must never break the stream it observes
            LOG.warning("batch history: could not fold %s", event.get("type"), exc_info=True)

    def _on_material(self, event: Dict[str, Any]) -> None:
        material_id = event.get("material_id")
        if not material_id:
            # No candidate backs this material (`REGISTRY.register` failed server-side), so there is
            # no id to key an artifact on and nothing later could resolve it. It still counts toward
            # the batch's totals through `batch_completed`; it just cannot be a history row.
            return
        summary = {
            "material_id": str(material_id),
            "scenario_key": str(event.get("scenario_key") or event.get("scenario") or ""),
            "slot_id": str(event.get("slot_id") or ""),
            "verdict": str((event.get("audit") or {}).get("verdict") or ""),
            "degraded": bool(event.get("degraded")),
            "created_at": _as_float(event.get("at")) or time.time(),
        }
        # The artifacts the reader and compare pages need. Stored per material rather than inside
        # the index for the reason `batch_store` gives: the panel's list must not carry them.
        #
        # The summary fields are repeated INTO the sidecar, which looks redundant next to the index
        # and is not: `get_material` resolves a material by id alone (the reader-page URL names no
        # batch), so it never reads the index and would otherwise return artifacts with no idea
        # which scenario they belong to.
        artifacts = dict(summary)
        artifacts["batch_id"] = self._batch_id
        artifacts.update({
            key: event.get(key)
            for key in ("material", "blueprint", "audit", "cross_check", "route",
                        "validation_findings", "group_key", "degraded_reason")
            if event.get(key) is not None
        })
        with self._lock:
            existing = [m for m in self._record["materials"]
                        if m.get("material_id") == summary["material_id"]]
            if not existing:
                self._record["materials"].append(summary)
            self._artifacts.append((summary["material_id"], artifacts))
        self._touch()

    def _on_completed(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self._record["counts"] = {
                key: int(_as_float(event.get(key)))
                for key in ("succeeded", "failed", "skipped", "degraded")
            }
            self._record["state"] = "complete"
            self._record["completed_at"] = _as_float(event.get("at")) or time.time()
        self._touch()

    def _touch(self) -> None:
        self._dirty.set()

    def close(self) -> None:
        """Stop the worker once it has drained. Runs from the stream's `finally`, disconnect included.

        `_stop` then `_dirty`, in that order: the worker checks `_stop` only *after* a flush, so
        waking it once here guarantees one more pass over whatever `batch_completed` just set. Waking
        it before setting `_stop` could let that pass finish first and leave the thread parked on
        `wait()` forever.

        Joined so the final snapshot has a chance to land -- without it a batch that completes and
        immediately closes could lose the write that flips `state` to `complete`, and the record
        would read as interrupted for no reason. Bounded, because a hung S3 call must not hold the
        response open.
        """
        self._stop.set()
        self._dirty.set()
        if self._started:
            self._thread.join(timeout=5.0)

    # ── the worker thread ────────────────────────────────────────────────────

    def _drain(self) -> None:
        """Flush whenever the record changes, and never otherwise.

        `wait()` has no timeout on purpose. A periodic wake-up would PUT the same bytes every tick
        for the whole length of a batch -- and worse, it would mask the incremental-write property:
        a record that only *appeared* to be written per material because a timer happened to catch
        it would pass every test here while losing an interrupted batch in production.

        `clear()` before `_flush()`, not after: the snapshot is taken inside the flush, so a change
        arriving during it re-sets the flag and is picked up by the next pass rather than being
        cleared away unwritten.
        """
        while True:
            self._dirty.wait()
            self._dirty.clear()
            self._flush()
            # Checked after the flush, so the snapshot `close()` asked for has already landed.
            if self._stop.is_set():
                return

    def _flush(self) -> None:
        with self._lock:
            # A copy, because the loop thread may append to `materials` while S3 is being written.
            snapshot = dict(self._record, materials=list(self._record["materials"]))
            pending, self._artifacts = self._artifacts, []
        store = None
        try:
            store = self._history.store
        except Exception:  # noqa: BLE001 - an unbuildable store is logged once, not per write
            LOG.warning("batch history: no store available", exc_info=True)
            return
        for material_id, artifacts in pending:
            try:
                store.save_material(self._batch_id, material_id, artifacts)
            except Exception:  # noqa: BLE001 - see the module docstring
                LOG.warning("batch history: artifact write failed for %s", material_id,
                            exc_info=True)
        try:
            store.save_index(self._batch_id, snapshot)
        except Exception:  # noqa: BLE001
            LOG.warning("batch history: index write failed for %s", self._batch_id, exc_info=True)
