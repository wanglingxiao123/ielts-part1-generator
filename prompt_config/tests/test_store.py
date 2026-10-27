import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from prompt_config.catalogue import defaults
from prompt_config.context import bind, current_values, metadata
from prompt_config.store import Conflict, MemoryStore, PromptStore


def changed():
    values = defaults()
    key = next(iter(values))
    values[key] += "\nUse clear language."
    return values


def publish(store):
    store.save_draft(changed(), "admin", None)
    return store.publish("admin", None, draft_etag=store.state()["draft_etag"])

def test_release_diff_tracks_predecessor_including_rollback_and_first_defaults():
    store = PromptStore(MemoryStore())
    first = publish(store)
    one = store.release_detail(first["release_id"])
    assert one["comparison_note"] is None
    assert one["changes"][0]["before"] == defaults()[one["changes"][0]["id"]]
    second_values = dict(first["values"])
    second_values["role.audit"] += "\nCheck corrections."
    state = store.state()
    store.save_draft(second_values, "admin", state["draft_etag"])
    second = store.publish("admin", state["active_etag"], draft_etag=store.state()["draft_etag"])
    two = store.release_detail(second["release_id"])
    assert two["base_release_id"] == first["release_id"]
    assert [c["id"] for c in two["changes"]] == ["role.audit"]
    assert store.state()["history"][0]["changed_ids"] == ["role.audit"]
    rollback = store.publish("admin", store.state()["active_etag"], release_id=first["release_id"])
    back = store.release_detail(rollback["release_id"])
    assert back["base_release_id"] == second["release_id"]
    assert back["changes"][0]["before"] == second_values["role.audit"]
    assert back["changes"][0]["after"] == first["values"]["role.audit"]
    # An unrelated trial snapshot is not exposed as a publication.
    trial = store.snapshot(defaults(), "admin")
    with pytest.raises(ValueError):
        store.release_detail(trial["release_id"])


def test_legacy_first_publication_labels_default_comparison_as_reference():
    store = PromptStore(MemoryStore())
    legacy = store.snapshot(changed(), "admin", previous=None)
    store.storage.write("active.json", {"release_id": legacy["release_id"]})
    assert "参考" in store.release_detail(legacy["release_id"])["comparison_note"]


def test_draft_publish_rollback_and_task_pinning():
    store = PromptStore(MemoryStore())
    original = store.resolve()
    pin = store.pin("batch:old")
    release = publish(store)
    assert store.resolve()["release_id"] == release["release_id"]
    assert store.pin("batch:old") == pin
    assert store.pin("batch:new")["release_id"] == release["release_id"]
    state = store.state()
    rollback = store.publish("other", state["active_etag"], release_id=original["release_id"])
    assert rollback["values"] == defaults()
    assert rollback["actor"] == "other"
    assert len(store.state()["history"]) == 2
    assert store.resolve(release["release_id"])["values"] == changed()


def test_save_has_no_effect_and_stale_writers_rejected():
    store = PromptStore(MemoryStore())
    store.save_draft(changed(), "one", None)
    assert store.resolve()["values"] == defaults()
    with pytest.raises(Conflict):
        store.save_draft(defaults(), "two", None)
    with pytest.raises(Conflict):
        store.publish("admin", None, draft_etag="stale")
    store.publish("admin", None, draft_etag=store.state()["draft_etag"])
    with pytest.raises(Conflict):
        store.publish("admin", None, draft_etag=store.state()["draft_etag"])


def test_concurrent_pins_have_one_winner():
    store = PromptStore(MemoryStore())
    publish(store)
    with ThreadPoolExecutor(max_workers=10) as executor:
        refs = list(executor.map(lambda _: store.pin("same"), range(30)))
    assert all(ref == refs[0] for ref in refs)


def test_missing_corrupt_and_incompatible_release_fail_closed():
    store = PromptStore(MemoryStore())
    with pytest.raises(ValueError):
        store.resolve("missing")
    release = publish(store)
    key = "releases/" + release["release_id"] + ".json"
    bad, etag = store.storage.read(key)
    bad["values"] = defaults()
    store.storage.write(key, bad, etag)
    with pytest.raises(ValueError):
        store.resolve(release["release_id"])
    with pytest.raises(ValueError):
        store.save_draft({"invented": "rule"}, "admin", None)


@pytest.mark.asyncio
async def test_context_isolation_across_overlapping_tasks_and_reset():
    store = PromptStore(MemoryStore())
    base, draft = store.bundled(), store.snapshot(changed(), "a")
    async def run(bundle):
        with bind(bundle):
            await asyncio.sleep(0)
            assert current_values() == bundle["values"]
            assert metadata()["digest"] == bundle["digest"]
            # Caller mutation cannot change the bound configuration.
            current_values().clear()
            return current_values()
    assert await asyncio.gather(run(base), run(draft)) == [base["values"], draft["values"]]
    assert current_values() == defaults()
    assert metadata() == {}


def test_local_file_store_shares_releases_across_instances(tmp_path):
    from prompt_config.store import FileStore
    first = PromptStore(FileStore(tmp_path))
    release = publish(first)
    second = PromptStore(FileStore(tmp_path))
    assert second.resolve() == release
    assert second.resolve(release["release_id"]) == release
    assert second.pin("batch:shared") == first.pin("batch:shared")
    with pytest.raises(Conflict):
        second.save_draft(defaults(), "other", None)
