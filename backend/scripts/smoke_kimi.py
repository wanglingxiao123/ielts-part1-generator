#!/usr/bin/env python
"""Opt-in, paid Kimi transport check using synthetic data only.

Run with IELTS_MODEL_AUTH=profile and IELTS_MODEL_PROFILE set locally.
At most three model turns, 1024 output tokens each; no SDK/agent retries.
This checks JSON and tool transport, not IELTS quality or China connectivity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from strands import Agent, tool
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeModelCallEvent

from backend.model import provider
from backend.steps.call import extract_json


class CallBudget(HookProvider):
    """Stop tool loops before they can exceed the synthetic test budget."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: object) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_call)

    def before_call(self, event: BeforeModelCallEvent) -> None:
        if self.calls >= self.limit:
            event.cancel = "Synthetic smoke call budget exhausted"
        else:
            self.calls += 1


async def run_smoke() -> dict:
    if provider.MODEL_ID not in ("moonshotai.kimi-k2.5", "us.moonshotai.kimi-k3"):
        raise ValueError("This smoke is limited to the implemented Kimi models")
    model = provider.build_model(max_output_tokens=1024, reasoning_effort="low")
    model.client_args.update({"max_retries": 0, "timeout": 60.0})
    json_budget = CallBudget(1)
    json_agent = Agent(
        model=model, tools=[], callback_handler=None, retry_strategy=None,
        hooks=[json_budget], system_prompt="Return only a JSON object. Be concise.",
    )
    result = await json_agent.invoke_async('Return exactly {"status":"ok"}.')
    json_ok = extract_json(str(result)) == {"status": "ok"}
    tool_calls = []

    @tool
    def lookup_test_booking(booking_id: str) -> str:
        """Look up a synthetic test booking using its booking_id."""
        tool_calls.append(booking_id)
        if booking_id != "TEST-001":
            return "Unknown test booking"
        return json.dumps({"room": "Cedar", "seats": 7})

    tool_budget = CallBudget(2)
    tool_agent = Agent(
        model=model, tools=[lookup_test_booking], callback_handler=None,
        retry_strategy=None, hooks=[tool_budget],
        system_prompt=(
            "Use lookup_test_booking exactly once to look up the requested booking. "
            "Then return only JSON with room and seats copied from the tool result."
        ),
    )
    result = await tool_agent.invoke_async("Look up booking TEST-001.")
    tool_ok = (
        tool_calls == ["TEST-001"]
        and extract_json(str(result)) == {"room": "Cedar", "seats": 7}
    )
    return {
        "json_ok": json_ok, "tool_roundtrip_ok": tool_ok,
        "model_calls": json_budget.calls + tool_budget.calls,
        "tool_calls": len(tool_calls),
    }


def main() -> int:
    # SDK exception logs can contain response bodies; this probe emits only safe summaries.
    logging.disable(logging.CRITICAL)
    started = time.monotonic()
    try:
        report = asyncio.run(asyncio.wait_for(run_smoke(), timeout=180))
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        return 1
    report.update({
        "model_id": provider.MODEL_ID, "model_region": provider.REGION,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    })
    print(json.dumps(report))
    return 0 if report["json_ok"] and report["tool_roundtrip_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
