"""S3 state directories, verdict routing, and crash-recoverable transitions.

Layout (design.md §9):

    {state}/{scenario_key}/{material_id}/material.json | blueprint.json | audit.json
                                        /audio/turn_NNN.mp3, audio/manifest.json
    _history/{material_id}/{ts}-{src}-{dst}.json

`_history/` sits outside the state directories on purpose: the audit trail has to outlive
the material being moved, and outlive it being deleted. Cross-directory copy is only how a
state change happens; the history is what the change *is*.

The two hard parts, both fully exercised against InMemoryObjectStore:

1. **Verdict routing.** FAIL and NOT_ASSESSABLE go to quarantine with a machine-readable
   reason and no audio -- a reviewer's time is scarce, and a known-bad material in the review
   queue is a net cost to them. A degraded material (revise/re-audit skipped for time
   budget) that still audited PASS goes to pending carrying `degraded: true`: degraded is not
   the same as unfit, and the quality bar stays single.

2. **copy + delete is not atomic.** So the transition is six steps with an intent marker and
   forward-only recovery. Every crash point leaves either the source or the destination
   complete, never neither -- and the read side hides the window where both are complete.
   Rolling back would need a second code path for a case that forward recovery already
   handles idempotently.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import manifest as manifest_module
from .object_store import ObjectNotFound, PreconditionFailed

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
PRODUCTION = "production"
QUARANTINE = "quarantine"

STATES = (PENDING, APPROVED, REJECTED, PRODUCTION, QUARANTINE)

# Business rule, so it lives in code where a config mistake cannot widen it. Note the
# absence of pending -> production: skipping review is the transition this whitelist exists
# to refuse.
ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    PENDING: (APPROVED, REJECTED, QUARANTINE),
    APPROVED: (PRODUCTION, REJECTED),
    REJECTED: (PENDING,),
    QUARANTINE: (PENDING,),
    PRODUCTION: (REJECTED,),
}

PASS_VERDICTS = ("PASS", "PASS_WITH_MINOR_EDITS")
QUARANTINE_VERDICTS = ("FAIL", "NOT_ASSESSABLE")

MANIFEST_NAME = "audio/manifest.json"
TRANSITION_MARKER = "_transition.json"
QUARANTINE_REASON = "quarantine_reason.json"
HISTORY_PREFIX = "_history"

_MATERIAL_ID_RE = re.compile(r"^\d{8}-[a-z0-9][a-z0-9-]*-[0-9a-f]{8}$")


def new_material_id(scenario_key: str, *, today: Optional[str] = None) -> str:
    """`{YYYYMMDD}-{scenario_key}-{8 hex}` (design.md §8.2).

    The date prefix makes the bucket browsable by a human and gives lifecycle rules something to
    match on; the random suffix avoids a collision between two materials generated for the same
    scenario on the same day. The id is the material's lifelong identity -- it does not change
    when the material moves between states, because the history is keyed on it.
    """
    import secrets

    if not scenario_key or "/" in scenario_key:
        raise StateStoreError("scenario_key {0!r} is not usable as a path segment".format(scenario_key))
    stamp = today or _utc_now()[:10].replace("-", "")
    return "{0}-{1}-{2}".format(stamp, scenario_key, secrets.token_hex(4))


class StateStoreError(RuntimeError):
    """A state-flow operation cannot proceed."""


class IllegalTransition(StateStoreError):
    """Not on the whitelist."""


class TransitionInFlight(StateStoreError):
    """Another transition holds the marker for this material."""


class MaterialNotFound(StateStoreError):
    """No state directory holds this material."""


class InjectedCrash(RuntimeError):
    """Raised by the crash_after test hook, mid-transition, on purpose."""


@dataclass
class MaterialRef:
    material_id: str
    state: str
    scenario_key: str
    complete: bool
    # True while this copy is the abandoned side of a transition; filtered from listings.
    shadow: bool = False

    @property
    def prefix(self) -> str:
        return "{0}/{1}/{2}/".format(self.state, self.scenario_key, self.material_id)


@dataclass
class TransitionRecord:
    material_id: str
    from_state: Optional[str]
    to_state: str
    actor: str
    reason: str
    at: str
    object_count: int
    scenario_key: str = ""
    recovered: bool = False


@dataclass
class ReconcileReport:
    dry_run: bool
    forward_rolled: List[str] = field(default_factory=list)
    incomplete: List[str] = field(default_factory=list)
    shadows: List[str] = field(default_factory=list)
    orphans: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.forward_rolled or self.incomplete or self.shadows or self.orphans)


def verdict_of(audit: dict) -> str:
    verdict = audit.get("verdict") if isinstance(audit, dict) else None
    if verdict not in PASS_VERDICTS + QUARANTINE_VERDICTS:
        # Unknown verdicts go to quarantine, not pending: an unreadable audit is a reason to
        # withhold from review, and defaulting the other way would let a broken generation
        # step publish silently.
        return "NOT_ASSESSABLE"
    return verdict


def route_for_verdict(verdict: str) -> str:
    return QUARANTINE if verdict in QUARANTINE_VERDICTS else PENDING


def quarantine_reason(audit: dict) -> dict:
    """Machine-readable, not prose: the review UI and metrics both consume this."""
    findings = [f for f in (audit.get("findings") or []) if isinstance(f, dict)]
    verdict = verdict_of(audit)
    return {
        "verdict": verdict,
        "assessable": bool(audit.get("assessable", verdict != "NOT_ASSESSABLE")),
        "critical_count": sum(f.get("severity") == "critical" for f in findings),
        "major_count": sum(f.get("severity") == "major" for f in findings),
        "minor_count": sum(f.get("severity") == "minor" for f in findings),
        "score_total": (audit.get("score") or {}).get("total"),
        "findings_digest": [
            {
                "severity": f.get("severity"),
                "rule": f.get("rule"),
                "turn_index": f.get("turn_index"),
            }
            for f in findings
            if f.get("severity") in ("critical", "major")
        ],
        "reason_code": (
            "no_assessable_script" if verdict == "NOT_ASSESSABLE" else "hard_defects_present"
        ),
        "has_audio": False,
    }


def _dumps(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")


class StateStore:
    """The only interface a review system needs. It never exposes S3 key layout."""

    def __init__(self, store, *, clock=None) -> None:
        self._store = store
        self._clock = clock or _utc_now

    @property
    def object_store(self):
        """The backing object store.

        Exposed for synthesize.py, which has to write clips into the prefix this store owns.
        Read-only access to the same instance, so there is still exactly one place that decides
        key layout -- a caller that used this to build keys by hand would be defeating the point.
        """
        return self._store

    # -- keys ------------------------------------------------------------------
    def _prefix(self, state: str, scenario_key: str, material_id: str) -> str:
        if state not in STATES:
            raise StateStoreError("unknown state {0!r}".format(state))
        return "{0}/{1}/{2}/".format(state, scenario_key, material_id)

    def _history_prefix(self, material_id: str) -> str:
        return "{0}/{1}/".format(HISTORY_PREFIX, material_id)

    # -- publish ---------------------------------------------------------------
    def publish_material(
        self,
        material: dict,
        blueprint: dict,
        audit: dict,
        *,
        scenario_key: str,
        material_id: str,
        actor: str = "system",
        audio: Optional[Dict[str, bytes]] = None,
        manifest: Optional[dict] = None,
        degraded: bool = False,
        degraded_reason: Optional[str] = None,
    ) -> MaterialRef:
        """Route by verdict, write the JSON, write audio, then the manifest sentinel last."""
        if not scenario_key or "/" in scenario_key:
            # scenario_key is a path segment supplied by the backend. The material's own
            # `scenario` field is a full English sentence and must never be used here.
            raise StateStoreError("scenario_key {0!r} is not usable as a path segment".format(scenario_key))
        verdict = verdict_of(audit)
        state = route_for_verdict(verdict)
        prefix = self._prefix(state, scenario_key, material_id)

        self._store.put(prefix + "material.json", _dumps(material))
        self._store.put(prefix + "blueprint.json", _dumps(blueprint))
        self._store.put(prefix + "audit.json", _dumps(audit))

        if state == QUARANTINE:
            # No audio by design: the material was never selected, so it was never
            # synthesised. That is not incompleteness (R10).
            self._store.put(prefix + QUARANTINE_REASON, _dumps(quarantine_reason(audit)))
        else:
            if manifest is None:
                raise StateStoreError(
                    "a {0} material needs a manifest; without one the read side treats it "
                    "as incomplete and it will never become visible".format(state)
                )
            if degraded:
                manifest = dict(manifest)
                manifest["degraded"] = True
                manifest["degraded_reason"] = degraded_reason or "revise/re-audit skipped"
            for key, body in (audio or {}).items():
                self._store.put(prefix + key, body)
            missing = [
                clip["key"]
                for clip in manifest.get("clips", [])
                if not self._store.head(prefix + clip["key"])
            ]
            if missing:
                # The sentinel is withheld rather than written over a gap, so the frontend
                # cannot read a half-built material (design.md §4.5).
                raise StateStoreError(
                    "refusing to write the manifest: audio objects missing {0}".format(missing)
                )
            self._store.put(prefix + MANIFEST_NAME, _dumps(manifest))

        self._write_history(
            TransitionRecord(
                material_id=material_id,
                from_state=None,
                to_state=state,
                actor=actor,
                reason="published with verdict {0}".format(verdict),
                at=self._clock(),
                object_count=len(self._store.list_keys(prefix)),
                scenario_key=scenario_key,
            )
        )
        return MaterialRef(material_id, state, scenario_key, complete=True)

    # -- discovery -------------------------------------------------------------
    def _scan(self, state: str) -> List[MaterialRef]:
        refs: List[MaterialRef] = []
        seen = set()
        for key in self._store.list_keys(state + "/"):
            parts = key[len(state) + 1 :].split("/")
            if len(parts) < 2:
                continue
            scenario_key, material_id = parts[0], parts[1]
            if (scenario_key, material_id) in seen:
                continue
            seen.add((scenario_key, material_id))
            prefix = self._prefix(state, scenario_key, material_id)
            keys = self._store.list_keys(prefix)
            # A directory holding nothing but the transition marker is an intent, not a
            # material: that is the step-1 crash state. Counting it as a copy would make
            # locate() see the material in two places and refuse to act, when in fact the
            # source is still the only real copy.
            if all(k.endswith(TRANSITION_MARKER) for k in keys):
                continue
            has_manifest = (prefix + MANIFEST_NAME) in keys
            # Quarantine is complete without audio; every other state needs the sentinel.
            complete = has_manifest or (state == QUARANTINE and (prefix + QUARANTINE_REASON) in keys)
            refs.append(MaterialRef(material_id, state, scenario_key, complete=complete))
        return refs

    def _all_refs(self) -> List[MaterialRef]:
        refs: List[MaterialRef] = []
        for state in STATES:
            refs.extend(self._scan(state))
        return self._mark_shadows(refs)

    def _mark_shadows(self, refs: List[MaterialRef]) -> List[MaterialRef]:
        """Resolve the ghost window: when a material sits in two state directories, the one
        holding _transition.json is the destination and therefore authoritative; the other is
        residue awaiting deletion. This read rule is what makes the window invisible to
        callers without a distributed transaction (design.md §9.2).

        The destination must be *complete* before the source is hidden. Crashing at step 1 or
        mid-copy also leaves a marker, but with a destination that has no manifest yet; hiding
        the source then would make the material vanish from every listing while a perfectly
        good copy of it still exists. Only after step 3 -- both sides complete -- is there
        anything to disambiguate.
        """
        by_id: Dict[str, List[MaterialRef]] = {}
        for ref in refs:
            by_id.setdefault(ref.material_id, []).append(ref)
        for material_id, group in by_id.items():
            if len(group) < 2:
                continue
            authoritative = [
                r
                for r in group
                if r.complete and self._store.head(r.prefix + TRANSITION_MARKER)
            ]
            if len(authoritative) == 1:
                for ref in group:
                    ref.shadow = ref is not authoritative[0]
            else:
                # No marker, or markers on both sides: nothing proves which is authoritative,
                # so nothing is hidden. reconcile reports it for a human rather than the
                # store picking arbitrarily and possibly hiding the surviving copy.
                for ref in group:
                    ref.shadow = False
        return refs

    def list_materials(
        self,
        state: str,
        *,
        scenario_key: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> dict:
        if state not in STATES:
            raise StateStoreError("unknown state {0!r}".format(state))
        refs = [r for r in self._all_refs() if r.state == state]
        refs = [r for r in refs if r.complete and not r.shadow]
        if scenario_key:
            refs = [r for r in refs if r.scenario_key == scenario_key]
        refs.sort(key=lambda r: r.material_id)
        if cursor:
            refs = [r for r in refs if r.material_id > cursor]
        page = refs[:limit]
        return {
            "items": page,
            "next_cursor": page[-1].material_id if len(refs) > limit else None,
        }

    def locate(self, material_id: str) -> MaterialRef:
        candidates = [r for r in self._all_refs() if r.material_id == material_id]
        live = [r for r in candidates if not r.shadow]
        if not live:
            raise MaterialNotFound(material_id)
        if len(live) > 1:
            raise StateStoreError(
                "{0} appears in {1} with no transition marker to arbitrate; run "
                "reconcile".format(material_id, [r.state for r in live])
            )
        return live[0]

    def get_material(self, material_id: str) -> dict:
        ref = self.locate(material_id)
        bundle = {"state": ref.state, "scenario_key": ref.scenario_key, "material_id": material_id}
        for name, field_name in (
            ("material.json", "material"),
            ("blueprint.json", "blueprint"),
            ("audit.json", "audit"),
            (MANIFEST_NAME, "manifest"),
            (QUARANTINE_REASON, "quarantine_reason"),
        ):
            try:
                bundle[field_name] = json.loads(self._store.get(ref.prefix + name))
            except ObjectNotFound:
                bundle[field_name] = None
        return bundle

    def presign_audio(self, material_id: str, *, ttl_seconds: int = 3600) -> Dict[int, str]:
        """{turn_index: url}. The frontend never sees a key, so a state change cannot break
        its links and it cannot come to depend on the directory layout."""
        ref = self.locate(material_id)
        manifest = json.loads(self._store.get(ref.prefix + MANIFEST_NAME))
        return {
            clip["turn_index"]: self._store.presign(ref.prefix + clip["key"], ttl_seconds)
            for clip in manifest.get("clips", [])
        }

    # -- transition ------------------------------------------------------------
    def transition(
        self,
        material_id: str,
        to_state: str,
        *,
        actor: str,
        reason: str,
        crash_after: Optional[int] = None,
    ) -> TransitionRecord:
        """Six steps, forward-only. `crash_after` is the test hook for §9.2's crash table."""
        ref = self.locate(material_id)
        from_state = ref.state
        if to_state not in STATES:
            raise StateStoreError("unknown state {0!r}".format(to_state))
        if to_state == from_state:
            raise IllegalTransition("{0} is already {1}".format(material_id, from_state))
        if to_state not in ALLOWED_TRANSITIONS[from_state]:
            raise IllegalTransition(
                "{0} -> {1} is not allowed; permitted: {2}".format(
                    from_state, to_state, ", ".join(ALLOWED_TRANSITIONS[from_state])
                )
            )

        src = ref.prefix
        dst = self._prefix(to_state, ref.scenario_key, material_id)
        marker = {
            "material_id": material_id,
            "from_state": from_state,
            "to_state": to_state,
            "scenario_key": ref.scenario_key,
            "actor": actor,
            "reason": reason,
            "started_at": self._clock(),
        }
        # Step 1: intent marker doubles as the mutex (create-if-absent).
        try:
            self._store.put(dst + TRANSITION_MARKER, _dumps(marker), if_none_match=True)
        except PreconditionFailed:
            raise TransitionInFlight(
                "a transition of {0} into {1} is already in flight".format(material_id, to_state)
            )
        if crash_after == 1:
            raise InjectedCrash("after writing the transition marker")

        record = self._run_transition(marker, crash_after=crash_after)
        return record

    def _run_transition(
        self, marker: dict, *, crash_after: Optional[int] = None, recovered: bool = False
    ) -> TransitionRecord:
        """Steps 2-6. Idempotent, so recovery is a re-run rather than a separate path."""
        material_id = marker["material_id"]
        scenario_key = marker["scenario_key"]
        from_state, to_state = marker["from_state"], marker["to_state"]
        src = self._prefix(from_state, scenario_key, material_id)
        dst = self._prefix(to_state, scenario_key, material_id)

        source_keys = [k for k in self._store.list_keys(src) if not k.endswith(TRANSITION_MARKER)]
        # Step 2: everything except the sentinel.
        body_keys = [k for k in source_keys if not k.endswith(MANIFEST_NAME)]
        for index, key in enumerate(body_keys):
            self._store.copy(key, dst + key[len(src) :])
            if crash_after == 2 and index * 2 >= len(body_keys):
                raise InjectedCrash("mid-copy, {0}/{1} objects".format(index + 1, len(body_keys)))

        # Step 3: the manifest last, so the destination only becomes visible once whole.
        for key in source_keys:
            if key.endswith(MANIFEST_NAME):
                self._store.copy(key, dst + key[len(src) :])
        if crash_after == 3:
            raise InjectedCrash("after copying the manifest: both copies are now complete")

        # Step 4: drop the source.
        self._store.delete(source_keys)
        if crash_after == 4:
            raise InjectedCrash("after deleting the source, before writing history")

        # Step 5: history, outside the state directories.
        record = TransitionRecord(
            material_id=material_id,
            from_state=from_state,
            to_state=to_state,
            actor=marker.get("actor", "unknown"),
            reason=marker.get("reason", ""),
            at=self._clock(),
            object_count=len(source_keys),
            scenario_key=scenario_key,
            recovered=recovered,
        )
        self._write_history(record)
        if crash_after == 5:
            raise InjectedCrash("after writing history, before clearing the marker")

        # Step 6: release the mutex.
        self._store.delete([dst + TRANSITION_MARKER])
        return record

    def _write_history(self, record: TransitionRecord) -> None:
        key = "{0}{1}-{2}-{3}.json".format(
            self._history_prefix(record.material_id),
            record.at.replace(":", "").replace("-", "").replace(".", ""),
            record.from_state or "new",
            record.to_state,
        )
        self._store.put(key, _dumps(asdict(record)))

    def history(self, material_id: str) -> List[TransitionRecord]:
        """Readable after the material has moved, and after it has been deleted."""
        records: List[TransitionRecord] = []
        for key in sorted(self._store.list_keys(self._history_prefix(material_id))):
            payload = json.loads(self._store.get(key))
            records.append(TransitionRecord(**payload))
        records.sort(key=lambda r: r.at)
        return records

    # -- reconcile -------------------------------------------------------------
    def reconcile(self, *, scenario_key: Optional[str] = None, dry_run: bool = True) -> ReconcileReport:
        """Roll every abandoned transition forward and report anything else off-model.

        Forward, never back: before step 4 the source is still complete and after step 3 the
        destination is complete, so no crash point can lose data. Forward recovery is the
        same code path as the original attempt, which is why it needs no separate testing
        of its own correctness -- only that it is reached.
        """
        report = ReconcileReport(dry_run=dry_run)

        for state in STATES:
            for key in self._store.list_keys(state + "/"):
                if not key.endswith(TRANSITION_MARKER):
                    continue
                marker = json.loads(self._store.get(key))
                if scenario_key and marker.get("scenario_key") != scenario_key:
                    continue
                material_id = marker.get("material_id")
                report.forward_rolled.append(material_id)
                report.actions.append(
                    "roll forward {0}: {1} -> {2}".format(
                        material_id, marker.get("from_state"), marker.get("to_state")
                    )
                )
                if dry_run:
                    continue
                src = self._prefix(marker["from_state"], marker["scenario_key"], material_id)
                if self._store.list_keys(src):
                    self._run_transition(marker, recovered=True)
                else:
                    # Crashed at or after step 4: the move is done. Finish the bookkeeping
                    # only, and do not re-copy -- the source is legitimately empty.
                    if not self.history(material_id) or self.history(material_id)[-1].to_state != marker["to_state"]:
                        self._write_history(
                            TransitionRecord(
                                material_id=material_id,
                                from_state=marker["from_state"],
                                to_state=marker["to_state"],
                                actor=marker.get("actor", "unknown"),
                                reason=marker.get("reason", ""),
                                at=self._clock(),
                                object_count=len(self._store.list_keys(
                                    self._prefix(marker["to_state"], marker["scenario_key"], material_id)
                                )),
                                scenario_key=marker["scenario_key"],
                                recovered=True,
                            )
                        )
                    self._store.delete([key])

        for ref in self._all_refs():
            if scenario_key and ref.scenario_key != scenario_key:
                continue
            label = "{0}/{1}".format(ref.state, ref.material_id)
            if ref.shadow:
                report.shadows.append(label)
            elif not ref.complete:
                report.incomplete.append(label)
            if not _MATERIAL_ID_RE.match(ref.material_id):
                report.orphans.append(label)
        return report

    # -- integrity -------------------------------------------------------------
    def verify_material(self, material_id: str) -> dict:
        """Every clip the manifest promises actually exists, and the manifest aligns with
        the material. Cheaper than a resynthesis and catches an object deleted underneath."""
        ref = self.locate(material_id)
        bundle = self.get_material(material_id)
        if ref.state == QUARANTINE:
            return {
                "ok": bundle.get("quarantine_reason") is not None,
                "state": ref.state,
                "expected_audio": False,
            }
        manifest = bundle.get("manifest")
        if not manifest:
            return {"ok": False, "state": ref.state, "errors": ["no manifest"]}
        missing = [
            clip["key"]
            for clip in manifest.get("clips", [])
            if not self._store.head(ref.prefix + clip["key"])
        ]
        alignment = manifest_module.check_alignment(
            manifest, bundle["material"], bundle.get("blueprint")
        )
        return {
            "ok": not missing and alignment["ok"],
            "state": ref.state,
            "missing_audio": missing,
            "alignment": alignment,
        }


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
