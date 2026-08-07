"""Request parsing for the AgentCore entrypoint.

Kept out of app.py so the protocol adapter stays free of business logic: swapping AgentCore for
another host should mean rewriting app.py and nothing else.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .orchestration.batch import MAX_CONCURRENCY, BatchRequest, Budget
from .orchestration.scenarios import InvalidScenario, Scenario, ScenarioCatalogue

__all__ = ["BadRequest", "DeliveryRequest", "parse_generate_request", "parse_delivery_request"]


class BadRequest(ValueError):
    """The payload is unusable. Reported to the caller rather than guessed at."""


def _expand(catalogue: ScenarioCatalogue, payload: Dict[str, Any]) -> List[Scenario]:
    """Turn requested scenario ids (plus counts) into one Scenario per slot."""
    requested = payload.get("scenarios")
    if requested is None:
        requested = []
    if not isinstance(requested, list):
        raise BadRequest("scenarios must be a list of scenario ids")

    counts = payload.get("counts") or {}
    if not isinstance(counts, dict):
        raise BadRequest("counts must be an object mapping scenario id to a count")
    default_count = payload.get("count")

    slots: List[Scenario] = []
    for entry in requested:
        if not isinstance(entry, str):
            raise BadRequest("scenario ids must be strings")
        scenario = catalogue.get(entry)
        if scenario is None:
            raise BadRequest(
                "unknown scenario id %r; call action=list_scenarios for the catalogue" % entry
            )
        count = counts.get(entry, default_count if default_count is not None
                           else scenario.default_count)
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise BadRequest("count for %r must be an integer" % entry)
        if count < 1:
            raise BadRequest("count for %r must be at least 1" % entry)
        slots.extend([scenario] * count)

    custom = payload.get("custom_scenario")
    if custom:
        text = custom.get("prompt_hint") if isinstance(custom, dict) else custom
        try:
            scenario = catalogue.build_custom(str(text))
        except InvalidScenario as exc:
            raise BadRequest(str(exc))
        count = 1
        if isinstance(custom, dict) and custom.get("count") is not None:
            try:
                count = max(1, int(custom["count"]))
            except (TypeError, ValueError):
                raise BadRequest("custom_scenario.count must be an integer")
        slots.extend([scenario] * count)

    if not slots:
        raise BadRequest("no scenarios requested")
    # No ceiling. There used to be one -- `catalogue.max_batch`, justified by the 15-minute
    # synchronous limit on a Runtime invocation -- and it was an artefact of putting every material
    # of a batch inside ONE invocation. The web tier now issues one invocation per material
    # (web/fanout.py), so the 900s wall applies to a single ~150-230s material and a "batch" here
    # is normally one slot. Re-adding a cap would only re-impose a limit the platform no longer
    # asks for; the honest signal to the user is the time estimate, not a refusal.
    return slots


def parse_generate_request(
    catalogue: ScenarioCatalogue, payload: Dict[str, Any]
) -> BatchRequest:
    slots = _expand(catalogue, payload)
    concurrency = payload.get("concurrency", MAX_CONCURRENCY)
    try:
        concurrency = max(1, int(concurrency))
    except (TypeError, ValueError):
        raise BadRequest("concurrency must be an integer")
    budget = Budget()
    if payload.get("hard_limit_seconds") is not None:
        # Present so the time-budget path can be exercised without waiting 15 minutes.
        try:
            budget = Budget(hard_limit=float(payload["hard_limit_seconds"]))
        except (TypeError, ValueError):
            raise BadRequest("hard_limit_seconds must be a number")
    return BatchRequest(slots=slots, concurrency=concurrency, budget=budget)


class DeliveryRequest(object):
    """One exact-count request: N complete material+question sets, under one resumable id.

    Deliberately NOT a ``BatchRequest`` with a flag. The two carry different budgets (``Budget`` has
    one start threshold, ``DeliveryBudget`` has one per stage) and, more importantly, different
    contracts: a ``BatchRequest`` may return fewer materials than it was asked for, and this one may
    not (§8.2(3)). One class with a mode would give the caller two contracts behind one type and no
    way to tell from the type which one applies.

    ``batch_id`` is the resumption key and is REQUIRED, not minted here. A missing id would be silently
    unresumable -- the next invocation would generate a fresh one, find no records and start over -- so
    the caller that owns the identity has to state it. ``group_id`` may differ from it; see
    ``delivery``'s module docstring.
    """

    __slots__ = ("slots", "batch_id", "group_id", "concurrency", "budget")

    def __init__(
        self,
        slots: List[Scenario],
        batch_id: str,
        group_id: Optional[str] = None,
        concurrency: Optional[int] = None,
        budget: Any = None,
    ) -> None:
        self.slots = slots
        self.batch_id = batch_id
        self.group_id = group_id or batch_id
        self.concurrency = concurrency
        self.budget = budget


def parse_delivery_request(
    catalogue: ScenarioCatalogue, payload: Dict[str, Any]
) -> DeliveryRequest:
    """Parse an ``action=generate_sets`` payload.

    Shares ``_expand`` with ``generate``, so the two actions agree on scenario ids, counts and the
    custom scenario, and on the ORDER those expand in -- which is the order the web tier's
    ``plan_children`` mirrors and the frontend laid its cards out in. A second expander would be a
    second place for that order to drift.

    What is not shared is the count semantics downstream: here ``len(slots)`` is a promise rather than
    an upper bound.
    """
    slots = _expand(catalogue, payload)

    batch_id = payload.get("batch_id")
    if not batch_id or not str(batch_id).strip():
        raise BadRequest(
            "batch_id is required for generate_sets: it is the key this request's slot state is "
            "stored under and the id a later invocation resumes it by"
        )
    batch_id = str(batch_id).strip()
    if "/" in batch_id:
        # It becomes a path segment under `_slots/`. A slash would silently write somewhere else and
        # make the request unresumable by the id the caller believes it used.
        raise BadRequest("batch_id must not contain '/'")

    group_id = payload.get("group_id")
    group_id = str(group_id).strip() if group_id else batch_id

    concurrency = payload.get("concurrency")
    if concurrency is not None:
        try:
            concurrency = max(1, int(concurrency))
        except (TypeError, ValueError):
            raise BadRequest("concurrency must be an integer")

    budget = None
    if payload.get("hard_limit_seconds") is not None:
        from .orchestration.delivery import DeliveryBudget

        # The same escape hatch `generate` has, and it earns its keep here for a second reason: the
        # checkpoint path (§8.2(4)) is only reachable by running out of clock, and a test that had to
        # wait 900s for it would not be run.
        try:
            budget = DeliveryBudget(hard_limit=float(payload["hard_limit_seconds"]))
        except (TypeError, ValueError):
            raise BadRequest("hard_limit_seconds must be a number")

    return DeliveryRequest(slots=slots, batch_id=batch_id, group_id=group_id,
                           concurrency=concurrency, budget=budget)
