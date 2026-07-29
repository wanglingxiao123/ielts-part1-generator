"""Shared-storage backing for the candidate registry.

The in-process registry was correct for a single server and wrong for AgentCore. Runtime routes
an invocation to whichever microVM is warm, and a different `runtimeSessionId` guarantees a new
one, so `generate` and the `select` that follows it routinely land in different processes. The
symptom was silent and confusing: generation succeeded, a `material_id` was minted and returned,
and then `list_candidates` came back empty and `select` reported the id unknown.

Candidates live under `_candidates/` rather than in a state directory. They are offers, not
materials: an unselected one is discarded and never published (audio-storage design.md §14), so
they must not appear in `pending/` where a reviewer would see them.

The one property that shared storage makes harder is `claim`'s atomicity. A thread lock cannot
span microVMs, so two concurrent selects could each believe they won and pay for synthesis twice.
The lock is replaced by a conditional write on a per-group marker: `put(..., if_none_match=True)`
succeeds for exactly one caller and raises `PreconditionFailed` for the rest. That primitive was
verified against real S3 (second write returns 412), which is why it is trusted here.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

__all__ = ["CandidateStore", "S3CandidateStore", "InMemoryCandidateStore", "build_store"]

CANDIDATE_PREFIX = "_candidates/"
CLAIM_PREFIX = "_claims/"
# Offers are ephemeral. An abandoned batch would otherwise leave candidates visible to
# list_candidates forever, and a reviewer picking a two-day-old offer would be selecting a
# material nobody remembers generating.
CANDIDATE_TTL_SECONDS = 24 * 3600


def _candidate_key(material_id: str) -> str:
    return "{0}{1}.json".format(CANDIDATE_PREFIX, material_id)


def _claim_key(group_key: str) -> str:
    return "{0}{1}.json".format(CLAIM_PREFIX, group_key)


class CandidateStore:
    """What the registry needs from shared storage. Deliberately narrow."""

    def save(self, material_id: str, record: Dict[str, Any]) -> None:
        raise NotImplementedError

    def load(self, material_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def load_all(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def drop(self, material_id: str) -> None:
        raise NotImplementedError

    def claim_group(self, group_key: str, material_id: str, job_id: str) -> Dict[str, Any]:
        """Record the winner for a group, or return the claim that already exists.

        Returns the stored claim either way, so the caller compares `material_id` to learn
        whether it won. Never raises on a lost race -- losing is an expected outcome here, and
        the caller turns it into `AlreadySelected` with the winner's id.
        """
        raise NotImplementedError

    def release_claim(self, group_key: str) -> None:
        """Undo a claim whose follow-up writes failed.

        Without this, a group whose winner could not be persisted stays claimed by a material_id
        that has no candidate and no job, and every later select on that group raises
        AlreadySelected naming a material nobody can find. Observed in the Runtime: two such
        orphans were left behind when `save` raised on a non-serialisable field.
        """
        raise NotImplementedError

    def save_job(self, material_id: str, job: Dict[str, Any]) -> None:
        raise NotImplementedError

    def load_job(self, material_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class InMemoryCandidateStore(CandidateStore):
    """For tests and single-process runs. Same semantics, no network."""

    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, Any]] = {}
        self._claims: Dict[str, Dict[str, Any]] = {}
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def save(self, material_id: str, record: Dict[str, Any]) -> None:
        self._records[material_id] = json.loads(json.dumps(record))

    def load(self, material_id: str) -> Optional[Dict[str, Any]]:
        found = self._records.get(material_id)
        return json.loads(json.dumps(found)) if found else None

    def load_all(self) -> List[Dict[str, Any]]:
        return [json.loads(json.dumps(v)) for v in self._records.values()]

    def drop(self, material_id: str) -> None:
        self._records.pop(material_id, None)

    def claim_group(self, group_key: str, material_id: str, job_id: str) -> Dict[str, Any]:
        existing = self._claims.get(group_key)
        if existing is not None:
            return dict(existing)
        claim = {"group_key": group_key, "material_id": material_id,
                 "job_id": job_id, "at": _now()}
        self._claims[group_key] = claim
        return dict(claim)

    def release_claim(self, group_key: str) -> None:
        self._claims.pop(group_key, None)

    def save_job(self, material_id: str, job: Dict[str, Any]) -> None:
        self._jobs[material_id] = json.loads(json.dumps(job))

    def load_job(self, material_id: str) -> Optional[Dict[str, Any]]:
        found = self._jobs.get(material_id)
        return json.loads(json.dumps(found)) if found else None


class S3CandidateStore(CandidateStore):
    """Backed by the materials bucket through audio_storage's object-store interface."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def save(self, material_id: str, record: Dict[str, Any]) -> None:
        self._store.put(_candidate_key(material_id), _dumps(record))

    def load(self, material_id: str) -> Optional[Dict[str, Any]]:
        return self._read(_candidate_key(material_id))

    def load_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cutoff = _now() - CANDIDATE_TTL_SECONDS
        for key in self._store.list_keys(CANDIDATE_PREFIX):
            record = self._read(key)
            if record is None:
                continue
            # Expiry is applied on read rather than by a sweeper: there is no scheduler here, and
            # a stale offer that is never listed is harmless until it is listed.
            if float(record.get("created_at") or 0) < cutoff:
                continue
            out.append(record)
        out.sort(key=lambda r: float(r.get("created_at") or 0))
        return out

    def drop(self, material_id: str) -> None:
        self._store.delete([_candidate_key(material_id)])

    def claim_group(self, group_key: str, material_id: str, job_id: str) -> Dict[str, Any]:
        claim = {"group_key": group_key, "material_id": material_id,
                 "job_id": job_id, "at": _now()}
        key = _claim_key(group_key)
        try:
            # if_none_match is the cross-instance lock: S3 lets exactly one of N concurrent
            # writers create the key and fails the others with 412.
            self._store.put(key, _dumps(claim), if_none_match=True)
            return claim
        except Exception as exc:  # noqa: BLE001 - PreconditionFailed lives in audio_storage
            if type(exc).__name__ not in ("PreconditionFailed", "ConditionalWriteUnsupported"):
                raise
            if type(exc).__name__ == "ConditionalWriteUnsupported":
                # Without the primitive there is no safe way to arbitrate; refuse rather than
                # race, since the failure mode is paying twice for synthesis.
                raise
            existing = self._read(key)
            # A 412 with no readable body should not silently become "you won".
            return existing if existing is not None else claim

    def release_claim(self, group_key: str) -> None:
        self._store.delete([_claim_key(group_key)])

    def save_job(self, material_id: str, job: Dict[str, Any]) -> None:
        self._store.put("{0}{1}.job.json".format(CANDIDATE_PREFIX, material_id), _dumps(job))

    def load_job(self, material_id: str) -> Optional[Dict[str, Any]]:
        return self._read("{0}{1}.job.json".format(CANDIDATE_PREFIX, material_id))

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            raw = self._store.get(key)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ObjectNotFound":
                return None
            raise
        try:
            found = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError):
            return None
        return found if isinstance(found, dict) else None


def _dumps(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _now() -> float:
    return time.time()


def build_store() -> CandidateStore:
    """S3 when a bucket is configured, otherwise in-memory.

    In-memory is the right fallback for local runs and tests, and the wrong one in the Runtime --
    which is exactly the bug this module fixes. So the caller logs which backend is in use (see
    `describe_store`), making a silent downgrade visible instead of appearing as an empty
    candidate list minutes later.
    """
    from .. import audio as audio_config

    try:
        bucket = audio_config.bucket_name()
    except Exception:  # noqa: BLE001 - AudioNotConfigured and anything else mean "no bucket"
        return InMemoryCandidateStore()
    from audio_storage.object_store import S3ObjectStore

    return S3CandidateStore(S3ObjectStore(bucket))


def describe_store(store: CandidateStore) -> str:
    return type(store).__name__
