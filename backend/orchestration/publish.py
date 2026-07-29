"""Candidate registry and the selection path: synthesise, publish, discard the rest.

The parent PRD's flow is "generate two, show both, the user picks one, only that one gets
audio". Three consequences shape this module.

**Synthesis must not block the event loop.** It is 30-45 Polly requests plus 30-45 S3 puts --
tens of seconds. ``/ping`` shares the loop with the entrypoint, and AgentCore kills an instance
whose health check times out (agent-backend design.md §7). So ``select`` returns a job
immediately and the work runs in a worker thread through ``asyncio.to_thread``; the client polls
``audio_status``. Everything inside the thread is synchronous boto3, which is correct there and
would be a fault on the loop.

**Selection must be idempotent.** The frontend contract requires a repeated POST not to bill
twice. Two layers cover it: the job registry returns the existing job for a material already
selected, and underneath, ``synthesize_material`` skips any clip whose cache key already matches
the object in S3. So even a lost job record cannot cause a second charge -- the expensive
guarantee does not depend on this process's memory.

**Every candidate is selectable and every selection is synthesised.** There is no verdict that
withholds audio. The product owner's rule: a user who asked for two materials receives two, a
flawed one included, with its shortcomings stated on the card so the user decides. So a FAIL
material takes exactly the same path as a PASS one -- ``audit.json`` is the only difference, and
the frontend reads it.

(A NOT_ASSESSABLE material never reaches selection: ``run_batch`` re-runs that slot, because an
empty or structurally broken script gives the user nothing to judge and no text to read.)
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

__all__ = [
    "Candidate",
    "CandidateRegistry",
    "AudioJob",
    "SelectionError",
    "AlreadySelected",
    "UnknownMaterial",
    "REGISTRY",
    "select_material",
    "audio_status",
    "list_candidates",
]

QUEUED = "queued"
SYNTHESIZING = "synthesizing"
READY = "ready"
FAILED = "failed"
NOT_REQUESTED = "not_requested"


class SelectionError(RuntimeError):
    """The selection cannot be performed."""


class UnknownMaterial(SelectionError):
    """No candidate with that material_id is registered."""


class AlreadySelected(SelectionError):
    """A sibling of this material was already selected, so this one was discarded."""


def _plain(value: Any) -> Any:
    """Best-effort conversion to something `json.dumps` accepts.

    Objects that already know their dict form (`as_dict`) are asked for it; dataclass-ish objects
    fall back to `__dict__`. Anything else becomes a string rather than raising: a candidate must
    remain storable even if one diagnostic field cannot be represented faithfully.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    for attr in ("as_dict", "to_dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return _plain(method())
            except Exception:  # noqa: BLE001 - fall through to the next strategy
                pass
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return {str(k): _plain(v) for k, v in data.items()}
    return str(value)


class Candidate:
    """One generated material as offered to the user, before any audio exists.

    ``group_key`` is what makes "discard the others" well defined: the candidates competing for
    one choice are those generated for the same scenario in the same batch. Without it, selecting
    a material would either discard nothing or discard the whole batch.
    """

    __slots__ = ("material_id", "scenario_key", "group_key", "slot_id", "material", "blueprint",
                 "audit", "cross_check", "verdict", "score", "degraded", "degraded_reason",
                 "created_at", "state")

    def __init__(
        self,
        material_id: str,
        scenario_key: str,
        group_key: str,
        slot_id: str,
        material: Dict[str, Any],
        blueprint: Dict[str, Any],
        audit: Dict[str, Any],
        cross_check: Any = None,
        degraded: bool = False,
        degraded_reason: Optional[str] = None,
    ) -> None:
        self.material_id = material_id
        self.scenario_key = scenario_key
        self.group_key = group_key
        self.slot_id = slot_id
        self.material = material
        self.blueprint = blueprint
        self.audit = audit
        self.cross_check = cross_check
        self.verdict = str((audit or {}).get("verdict", "NOT_ASSESSABLE"))
        score = (audit or {}).get("score")
        self.score = score.get("total") if isinstance(score, dict) else None
        self.degraded = degraded
        self.degraded_reason = degraded_reason
        self.created_at = time.time()
        self.state = "offered"

    def card_fields(self) -> Dict[str, Any]:
        """The three derived fields the card grid renders. Never raises.

        Computed on demand rather than at construction: ``from_record`` rebuilds a Candidate on
        every load, and paying for three scans of the script on a path that only wanted the
        verdict would be waste.

        Failures degrade to empty values. These are display strings; a card missing its preview
        line is a cosmetic loss, while an exception here would propagate into `register` and cost
        the user a material they can otherwise select and listen to.
        """
        try:
            from ..deterministic import cards
            from .scenarios import title_for_key

            return {
                "preview_first_line": cards.preview_first_line(self.material),
                "preview_summary": cards.preview_summary(
                    self.material, self.blueprint, title_for_key(self.scenario_key)
                ),
                "flagged_points": cards.flagged_points(self.material, self.blueprint),
            }
        except Exception:  # noqa: BLE001 - see docstring
            import logging

            logging.getLogger(__name__).warning(
                "card fields unavailable for %s", self.material_id, exc_info=True
            )
            return {"preview_first_line": "", "preview_summary": "", "flagged_points": []}

    def as_dict(self) -> Dict[str, Any]:
        """Summary for the candidate list. Excludes the artifacts: a list of six candidates
        would otherwise carry six full scripts, and the UI only needs the verdict to render.

        The three card fields ARE included, precisely because they let the grid render without the
        scripts -- they are a few hundred bytes each and replace ~15KB of material per card.

        There is no `expects_audio`: every candidate is selectable and every selection is
        synthesised, so a field gating that would only ever be true.
        """
        payload = {
            "material_id": self.material_id,
            "scenario_key": self.scenario_key,
            "group_key": self.group_key,
            "slot_id": self.slot_id,
            "verdict": self.verdict,
            "score": self.score,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "state": self.state,
        }
        payload.update(self.card_fields())
        return payload

    def as_record(self) -> Dict[str, Any]:
        """Everything needed to reconstruct this candidate in another process.

        The artifacts are included because selection needs them: the process that synthesises may
        not be the one that generated, so it cannot rely on holding them in memory.
        """
        record = self.as_dict()
        record.update({
            "material": self.material,
            "blueprint": self.blueprint,
            "audit": self.audit,
            # cross_check arrives as a CrossCheckResult from the Loop, which json cannot encode.
            # Normalised here rather than at the call site: this is the only place the candidate
            # crosses a serialisation boundary, and a TypeError raised inside `select` surfaced as
            # an opaque 500 from the Runtime.
            "cross_check": _plain(self.cross_check),
            "created_at": self.created_at,
        })
        return record

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "Candidate":
        candidate = cls(
            material_id=str(record.get("material_id") or ""),
            scenario_key=str(record.get("scenario_key") or ""),
            group_key=str(record.get("group_key") or ""),
            slot_id=str(record.get("slot_id") or ""),
            material=record.get("material") or {},
            blueprint=record.get("blueprint") or {},
            audit=record.get("audit") or {},
            cross_check=record.get("cross_check"),
            degraded=bool(record.get("degraded")),
            degraded_reason=record.get("degraded_reason"),
        )
        # Preserved rather than recomputed: `created_at` drives offer expiry, and `state` is the
        # discarded/selected outcome another instance already decided.
        if record.get("created_at"):
            candidate.created_at = float(record["created_at"])
        if record.get("state"):
            candidate.state = str(record["state"])
        return candidate


class AudioJob:
    """Progress of one synthesis. Mutated from the worker thread, read from the loop."""

    __slots__ = ("job_id", "material_id", "status", "done", "total", "error", "manifest",
                 "started_at", "finished_at", "polly_calls", "reused", "cost_usd", "state",
                 "siblings_discarded")

    def __init__(self, job_id: str, material_id: str, total: int) -> None:
        self.job_id = job_id
        self.material_id = material_id
        self.status = QUEUED
        self.done = 0
        self.total = total
        self.error: Optional[str] = None
        self.manifest: Optional[Dict[str, Any]] = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.polly_calls = 0
        self.reused = 0
        self.cost_usd = 0.0
        self.state: Optional[str] = None
        self.siblings_discarded: List[str] = []

    def as_dict(self) -> Dict[str, Any]:
        payload = {
            "audio_job_id": self.job_id,
            "material_id": self.material_id,
            "status": self.status,
            "progress": {"done": self.done, "total": self.total},
            "state": self.state,
            "siblings_discarded": list(self.siblings_discarded),
            "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at, 2),
            # Reported even on success: it is the evidence for "a repeat selection did not bill
            # again", and a number nobody can see is a guarantee nobody can check.
            "polly_calls": self.polly_calls,
            "reused_clips": self.reused,
            "cost_usd": self.cost_usd,
        }
        if self.error:
            payload["error"] = self.error
        if self.manifest is not None:
            payload["manifest"] = self.manifest
        return payload

    def as_record(self) -> Dict[str, Any]:
        """Full state for shared storage, including the timestamps `as_dict` derives away.

        `as_dict` reports `elapsed_seconds`, which is a view. Persisting that instead of
        `started_at` would make elapsed time restart every time another instance loaded the job.
        """
        record = self.as_dict()
        record.update({
            "done": self.done,
            "total": self.total,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        })
        return record

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "AudioJob":
        progress = record.get("progress") or {}
        job = cls(
            str(record.get("audio_job_id") or ""),
            str(record.get("material_id") or ""),
            int(record.get("total") or progress.get("total") or 0),
        )
        job.status = str(record.get("status") or "queued")
        job.done = int(record.get("done") or progress.get("done") or 0)
        job.error = record.get("error")
        job.manifest = record.get("manifest")
        job.state = record.get("state")
        job.siblings_discarded = list(record.get("siblings_discarded") or [])
        job.polly_calls = int(record.get("polly_calls") or 0)
        job.reused = int(record.get("reused_clips") or 0)
        job.cost_usd = float(record.get("cost_usd") or 0.0)
        if record.get("started_at"):
            job.started_at = float(record["started_at"])
        job.finished_at = record.get("finished_at")
        return job


class CandidateRegistry:
    """Registry of offered candidates and their audio jobs, backed by shared storage.

    Originally in-process, on the reasoning that an unselected offer need not outlive the user's
    session. That reasoning holds for one server and fails on AgentCore: Runtime dispatches each
    invocation to whichever microVM is warm, so the `select` following a `generate` is routinely a
    different process. Generation would succeed and return a `material_id`, and the next call
    would report that id unknown.

    So offers go to shared storage (`candidate_store`), keyed by `material_id`. The in-process
    dicts remain as a read-through cache -- within one instance they save a round trip, and they
    are never the source of truth.

    What shared storage cannot provide is a lock spanning instances, which `claim` needed. That is
    handled with a conditional write instead; see `claim`.
    """

    def __init__(self, store: Optional[Any] = None) -> None:
        self._lock = threading.Lock()
        self._candidates: Dict[str, Candidate] = {}
        self._jobs: Dict[str, AudioJob] = {}
        self._selected_by_group: Dict[str, str] = {}
        self._counter = 0
        self._store = store

    @property
    def store(self) -> Any:
        """Built lazily so importing this module never touches AWS."""
        if self._store is None:
            from .candidate_store import build_store, describe_store

            self._store = build_store()
            # Logged once, at the only moment the choice is made. An in-memory store in the
            # Runtime is the defect this module exists to fix, and its symptom (an empty
            # candidate list minutes later, in a different process) points nowhere near the
            # cause. One line here turns that into a grep.
            import logging

            logging.getLogger(__name__).info(
                "candidate store backend: %s", describe_store(self._store)
            )
        return self._store

    def register(self, candidate: Candidate) -> Candidate:
        with self._lock:
            self._candidates[candidate.material_id] = candidate
        # Persisted after the local write so a storage failure cannot leave the cache empty while
        # the caller believes registration succeeded. batch.py downgrades an exception here to a
        # warning on the material, which is right: the material is valid either way.
        self.store.save(candidate.material_id, candidate.as_record())
        return candidate

    def get(self, material_id: str) -> Candidate:
        with self._lock:
            candidate = self._candidates.get(material_id)
        if candidate is not None:
            return candidate
        record = self.store.load(material_id)
        if record is None:
            raise UnknownMaterial(
                "no candidate {0!r}; it was never offered, was discarded, or the offer "
                "expired".format(material_id)
            )
        candidate = Candidate.from_record(record)
        with self._lock:
            self._candidates.setdefault(material_id, candidate)
        return candidate

    def group(self, group_key: str) -> List[Candidate]:
        return [c for c in self.all() if c.group_key == group_key]

    def all(self) -> List[Candidate]:
        """Shared storage is authoritative; the local cache only fills gaps within an instance."""
        found: Dict[str, Candidate] = {}
        for record in self.store.load_all():
            candidate = Candidate.from_record(record)
            if candidate.material_id:
                found[candidate.material_id] = candidate
        with self._lock:
            for material_id, candidate in self._candidates.items():
                found.setdefault(material_id, candidate)
        return sorted(found.values(), key=lambda c: c.created_at)

    def job(self, material_id: str) -> Optional[AudioJob]:
        with self._lock:
            job = self._jobs.get(material_id)
        if job is not None:
            return job
        record = self.store.load_job(material_id)
        return AudioJob.from_record(record) if record else None

    def save_job(self, job: AudioJob) -> None:
        """Publish job progress so a poll served by another instance sees it.

        Called after each state change in the worker thread. The write is best-effort: losing a
        progress update degrades the UI to a stale percentage, whereas raising here would abort a
        synthesis that is otherwise succeeding.
        """
        try:
            self.store.save_job(job.material_id, job.as_record())
        except Exception:  # noqa: BLE001 - progress reporting must not break synthesis
            pass

    def claim(self, candidate: Candidate, total: int) -> "tuple[AudioJob, bool, List[str]]":
        """Reserve the single job for this material. Returns (job, is_new, discarded_siblings).

        Arbitration is a conditional write on a per-group marker, not a lock. A `threading.Lock`
        only orders callers inside one process, and the two selects racing here can be in
        different microVMs; the loser would then pay for a second full synthesis. S3's
        create-if-absent gives exactly one winner across instances (verified against real S3: the
        second write returns 412).

        A repeated select on the *same* material is not a race and must not raise -- the frontend
        contract allows a retry, and the existing job is returned unchanged.
        """
        existing = self.job(candidate.material_id)
        if existing is not None:
            return existing, False, list(existing.siblings_discarded)

        job_id = "job-{0}".format(uuid.uuid4().hex[:12])
        claim = self.store.claim_group(candidate.group_key, candidate.material_id, job_id)
        winner = str(claim.get("material_id") or "")
        if winner and winner != candidate.material_id:
            raise AlreadySelected(
                "{0} was already selected for {1}; {2} was discarded".format(
                    winner, candidate.group_key, candidate.material_id
                )
            )

        # Won the group. Re-check for a job: the winning claim may have been recorded by another
        # instance that has already started synthesising.
        existing = self.job(candidate.material_id)
        if existing is not None:
            return existing, False, list(existing.siblings_discarded)

        job = AudioJob(str(claim.get("job_id") or job_id), candidate.material_id, total)

        # Order matters, and it is the reverse of the obvious one. Persisting the winner comes
        # before discarding the siblings, and the claim is released if it fails: a claim that
        # outlives its winner makes the whole group permanently unselectable, since every later
        # attempt raises AlreadySelected naming a material_id that no longer has a candidate.
        # Discarding first would additionally destroy the alternatives on the way out.
        candidate.state = "selected"
        try:
            self.store.save(candidate.material_id, candidate.as_record())
        except Exception:
            candidate.state = "offered"
            try:
                self.store.release_claim(candidate.group_key)
            except Exception:  # noqa: BLE001 - the original failure is the one worth raising
                pass
            raise

        discarded = []
        for sibling in self.group(candidate.group_key):
            if sibling.material_id == candidate.material_id:
                continue
            sibling.state = "discarded"
            # Removed, not archived: audio-storage design.md §14 settled that the unselected
            # material is discarded, and adding a sixth state directory to keep it is a decision
            # for a human rather than a convenience.
            self.store.drop(sibling.material_id)
            with self._lock:
                self._candidates.pop(sibling.material_id, None)
            discarded.append(sibling.material_id)
        job.siblings_discarded = discarded

        with self._lock:
            self._jobs[candidate.material_id] = job
            self._selected_by_group[candidate.group_key] = candidate.material_id
        self.save_job(job)
        return job, True, discarded


REGISTRY = CandidateRegistry()


def list_candidates(registry: Optional[CandidateRegistry] = None) -> List[Dict[str, Any]]:
    return [c.as_dict() for c in (registry or REGISTRY).all()]


def scenario_key_for(scenario: Any) -> str:
    """The S3 path segment for a scenario (audio-storage design.md §8.1).

    Never derived from ``material.json``'s ``scenario`` field: that is a full English sentence
    and cannot be a key. A catalogue scenario supplies its id; a user-written one gets
    ``custom-<sha1(text)[:8]>``, which is stable so the same custom text always lands in the same
    prefix instead of scattering across a new directory per batch.
    """
    import hashlib

    scenario_id = str(getattr(scenario, "id", "") or "")
    if scenario_id and scenario_id != "custom":
        return scenario_id
    hint = str(getattr(scenario, "prompt_hint", "") or "")
    return "custom-{0}".format(hashlib.sha1(hint.encode("utf-8")).hexdigest()[:8])


def _turn_count(candidate: Candidate) -> int:
    parts = (candidate.material or {}).get("listening_material_parts") or []
    if not parts:
        return 0
    script = parts[0].get("script") if isinstance(parts[0], dict) else None
    turns = script.get("turns") if isinstance(script, dict) else None
    return len(turns or [])


def _persist(registry: Optional["CandidateRegistry"], job: AudioJob) -> None:
    """Best-effort publish of job state. Never raises: a lost progress update degrades the UI to
    a stale percentage, while an exception here would abort a synthesis that is succeeding."""
    if registry is not None:
        registry.save_job(job)


def _publish_blocking(
    candidate: Candidate,
    job: AudioJob,
    state_store,
    backing,
    polly,
    actor: str,
    registry: Optional["CandidateRegistry"] = None,
) -> None:
    """The whole slow path, run in a worker thread. Never call this on the event loop.

    Ordering: clips first, sentinel last. ``synthesize_material`` writes the MP3s into the
    destination prefix and returns the manifest without publishing it; ``publish_material`` then
    heads every clip and writes ``audio/manifest.json`` in one PutObject. A crash anywhere before
    that leaves paid-for clips in place and the material invisible, which is exactly what the
    completeness sentinel is for.
    """
    from audio_storage import synthesize as synth
    from audio_storage.state_store import route_for_verdict, verdict_of

    # No verdict branch: the user picked this material, so it gets voiced. A FAIL script the user
    # chose knowing its defects is a material they intend to listen to.
    verdict = verdict_of(candidate.audit)
    state = route_for_verdict(verdict)
    job.state = state

    def on_event(name: str, detail: Dict[str, Any]) -> None:
        if name == "synthesis_started":
            job.status = SYNTHESIZING
            job.total = detail.get("total", job.total)
            job.done = detail.get("reused", 0)
        elif name == "turn_done":
            job.done = detail.get("done", job.done)
        # Published on every turn so a poll landing on another instance shows real progress
        # instead of sitting at "queued" for the whole minute.
        _persist(registry, job)

    result = synth.synthesize_material(
        candidate.material,
        material_id=candidate.material_id,
        scenario_key=candidate.scenario_key,
        store=backing,
        polly=polly,
        blueprint=candidate.blueprint,
        state=state,
        degraded=candidate.degraded,
        degraded_reason=candidate.degraded_reason,
        on_event=on_event,
    )
    job.polly_calls = result.polly_calls
    job.reused = len(result.reused)
    job.cost_usd = result.cost_usd

    if not result.ok:
        job.status = FAILED
        job.error = "turns {0} failed to synthesise: {1}".format(
            sorted(result.failed), "; ".join(list(result.failed.values())[:2])
        )
        job.finished_at = time.time()
        _persist(registry, job)
        return

    # No `audio=` payload: the clips are already at the destination prefix, and re-uploading them
    # would double the bytes written for no gain. publish_material heads each one before it
    # writes the sentinel, so a missing clip still blocks publication.
    state_store.publish_material(
        candidate.material, candidate.blueprint, candidate.audit,
        scenario_key=candidate.scenario_key, material_id=candidate.material_id,
        actor=actor, manifest=result.manifest, degraded=candidate.degraded,
        degraded_reason=candidate.degraded_reason,
    )
    job.manifest = result.manifest
    job.done = job.total
    job.status = READY
    job.finished_at = time.time()
    _persist(registry, job)


async def select_material(
    material_id: str,
    *,
    registry: Optional[CandidateRegistry] = None,
    state_store=None,
    backing=None,
    polly=None,
    actor: str = "user",
    wait: bool = False,
) -> Dict[str, Any]:
    """Accept a user's choice and start synthesis. Returns as soon as the job exists.

    ``wait`` is for tests and the CLI only. A caller on the AgentCore event loop must never set
    it: awaiting the thread there is the same stall as running synthesis inline, and the health
    check is what pays for it.
    """
    registry = registry or REGISTRY
    candidate = registry.get(material_id)
    job, is_new, discarded = registry.claim(candidate, _turn_count(candidate))

    if not is_new:
        # Idempotent: the same job, and no second synthesis. The manifest, if there is one, comes
        # back so a client that lost the response can pick up where it left off.
        return dict(job.as_dict(), repeat=True)

    if state_store is None or backing is None or polly is None:
        from .. import audio

        built_state, built_backing, built_polly = audio.build_clients()
        state_store = state_store or built_state
        backing = backing or built_backing
        polly = polly or built_polly

    def work() -> None:
        try:
            _publish_blocking(candidate, job, state_store, backing, polly, actor,
                              registry=registry)
        except BaseException as exc:  # noqa: BLE001 - a thread's exception must land on the job
            job.status = FAILED
            job.error = "{0}: {1}".format(type(exc).__name__, str(exc)[:400])
            job.finished_at = time.time()
            _persist(registry, job)

    task = asyncio.create_task(asyncio.to_thread(work))
    if wait:
        await task
    payload = dict(job.as_dict(), repeat=False)
    payload["siblings_discarded"] = discarded
    return payload


def audio_status(
    material_id: str, *, registry: Optional[CandidateRegistry] = None
) -> Dict[str, Any]:
    """Poll one material's audio job. ``not_requested`` when it was never selected."""
    registry = registry or REGISTRY
    job = (registry or REGISTRY).job(material_id)
    if job is None:
        return {"material_id": material_id, "status": NOT_REQUESTED,
                "progress": {"done": 0, "total": 0}}
    return job.as_dict()
