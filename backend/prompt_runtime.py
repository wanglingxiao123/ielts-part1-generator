"""Release binding at the Runtime boundary and isolated administrator trials."""
from __future__ import annotations

import asyncio
import hmac
import logging

from prompt_config.context import bind, metadata
from prompt_config.store import PromptStore

LOG = logging.getLogger(__name__)
TRIAL_TIMEOUT_SECONDS = 120


async def resolve_bundle(payload: dict, *, required: bool = False) -> dict:
    ref = payload.get("_prompt_release")
    if ref is None:
        if required:
            raise ValueError("prompt trial requires an immutable prompt release")
        return await asyncio.to_thread(PromptStore().resolve)
    if (
        not isinstance(ref, dict)
        or not isinstance(ref.get("release_id"), str)
        or not ref["release_id"]
        or not isinstance(ref.get("digest"), str)
        or not ref["digest"]
    ):
        raise ValueError("invalid prompt release reference")
    bundle = await asyncio.to_thread(PromptStore().resolve, ref["release_id"])
    if bundle["release_id"] != ref["release_id"] or not hmac.compare_digest(
        bundle["digest"], ref["digest"]
    ):
        raise ValueError("prompt release digest mismatch")
    return bundle


async def stream_with_prompts(factory, payload: dict):
    """Bind each iterator advancement, including teardown, without leaking across yields.

    Child tasks created by the producer inherit the release. The caller's context
    is restored before yielding, even if it closes the iterator in another task.
    """
    bundle = await resolve_bundle(payload)
    LOG.info("prompt_stream release_id=%s digest=%s", bundle["release_id"], bundle["digest"])
    with bind(bundle):
        stream = factory(payload)
    try:
        while True:
            with bind(bundle):
                try:
                    event = await anext(stream)
                except StopAsyncIteration:
                    break
                stamped = dict(event) if event is not None else None
                if stamped is not None:
                    stamped["prompt_release"] = dict(metadata())
            yield stamped
    finally:
        with bind(bundle):
            await stream.aclose()


async def prompt_trial(payload: dict) -> dict:
    """A fresh, tool-free model call; never runs orchestration or persists artifacts."""
    from strands import Agent

    from prompt_config.catalogue import apply_task, trial_spec
    from .model import provider

    prompt_id = payload.get("prompt_id")
    example = payload.get("input")
    if not isinstance(prompt_id, str) or not isinstance(example, str) or not example.strip():
        raise ValueError("prompt_id and nonempty input are required")
    if len(example) > 100_000:
        raise ValueError("trial input exceeds 100000 characters")

    async def run() -> dict:
        bundle = await resolve_bundle(payload, required=True)
        model_id = await asyncio.to_thread(provider.resolve_model_id, payload.get("model_id"))
        with bind(bundle), provider.use_model(model_id):
            spec = trial_spec(prompt_id)
            system = spec["system_prompt"]
            if spec["supplement"]:
                system += "\n\n" + spec["supplement"]
            system += (
                "\n\nThis is an isolated text-only prompt trial. Tools and filesystem access "
                "are unavailable. Use only the supplied example. Do not claim to have run "
                "skills, validators, a complete generation pipeline, or saved artifacts. "
                "Follow the task's output contract where the supplied example permits it."
            )
            message = spec["message"]
            if message and spec["label"]:
                message = apply_task(message, spec["label"])
            message += ("\n\n## Administrator example\n\n" if message else "") + example
            agent = Agent(
                model=provider.build_model(max_output_tokens=8000, reasoning_effort="medium"),
                system_prompt=system, tools=[], callback_handler=None,
            )
            LOG.info("prompt_trial prompt_id=%s release_id=%s digest=%s model_id=%s",
                     prompt_id, bundle["release_id"], bundle["digest"], model_id)
            return {"output": str(await agent.invoke_async(message)), "model_id": model_id}

    return await asyncio.wait_for(run(), timeout=TRIAL_TIMEOUT_SECONDS)
