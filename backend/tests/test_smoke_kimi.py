"""The paid smoke must stop even when a model repeatedly requests tools."""

from types import SimpleNamespace

import pytest
from strands import Agent, tool
from strands.models.openai import OpenAIModel

from backend.scripts.smoke_kimi import CallBudget


@pytest.mark.asyncio
async def test_real_agent_stops_a_repeating_tool_loop_at_budget():
    class RepeatingModel(OpenAIModel):
        calls = 0

        async def stream(self, *args, **kwargs):
            self.calls += 1
            response = SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
                    id=f"test-call-{self.calls}", type="function",
                    function=SimpleNamespace(name="ping", arguments="{}"),
                )]),
            )])
            for event in self._format_non_streaming_response(response):
                yield event

    tool_calls = []

    @tool
    def ping() -> str:
        """Return a synthetic acknowledgement."""
        tool_calls.append(True)
        return "ok"

    model = RepeatingModel(model_id="offline")
    budget = CallBudget(2)
    agent = Agent(model=model, tools=[ping], hooks=[budget],
                  callback_handler=None, retry_strategy=None)
    result = await agent.invoke_async("ping")
    assert model.calls == budget.calls == 2
    assert len(tool_calls) == 2
    assert "Synthetic smoke call budget exhausted" in str(result)
