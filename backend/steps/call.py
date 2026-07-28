"""One-shot model call plus JSON extraction, shared by the three steps.

Every step creates a fresh ``Agent``, uses it once and drops it (design.md §3.1). No
conversation history crosses step boundaries: state lives in the orchestrator's Python objects
only. This is what makes the re-audit genuinely memoryless -- it cannot inherit the first
audit's conclusions because there is no shared session to inherit them from.

No tools are ever registered. The validation scripts in particular are never exposed to the
model: if it could call them it could also decide it had passed, and the whole point of
design.md §3 is that the *orchestrator* decides that.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from strands import Agent

__all__ = ["ModelCallError", "call_json", "extract_json"]

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ModelCallError(RuntimeError):
    """The model produced nothing usable.

    Classified as infrastructure in design.md §3.3 (alongside throttling and 5xx), so it
    retries on the backoff budget and does not consume a generation attempt. A truncated or
    unfenced response says nothing about the material's quality -- spending a quality retry on
    it would let one transport hiccup condemn a material that was fine.
    """


def _decode_first_object(text: str) -> Optional[Any]:
    """Find the first balanced JSON object in ``text``.

    Models sometimes wrap output in prose despite instructions. ``raw_decode`` from each ``{``
    is used rather than a regex because braces nest, and the outer object is what matters.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def extract_json(text: str) -> Dict[str, Any]:
    """Parse one JSON object out of a model response."""
    stripped = (text or "").strip()
    if not stripped:
        raise ModelCallError("model returned an empty response")

    candidates: List[str] = [stripped]
    candidates.extend(match.strip() for match in _FENCE_RE.findall(stripped))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value

    value = _decode_first_object(stripped)
    if value is not None:
        return value
    raise ModelCallError("model response contained no JSON object: %r" % stripped[:200])


async def call_json(model: Any, system_prompt: str, user_message: str) -> Dict[str, Any]:
    """Run a single memoryless model call and parse its JSON response.

    ``tools`` is left empty deliberately -- see the module docstring.
    """
    agent = Agent(model=model, system_prompt=system_prompt, tools=[], callback_handler=None)
    try:
        result = await agent.invoke_async(user_message)
    except Exception as exc:  # noqa: BLE001 - SDK raises provider-specific errors
        raise ModelCallError("model call failed: %s: %s" % (type(exc).__name__, exc))
    return extract_json(str(result))
