"""Shared-storage backing for batch history. Storage only -- no policy.

## Why the web tier owns this at all

A batch is a unit that exists nowhere else. The Runtime sees one material per invocation
(`web/fanout.py`), so it has no concept of "these six materials were asked for together"; the
frontend used to hold that fact in a browser-side `Map`, which a page reload discarded. The web
tier is the only component that knows a batch exists as a unit, so it is the only component that
can record one.

## Layout

    _batches/{batch_id}/index.json                      the light record (the panel's list)
    _batches/{batch_id}/materials/{material_id}.json     one material's artifacts

Split for one reason: the history panel lists every batch a reviewer ever ran, and a list call
must not drag ~20KB of script per material through the web tier to render a row that says
"6 套 · 待选稿". The index is a few hundred bytes and is all the list needs; the artifacts are
fetched only when a batch is actually opened.

The prefix is `_batches/`, alongside `_candidates/` and `_history/`, so a batch record cannot be
mistaken for a material in a state directory -- the same reasoning `candidate_store` gives for
keeping offers out of `pending/`.

## Batch records do NOT expire

`_candidates/` applies a 24h TTL on read (`CANDIDATE_TTL_SECONDS`) because an offer nobody took is
noise. A batch record is the opposite kind of object: "已归档" is a status the product asks for, so
the record has to outlive everything it refers to. There is deliberately no cutoff anywhere in this
module and no sweeper to add one.

## Why writes are best-effort and never conditional

Every write here is a plain `put`. There is no `if_none_match` and no read-modify-write race to
protect against, because exactly one process writes any given batch key: the record is written by
the `FanOut` recorder that serves that batch's own POST, and each subsequent write is a full
overwrite of a record that process holds in memory. `candidate_store` needs conditional writes
because two microVMs can arbitrate one candidate group; nothing here is arbitrated.

Failures are swallowed by the caller (`web/batch_history.py`), not here: this module reports what
happened, and the decision that a lost history write must not break an in-flight batch belongs to
the layer that knows a batch is in flight.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

__all__ = [
    "BATCH_PREFIX",
    "BatchStore",
    "InMemoryBatchStore",
    "S3BatchStore",
    "build_store",
    "describe_store",
]

BATCH_PREFIX = "_batches/"
INDEX_NAME = "index.json"
MATERIALS_SEGMENT = "materials/"


def index_key(batch_id: str) -> str:
    return "{0}{1}/{2}".format(BATCH_PREFIX, batch_id, INDEX_NAME)


def material_key(batch_id: str, material_id: str) -> str:
    return "{0}{1}/{2}{3}.json".format(BATCH_PREFIX, batch_id, MATERIALS_SEGMENT, material_id)


class BatchStore:
    """What batch history needs from shared storage. Deliberately narrow."""

    def save_index(self, batch_id: str, record: Dict[str, Any]) -> None:
        raise NotImplementedError

    def load_index(self, batch_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def load_all_indexes(self) -> List[Dict[str, Any]]:
        """Every batch record, newest first. No TTL -- see the module docstring."""
        raise NotImplementedError

    def save_material(self, batch_id: str, material_id: str, record: Dict[str, Any]) -> None:
        raise NotImplementedError

    def load_material(self, batch_id: str, material_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def find_material(self, material_id: str) -> Optional[Dict[str, Any]]:
        """One material's artifacts without knowing which batch it belongs to.

        Needed because a reader-page URL is `/materials/{id}` and carries no batch id. Before batch
        history existed the frontend answered that from its in-session cache, so a reload made every
        material unreadable -- which is exactly the failure the history panel would otherwise
        reintroduce the moment a reviewer clicked 阅读全文 on a batch from last week.
        """
        raise NotImplementedError


class InMemoryBatchStore(BatchStore):
    """For tests and local runs. Same semantics, no network.

    Round-trips through JSON on every read and write, exactly like the S3 backend does, so a test
    cannot pass by mutating a record the store still holds a reference to.
    """

    def __init__(self) -> None:
        self._indexes: Dict[str, str] = {}
        self._materials: Dict[str, str] = {}

    def save_index(self, batch_id: str, record: Dict[str, Any]) -> None:
        self._indexes[batch_id] = json.dumps(record, ensure_ascii=False)

    def load_index(self, batch_id: str) -> Optional[Dict[str, Any]]:
        raw = self._indexes.get(batch_id)
        return json.loads(raw) if raw is not None else None

    def load_all_indexes(self) -> List[Dict[str, Any]]:
        return _newest_first([json.loads(raw) for raw in self._indexes.values()])

    def save_material(self, batch_id: str, material_id: str, record: Dict[str, Any]) -> None:
        self._materials[material_key(batch_id, material_id)] = json.dumps(
            record, ensure_ascii=False
        )

    def load_material(self, batch_id: str, material_id: str) -> Optional[Dict[str, Any]]:
        raw = self._materials.get(material_key(batch_id, material_id))
        return json.loads(raw) if raw is not None else None

    def find_material(self, material_id: str) -> Optional[Dict[str, Any]]:
        suffix = "/{0}{1}.json".format(MATERIALS_SEGMENT, material_id)
        for key, raw in self._materials.items():
            if key.endswith(suffix):
                return json.loads(raw)
        return None


class S3BatchStore(BatchStore):
    """Backed by the materials bucket through audio_storage's object-store interface."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def save_index(self, batch_id: str, record: Dict[str, Any]) -> None:
        self._store.put(index_key(batch_id), _dumps(record))

    def load_index(self, batch_id: str) -> Optional[Dict[str, Any]]:
        return self._read(index_key(batch_id))

    def load_all_indexes(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for key in self._store.list_keys(BATCH_PREFIX):
            # `list_keys` returns the material sidecars too. Filtering by suffix rather than by
            # using a narrower prefix per batch, because the alternative is one LIST per batch.
            if not key.endswith("/" + INDEX_NAME):
                continue
            record = self._read(key)
            if record is not None:
                out.append(record)
        return _newest_first(out)

    def save_material(self, batch_id: str, material_id: str, record: Dict[str, Any]) -> None:
        self._store.put(material_key(batch_id, material_id), _dumps(record))

    def load_material(self, batch_id: str, material_id: str) -> Optional[Dict[str, Any]]:
        return self._read(material_key(batch_id, material_id))

    def find_material(self, material_id: str) -> Optional[Dict[str, Any]]:
        """One LIST over `_batches/` and one GET, rather than a GET per known batch.

        The sidecar key ends with the material id, so the whole search is a suffix match on the
        listing. Not free -- the LIST grows with the number of batches -- but it is one call
        regardless, and the alternative (an index from material_id to batch_id) is a second object
        that can disagree with the first.
        """
        suffix = "/{0}{1}.json".format(MATERIALS_SEGMENT, material_id)
        for key in self._store.list_keys(BATCH_PREFIX):
            if key.endswith(suffix):
                return self._read(key)
        return None

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
            # A truncated or hand-edited record is skipped, not fatal: one unreadable batch must
            # not empty the whole history panel.
            return None
        return found if isinstance(found, dict) else None


def _newest_first(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sorted by creation time, descending -- the order the panel renders in.

    Sorted here rather than in the API layer so both backends and every caller agree, and so
    "数据按时间倒序" is a property of the store rather than of one route.
    """
    return sorted(records, key=lambda r: float(r.get("created_at") or 0), reverse=True)


def _dumps(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def build_store() -> BatchStore:
    """S3 when a bucket is configured, otherwise in-memory.

    In-memory is right for local runs and tests and wrong in the deployed web tier -- it is a
    single Fargate task, so an in-memory history would be lost on every redeploy, which is the
    exact failure this module exists to fix. The caller logs which backend it got (`describe_store`)
    so a silent downgrade is a grep rather than an empty panel days later.
    """
    import os

    bucket = (os.environ.get("IELTS_AUDIO_BUCKET") or "").strip()
    if not bucket:
        return InMemoryBatchStore()
    from audio_storage.object_store import S3ObjectStore

    return S3BatchStore(S3ObjectStore(bucket))


def describe_store(store: BatchStore) -> str:
    return type(store).__name__
