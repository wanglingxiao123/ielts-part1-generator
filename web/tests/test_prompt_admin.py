from .conftest import register
from prompt_config.catalogue import defaults


def test_ordinary_user_cannot_read_or_modify_prompts(client):
    register(client, "admin@amazon.com")
    client.cookies.clear()
    register(client, "user@amazon.com")
    for path in ("/api/admin/prompts", "/api/admin/prompts/test/" + "a" * 32,
                 "/api/admin/prompts/releases/" + "a" * 32):
        assert client.get(path).status_code == 403
    for operation in ("draft", "publish", "rollback", "test"):
        assert client.post("/api/admin/prompts/" + operation,
                           json={"is_admin": True, "values": defaults()}).status_code == 403


def test_admin_draft_publish_conflict(client, tier):
    register(client)
    state = client.get("/api/admin/prompts").json()
    assert state["catalogue"] and state["skills"]
    values = defaults()
    values[next(iter(values))] += "\nBe concise."
    result = client.post("/api/admin/prompts/draft", json={"values": values, "expected_etag": None})
    assert result.status_code == 200
    assert tier.prompts.resolve()["values"] == defaults()
    state = result.json()
    result = client.post("/api/admin/prompts/publish", json={"draft_etag": state["draft_etag"],
                                                           "expected_etag": state["active_etag"]})
    assert result.status_code == 200
    assert tier.prompts.resolve()["values"] == values
    assert client.post("/api/admin/prompts/publish", json={"draft_etag": state["draft_etag"],
                                                          "expected_etag": None}).status_code == 409


def test_public_proxy_cannot_inject_config_or_trial(client, runtime):
    register(client)
    for body in ({"action": "prompt_trial"}, {"action": "generate", "_prompt_release": {}},
                 {"action": "list_models", "_prompt_overrides": {}}):
        assert client.post("/api/invocations", json=body).status_code == 403
    assert runtime.calls == []


def test_trial_setup_failure_releases_lease(tier, monkeypatch):
    import pytest
    from web.prompt_admin import start_trial
    tier.prompts.save_draft(defaults(), "admin", None)
    etag = tier.prompts.state()["draft_etag"]
    def fail():
        raise ValueError("bad active release")
    monkeypatch.setattr(tier.prompts, "resolve", fail)
    with pytest.raises(ValueError):
        start_trial(tier, {"prompt_id": "role.generate", "input": "example",
                           "draft_etag": etag}, "admin")
    lease, _ = tier.prompts.storage.read("trial-lease.json")
    assert lease["expires"] == 0


def test_revision_binding_failure_is_terminal(tier, monkeypatch):
    from unittest.mock import Mock
    service = Mock()
    tier.question_versions = service
    def fail(*args, **kwargs):
        raise ValueError("unavailable")
    monkeypatch.setattr(tier.prompts, "pin", fail)
    record = {"request_id": "r1"}
    response = tier._start_revision_execution({}, "m1", record)
    assert response.status_code == 503
    service.fail_request.assert_called_once()
    assert not service.bind_prompt_release.called


def test_revision_followup_inherits_root_release(tier, monkeypatch):
    from unittest.mock import Mock
    import web.app as app_module
    ref = tier.prompts.pin("revision:root")
    values = defaults()
    values["role.generate"] += "\nLater rules."
    tier.prompts.save_draft(values, "admin", None)
    tier.prompts.publish("admin", None, draft_etag=tier.prompts.state()["draft_etag"])
    tier.question_versions = Mock()
    tier.question_versions.load_request.return_value = {"request_id": "root"}
    tier.question_versions.bind_prompt_release.side_effect = lambda material, record, release: record
    tier.executions = Mock()
    tier.executions.start.return_value = (object(), True)
    monkeypatch.setattr(app_module, "_execution_response", lambda *a, **k: "stream")
    assert tier._start_revision_execution({}, "m1", {"request_id": "followup",
                                                    "source_request_id": "root"}) == "stream"
    assert tier.question_versions.bind_prompt_release.call_args.args[2] == ref


def test_refill_binding_failure_ends_reservation(tier, monkeypatch):
    import pytest
    tier.history.store.save_index("b1", {"batch_id": "b1", "owner": "owner", "requested_total": 1,
        "scenarios": [{"scenario_key": "hotel", "count": 1}], "materials": [], "state": "complete"})
    reservation = tier.history.reserve_refill("b1", [{"scenario_key": "hotel", "index": 0}], owner="owner")
    def fail(*args, **kwargs):
        raise ValueError("bad config")
    monkeypatch.setattr(tier.prompts, "pin", fail)
    with pytest.raises(ValueError):
        tier._start_batch_refill("b1", reservation["execution_id"], reservation["targets"])
    record = tier.history.store.load_index("b1")
    assert record["refill_execution"]["status"] == "failed"
    assert record["state"] == "complete"


def test_batch_children_and_duplicate_submission_keep_one_release(tier, monkeypatch):
    from unittest.mock import Mock
    import web.app as app_module
    captured = []
    class Fan:
        def __init__(self, runtime, children, slots, **kwargs):
            captured.append(children)
        def custom_label(self):
            return ""
    monkeypatch.setattr(app_module, "FanOut", Fan)
    monkeypatch.setattr(app_module, "_execution_response", lambda *a, **k: "stream")
    tier.executions = Mock()
    tier.executions.start.return_value = (object(), True)
    payload = {"action": "generate_sets", "scenarios": ["hotel"], "count": 2}
    tier._fanned_out_generate(payload, owner="a", idempotency_key="same")
    refs = [child.payload["_prompt_release"] for child in captured[0]]
    assert len(refs) == 2 and refs[0] == refs[1]
    values = defaults()
    values["role.generate"] += "Later rules."
    tier.prompts.save_draft(values, "admin", None)
    tier.prompts.publish("admin", None, draft_etag=tier.prompts.state()["draft_etag"])
    tier._fanned_out_generate(payload, owner="a", idempotency_key="same")
    assert all(child.payload["_prompt_release"] == refs[0] for child in captured[1])


def test_history_and_default_restore_are_admin_only(client):
    register(client, "admin@example.com")
    client.cookies.clear()
    register(client, "reader@example.com")
    assert client.get('/api/admin/prompts/tests').status_code == 403
    assert client.post('/api/admin/prompts/restore-defaults', json={}).status_code == 403


def test_restore_defaults_retains_published_history_and_draft(client, tier):
    register(client)
    values = defaults()
    values['task.audit'] = 'Hotel audit rules'
    tier.prompts.save_draft(values, 'admin', None)
    original = tier.prompts.publish('admin', None, draft_etag=tier.prompts.state()['draft_etag'])
    state = client.get('/api/admin/prompts').json()
    body = {'release_id': state['bundled']['release_id'], 'expected_etag': state['active_etag']}
    restored = client.post('/api/admin/prompts/restore-defaults', json=body)
    assert restored.status_code == 200
    result = restored.json()
    assert result['active']['values'] == defaults()
    assert result['active']['release_id'] != state['bundled']['release_id']
    assert result['history'][1]['release_id'] == original['release_id']
    assert result['draft']['values'] == values
    assert client.post('/api/admin/prompts/restore-defaults', json=body).status_code == 409
    body['release_id'] = 'bundled-stale'
    body['expected_etag'] = result['active_etag']
    assert client.post('/api/admin/prompts/restore-defaults', json=body).status_code == 409


def test_trial_persists_input_and_exact_prompt_snapshots(tier, monkeypatch):
    import io
    import json
    from types import SimpleNamespace
    from web.prompt_admin import start_trial, trial_detail
    import web.prompt_admin as module
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(module.threading, 'Thread', InlineThread)
    tier.runtime = SimpleNamespace(invoke=lambda *a, **kw: (None, io.BytesIO(
        json.dumps({'output': 'audited', 'model_id': 'model'}).encode()), None))
    values = defaults()
    values['task.audit'] = 'Parking must state its fee.'
    tier.prompts.save_draft(values, 'admin', None)
    result = start_trial(tier, {'prompt_id': 'task.audit', 'input': 'hotel dialogue',
        'model_id': 'model', 'draft_etag': tier.prompts.state()['draft_etag']}, 'admin')
    record, _ = tier.prompts.storage.read('tests/' + result['test_id'] + '.json')
    assert record['status'] == 'completed'
    assert record['input'] == 'hotel dialogue'
    assert record['baseline_prompt'] == ''
    assert record['draft_prompt'] == values['task.audit']
    assert tier.prompts.resolve(record['baseline_snapshot_release'])['values'] == defaults()
    tier.prompts.publish('admin', None, draft_etag=tier.prompts.state()['draft_etag'])
    assert trial_detail(tier, record)['baseline_prompt'] == ''


def test_history_pages_legacy_records_and_never_invents_input(client, tier):
    register(client)
    import time
    snapshot = tier.prompts.snapshot(defaults(), 'admin')
    for i in range(23):
        test_id = f'{i:032x}'
        tier.prompts.storage.write('tests/' + test_id + '.json', {
            'test_id': test_id, 'status': 'completed', 'created_at': i,
            'prompt_id': 'task.audit', 'model_id': 'kimi', 'baseline': 'old output',
            'baseline_release': 'bundled-not-preserved', 'draft_release': snapshot['release_id']})
    first = client.get('/api/admin/prompts/tests').json()
    assert len(first['items']) == 20 and first['next_offset'] == 20
    assert first['items'][0]['test_id'] == f'{22:032x}'
    assert first['items'][0]['has_input'] is False
    assert 'baseline' not in first['items'][0]
    second = client.get('/api/admin/prompts/tests?offset=20').json()
    assert len(second['items']) == 3 and second['next_offset'] is None
    detail = client.get('/api/admin/prompts/test/' + f'{22:032x}').json()
    assert 'input' not in detail and 'baseline_prompt' not in detail
    assert detail['draft_prompt'] == ''
    assert client.get('/api/admin/prompts/tests?offset=-1').status_code == 400
    tier.prompts.storage.write('tests/' + 'a' * 32 + '.json', {
        'test_id': 'a' * 32, 'status': 'running', 'created_at': time.time() - 1300})
    assert client.get('/api/admin/prompts/test/' + 'a' * 32).json()['status'] == 'failed'
