"""Request parsing for the AgentCore entrypoint.

Kept out of app.py so the protocol adapter stays free of business logic: swapping AgentCore for
another host should mean rewriting app.py and nothing else.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .orchestration.batch import DEFAULT_CONCURRENCY, BatchRequest, Budget
from .orchestration.scenarios import InvalidScenario, Scenario, ScenarioCatalogue

__all__ = ["BadRequest", "parse_generate_request"]


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
    if len(slots) > catalogue.max_batch:
        raise BadRequest(
            "batch of %d exceeds the maximum of %d; the 15-minute synchronous limit is a hard "
            "platform constraint" % (len(slots), catalogue.max_batch)
        )
    return slots


def parse_generate_request(
    catalogue: ScenarioCatalogue, payload: Dict[str, Any]
) -> BatchRequest:
    slots = _expand(catalogue, payload)
    concurrency = payload.get("concurrency", DEFAULT_CONCURRENCY)
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
