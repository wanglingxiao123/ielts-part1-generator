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

**Routing is not this module's decision.** ``state_store.publish_material`` routes on the audit
verdict, and a FAIL or NOT_ASSESSABLE material is written to quarantine with no audio at all.
Synthesis is skipped in that case: paying to voice a material that no reviewer should see is
waste, and quarantine having no audio is expected rather than incomplete.
"""

from __future__ import annotations

import asyncio
import threading
import time
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
QUARANTINED = "quarantined"
NOT_REQUESTED = "not_requested"

# Verdicts that never get audio. Mirrors state_store.QUARANTINE_VERDICTS, but this module must
# not import audio_storage at module scope: backend.tests import it without AWS configured.
_QUARANTINE_VERDICTS = ("FAIL", "NOT_ASSESSABLE")


class SelectionError(RuntimeError):
    """The selection cannot be performed."""


class UnknownMaterial(SelectionError):
    """No candidate with that material_id is registered."""


class AlreadySelected(SelectionError):
    """A sibling of this material was already selected, so this one was discarded."""


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

    @property
    def expects_audio(self) -> bool:
        return self.verdict not in _QUARANTINE_VERDICTS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "material_id": self.material_id,
            "scenario_key": self.scenario_key,
            "group_key": self.group_key,
            "slot_id": self.slot_id,
            "verdict": self.verdict,
            "score": self.score,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "expects_audio": self.expects_audio,
            "state": self.state,
        }


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


class CandidateRegistry:
    """In-process registry of offered candidates and their audio jobs.

    In memory on purpose: a candidate that was never selected has no reason to exist beyond the
    user's session (design.md §14 -- the unselected material is discarded, not archived). The
    cost is that a restart loses the offer, which the frontend already handles by re-generating.
    What a restart cannot lose is a *published* material or a paid-for clip; both live in S3.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._candidates: Dict[str, Candidate] = {}
        self._jobs: Dict[str, AudioJob] = {}
        self._selected_by_group: Dict[str, str] = {}
        self._counter = 0

    def register(self, candidate: Candidate) -> Candidate:
        with self._lock:
            self._candidates[candidate.material_id] = candidate
        return candidate

    def get(self, material_id: str) -> Candidate:
        with self._lock:
            candidate = self._candidates.get(material_id)
        if candidate is None:
            raise UnknownMaterial(
                "no candidate {0!r}; it was never offered, was discarded, or the process "
                "restarted".format(material_id)
            )
        return candidate

    def group(self, group_key: str) -> List[Candidate]:
        with self._lock:
            return [c for c in self._candidates.values() if c.group_key == group_key]

    def all(self) -> List[Candidate]:
        with self._lock:
            return list(self._candidates.values())

    def job(self, material_id: str) -> Optional[AudioJob]:
        with self._lock:
            return self._jobs.get(material_id)

    def claim(self, candidate: Candidate, total: int) -> "tuple[AudioJob, bool, List[str]]":
        """Reserve the single job for this material. Returns (job, is_new, discarded_siblings).

        Under the lock, so two concurrent selects on the same material produce one job rather
        than two synthesis runs. The sibling check is here too: a second pick within the same
        group must be refused, not silently published alongside the first.
        """
        with self._lock:
            existing = self._jobs.get(candidate.material_id)
            if existing is not None:
                return existing, False, list(existing.siblings_discarded)

            chosen = self._selected_by_group.get(candidate.group_key)
            if chosen is not None and chosen != candidate.material_id:
                raise AlreadySelected(
                    "{0} was already selected for {1}; {2} was discarded".format(
                        chosen, candidate.group_key, candidate.material_id
                    )
                )

            self._counter += 1
            job = AudioJob("job-{0}".format(self._counter), candidate.material_id, total)
            self._jobs[candidate.material_id] = job
            self._selected_by_group[candidate.group_key] = candidate.material_id

            discarded = []
            for sibling in list(self._candidates.values()):
                if sibling.group_key != candidate.group_key or sibling.material_id == candidate.material_id:
                    continue
                sibling.state = "discarded"
                # Dropped from the registry, not written anywhere. design.md §14 settled this:
                # the unselected material is not archived, and adding a sixth state directory to
                # keep it would be a decision for a human, not a convenience.
                self._candidates.pop(sibling.material_id, None)
                discarded.append(sibling.material_id)
            job.siblings_discarded = discarded
            candidate.state = "selected"
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


def _publish_blocking(
    candidate: Candidate,
    job: AudioJob,
    state_store,
    backing,
    polly,
    actor: str,
) -> None:
    """The whole slow path, run in a worker thread. Never call this on the event loop.

    Ordering: clips first, sentinel last. ``synthesize_material`` writes the MP3s into the
    destination prefix and returns the manifest without publishing it; ``publish_material`` then
    heads every clip and writes ``audio/manifest.json`` in one PutObject. A crash anywhere before
    that leaves paid-for clips in place and the material invisible, which is exactly what the
    completeness sentinel is for.
    """
    from audio_storage import synthesize as synth
    from audio_storage.state_store import QUARANTINE, route_for_verdict, verdict_of

    verdict = verdict_of(candidate.audit)
    state = route_for_verdict(verdict)
    job.state = state

    if state == QUARANTINE:
        # No synthesis at all. Not an optimisation: a quarantined material is one a reviewer
        # should not be handed, so voicing it spends money to produce something nobody plays.
        state_store.publish_material(
            candidate.material, candidate.blueprint, candidate.audit,
            scenario_key=candidate.scenario_key, material_id=candidate.material_id,
            actor=actor, degraded=candidate.degraded,
            degraded_reason=candidate.degraded_reason,
        )
        job.status = QUARANTINED
        job.finished_at = time.time()
        return

    def on_event(name: str, detail: Dict[str, Any]) -> None:
        if name == "synthesis_started":
            job.status = SYNTHESIZING
            job.total = detail.get("total", job.total)
            job.done = detail.get("reused", 0)
        elif name == "turn_done":
            job.done = detail.get("done", job.done)

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
            _publish_blocking(candidate, job, state_store, backing, polly, actor)
        except BaseException as exc:  # noqa: BLE001 - a thread's exception must land on the job
            job.status = FAILED
            job.error = "{0}: {1}".format(type(exc).__name__, str(exc)[:400])
            job.finished_at = time.time()

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
