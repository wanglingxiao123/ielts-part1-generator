"""Prompt migration, streaming isolation and isolated trial contracts (no model/network)."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from prompt_config import catalogue
from prompt_config.context import bind, current_values, metadata
from prompt_config.store import MemoryStore, PromptStore
from backend import prompt_runtime


def bundle(**values):
    result = PromptStore(MemoryStore()).bundled()
    result["values"].update(values)
    return result


def test_catalogue_defaults_are_exact_source_anchors():
    root = Path(__file__).resolve().parents[2]
    for row in catalogue._rows():
        filename = row["source"].split(":")[0]
        tree = ast.parse((root / filename).read_text())
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if row["kind"] == "role":
            assert row["original"] in literals
            assert catalogue.apply_role(row["original"]).encode() == row["original"].encode()
        else:
            assert any(row["default"] in text for text in literals)
            original = row["template"] + '\n\n{"input":"unchanged 中文"}'
            assert catalogue.apply_task(original, row["label"]).encode() == original.encode()
    assert len(catalogue.entries()) == 18
    assert all(set(row) == {"id", "name", "purpose", "default", "source", "protected"}
               for row in catalogue.entries())


def test_only_leading_business_anchor_is_replaced_and_contract_is_preserved():
    row = next(r for r in catalogue._rows() if r["id"] == "task.assessment_patch")
    original = row["template"] + "\n" + row["default"]
    with bind(bundle(**{row["id"]: "按教师要求修改。", "role.audit": "独立评审。"})):
        actual = catalogue.apply_task(original, row["label"])
        assert actual == "按教师要求修改。" + original[len(row["default"]):]
        role = next(r for r in catalogue._rows() if r["id"] == "role.audit")
        assert catalogue.apply_role(role["original"]) == "独立评审。\n\n" + role["protected"]
        with pytest.raises(ValueError, match="anchor"):
            catalogue.apply_task("unexpected source", row["label"])
    assert current_values() == catalogue.defaults()


@pytest.mark.asyncio
async def test_audit_supplement_leaves_guarded_json_byte_identical():
    from backend.steps.agent_steps import _invoke

    sent = []
    async def invoke(message):
        sent.append(message)
        return "{}"
    agent = SimpleNamespace(system_prompt="Original protected system", invoke_async=invoke)
    original = '{"material": {"script": "中文"}, "metrics": {}}'
    with bind(bundle(**{"task.audit": "额外关注表达清晰度。"})):
        await _invoke(agent, original, "audit")
    assert sent == [original]
    assert agent.system_prompt == "Original protected system\n\n额外关注表达清晰度。"


@pytest.mark.asyncio
async def test_release_references_fail_closed(monkeypatch):
    store = PromptStore(MemoryStore())
    monkeypatch.setattr(prompt_runtime, "PromptStore", lambda: store)
    ref = store.bundled()
    assert await prompt_runtime.resolve_bundle({"_prompt_release": ref}) == ref
    for invalid in ({}, {"release_id": "missing", "digest": "abc"},
                    {"release_id": ref["release_id"], "digest": "wrong"}, "forged"):
        with pytest.raises(ValueError):
            await prompt_runtime.resolve_bundle({"_prompt_release": invalid})
    with pytest.raises(ValueError):
        await prompt_runtime.resolve_bundle({}, required=True)


@pytest.mark.asyncio
async def test_concurrent_streams_keep_release_in_children_and_cleanup(monkeypatch):
    store = PromptStore(MemoryStore())
    monkeypatch.setattr(prompt_runtime, "PromptStore", lambda: store)
    a = store.snapshot({**catalogue.defaults(), "task.generation": "A"}, "test")
    b = store.snapshot({**catalogue.defaults(), "task.generation": "B"}, "test")
    cleanup = []
    async def producer(payload):
        expected = payload["expected"]
        try:
            for index in range(3):
                async def child():
                    await asyncio.sleep(0)
                    return current_values()["task.generation"]
                assert await asyncio.create_task(child()) == expected
                yield {"type": "progress", "index": index}
        finally:
            cleanup.append(current_values()["task.generation"])
    async def consume(config, expected):
        events = []
        async for event in prompt_runtime.stream_with_prompts(
            producer, {"_prompt_release": config, "expected": expected}
        ):
            assert metadata() == {}
            assert event["prompt_release"] == {k: config[k] for k in ("release_id", "digest")}
            events.append(event)
            await asyncio.sleep(0)
        return events
    results = await asyncio.gather(consume(a, "A"), consume(b, "B"))
    assert [len(events) for events in results] == [3, 3]
    assert sorted(cleanup) == ["A", "B"]
    assert metadata() == {}


@pytest.mark.asyncio
async def test_early_stream_close_preserves_cleanup_binding(monkeypatch):
    config = bundle()
    async def resolve(_):
        return config
    monkeypatch.setattr(prompt_runtime, "resolve_bundle", resolve)
    cleaned = []
    async def producer(_):
        try:
            yield {}
        finally:
            cleaned.append(metadata())
    stream = prompt_runtime.stream_with_prompts(producer, {})
    await anext(stream)
    await asyncio.create_task(stream.aclose())
    assert cleaned == [{k: config[k] for k in ("release_id", "digest")}]
    assert metadata() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("action,handler", [
    ("generate", "_generate"),
    ("generate_sets", "_generate_sets"),
    ("revise_questions_from_comments", "_revise_questions_from_comments"),
    ("replan_questions_from_comments", "_replan_questions_from_comments"),
    ("revise_material_from_comments", "_revise_material_from_comments"),
    ("revise_material_local", "_revise_material_local"),
])
async def test_every_runtime_model_stream_binds_through_iteration(monkeypatch, action, handler):
    from backend import app
    config = bundle()
    async def resolve(_):
        return config
    async def producer(_):
        assert metadata()["release_id"] == config["release_id"]
        yield {"type": "done"}
    monkeypatch.setattr(prompt_runtime, "resolve_bundle", resolve)
    monkeypatch.setattr(app, handler, producer)
    stream = await app.invoke({"action": action, "_prompt_release": config})
    assert metadata() == {}
    events = [event async for event in stream]
    assert events[0]["prompt_release"]["digest"] == config["digest"]
    assert metadata() == {}


@pytest.mark.asyncio
async def test_trial_is_fresh_tool_free_and_preserves_task_schema(monkeypatch):
    import strands
    from backend.model import provider

    store = PromptStore(MemoryStore())
    config = store.snapshot({**catalogue.defaults(), "task.assessment_patch": "仅修改示例。"}, "test")
    monkeypatch.setattr(prompt_runtime, "PromptStore", lambda: store)
    monkeypatch.setattr(provider, "resolve_model_id", lambda _: "test-model")
    monkeypatch.setattr(provider, "build_model", lambda **_: object())
    calls = []
    class Agent:
        def __init__(self, **kwargs):
            assert kwargs["tools"] == []
            self.options = kwargs
        async def invoke_async(self, message):
            calls.append((self, message, metadata(), provider.current_model_id()))
            return "trial output"
    monkeypatch.setattr(strands, "Agent", Agent)
    payload = dict(prompt_id="task.assessment_patch", input="synthetic example",
                   model_id="test-model", _prompt_release=config)
    for _ in range(2):
        assert await prompt_runtime.prompt_trial(payload) == {"output": "trial output", "model_id": "test-model"}
    assert calls[0][0] is not calls[1][0]
    assert calls[0][1].startswith("仅修改示例。")
    assert '"turn_edits"' in calls[0][1]
    assert calls[0][2]["release_id"] == config["release_id"]
    assert calls[0][3] == "test-model"
    assert metadata() == {}
    with pytest.raises(ValueError, match="unknown prompt_id"):
        await prompt_runtime.prompt_trial({**payload, "prompt_id": "arbitrary"})


@pytest.mark.asyncio
async def test_trial_timeout(monkeypatch):
    import strands
    from backend.model import provider
    monkeypatch.setattr(prompt_runtime, "TRIAL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(provider, "resolve_model_id", lambda _: "test")
    monkeypatch.setattr(provider, "build_model", lambda **_: object())
    class Agent:
        def __init__(self, **kwargs):
            pass
        async def invoke_async(self, message):
            await asyncio.Event().wait()
    monkeypatch.setattr(strands, "Agent", Agent)
    with pytest.raises(TimeoutError):
        await prompt_runtime.prompt_trial(dict(
            prompt_id="role.generate", input="example", _prompt_release=bundle()))


@pytest.mark.asyncio
async def test_keepalive_none_is_forwarded_without_conversion(monkeypatch):
    config = bundle()
    async def resolve(_):
        return config
    monkeypatch.setattr(prompt_runtime, "resolve_bundle", resolve)
    async def producer(_):
        yield None
        yield {"type": "completed"}
    events = [event async for event in prompt_runtime.stream_with_prompts(producer, {})]
    assert events[0] is None
    assert events[1]["prompt_release"]["release_id"] == config["release_id"]


def test_stored_assessment_and_revision_keep_prompt_provenance():
    from audio_storage.object_store import InMemoryObjectStore
    from backend.orchestration.slot_store import SlotStore
    store = SlotStore(InMemoryObjectStore())
    config = bundle()
    with bind(config):
        store.save_question_version("material-1", "v1", {"id": "v1"})
        store.save_question_revision("material-1", "r1", {"request_id": "r1"})
    for record in (store.load_question_version("material-1", "v1"),
                   store.load_question_revision("material-1", "r1")):
        assert record["prompt_release"] == {k: config[k] for k in ("release_id", "digest")}
