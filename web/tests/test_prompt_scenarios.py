import io
import json
import time
from types import SimpleNamespace

import pytest

from prompt_config.catalogue import defaults
from prompt_config.store import Conflict, MemoryStore, PromptStore
from web.prompt_scenarios import start, detail, history, side_key
from .conftest import register


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False
    def iter_lines(self, **kwargs):
        for event in self.events:
            yield ('data: ' + json.dumps(event)).encode()
    def close(self):
        self.closed = True


class Runtime:
    configured = True
    def __init__(self):
        self.calls = []
        self.streams = []
        self.fail_baseline = False
        self.omit_terminal = False
        self.wrong_version = False
    def invoke(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        action = payload['action']
        if action == 'list_scenarios':
            body = {'scenarios': {'categories': [{'scenarios': [{'id': 'booking-hotel', 'title_zh': '酒店预订'}]}]}}
        elif action == 'list_models':
            body = {'models': [{'id': 'model'}]}
        else:
            assert action == 'generate_sets'
            if self.fail_baseline and payload['batch_id'].endswith('baseline'):
                raise RuntimeError('synthetic failure')
            events = [
                None,
                {'type': 'stage', 'stage': 'generating', 'detail': {'attempt': 1}, 'prompt_release': payload['_prompt_release']},
                None,
                {'type': 'material_completed', 'material': {'listening_material_parts': [{'script': {'turns': [{'speaker': 'A', 'text': 'Hello'}]}}]}},
            ]
            if self.wrong_version:
                events[1]['prompt_release'] = {'release_id': 'wrong', 'digest': 'wrong'}
            if not self.omit_terminal:
                events.append({'type': 'request_completed', 'status': 'succeeded', 'sets': [{'questions': {'ok': True, 'package': {'question_face': {'groups': [], 'questions': [], 'instructions': []}, 'answer_key': [], 'evidence': []}}}], 'slots': []})
            stream = Stream(events)
            self.streams.append(stream)
            return 'text/event-stream', stream, 'session'
        return 'application/json', io.BytesIO(json.dumps(body).encode()), 'session'


class DeferredExecutions:
    def __init__(self):
        self.work = []
    def start(self, key, producer):
        self.work.append(producer)
    def run(self):
        for producer in self.work:
            producer(lambda frame: None)


def setup(tier):
    tier.prompts = PromptStore(MemoryStore())
    tier.runtime = Runtime()
    tier.executions = DeferredExecutions()
    values = defaults()
    values['role.audit'] += '\nRequire corrected figures to be repeated.'
    tier.prompts.save_draft(values, 'admin', None)
    return {'request_id': 'a' * 32, 'scenario_id': 'booking-hotel', 'model_id': 'model',
            'baseline': 'active', 'candidate': 'draft', 'active_etag': None,
            'draft_etag': tier.prompts.state()['draft_etag']}


def test_admin_only_start_history_detail(client):
    register(client, 'admin@example.com')
    client.cookies.clear()
    register(client, 'user@example.com')
    assert client.post('/api/admin/prompt-scenarios', json={}).status_code == 403
    assert client.get('/api/admin/prompt-scenarios').status_code == 403
    assert client.get('/api/admin/prompt-scenarios/' + 'a' * 32).status_code == 403


def test_sides_pin_versions_and_do_not_publish_or_submit(tier):
    body = setup(tier)
    result = start(tier, body, 'admin')
    run_id = result['run_id']
    before = detail(tier, run_id)
    assert before['status'] == 'running'
    assert before['versions']['baseline']['release_id'] != before['versions']['candidate']['release_id']
    # Publish a new configuration while the already-reserved run has not started its workers.
    tier.prompts.publish('admin', None, draft_etag=body['draft_etag'])
    published = tier.prompts.resolve()['release_id']
    tier.executions.run()
    after = detail(tier, run_id)
    assert after['status'] == 'completed'
    assert tier.prompts.resolve()['release_id'] == published
    calls = [p for p, kw in tier.runtime.calls if p['action'] == 'generate_sets']
    assert len(calls) == 2 and all(p['count'] == 1 for p in calls)
    assert calls[0]['group_id'] != calls[1]['group_id']
    assert calls[0]['_prompt_release']['release_id'] == before['versions']['baseline']['release_id']
    assert all(stream.closed for stream in tier.runtime.streams)
    assert all(row['counts']['generating'] == 1 for row in after['sides'].values())
    assert after['prompt_changes'][0]['id'] == 'role.audit'
    assert history(tier, 0)['items'][0]['status'] == 'completed'
    assert tier.prompts.storage.read('scenario-lease.json')[0]['expires'] == 0


def test_idempotency_and_global_lease(tier):
    body = setup(tier)
    first = start(tier, body, 'admin')
    assert start(tier, body, 'admin') == {'run_id': first['run_id'], 'reused': True}
    assert len(tier.executions.work) == 2
    with pytest.raises(Conflict):
        start(tier, dict(body, model_id='other'), 'admin')
    with pytest.raises(Conflict):
        start(tier, dict(body, request_id='b' * 32), 'admin')
    tier.executions.run()
    assert start(tier, body, 'admin')['reused'] is True
    assert len(tier.executions.work) == 2


def test_one_side_failure_does_not_cancel_other_side(tier):
    body = setup(tier)
    tier.runtime.fail_baseline = True
    run_id = start(tier, body, 'admin')['run_id']
    tier.executions.run()
    result = detail(tier, run_id)
    assert result['status'] == 'finished_with_issues'
    assert result['sides']['baseline']['status'] == 'failed'
    assert result['sides']['candidate']['status'] == 'succeeded'
    assert tier.prompts.storage.read('scenario-lease.json')[0]['expires'] == 0


@pytest.mark.parametrize('failure,expected', [('omit_terminal', 'interrupted'), ('wrong_version', 'failed')])
def test_missing_terminal_or_wrong_version_never_succeeds(tier, failure, expected):
    body = setup(tier)
    setattr(tier.runtime, failure, True)
    run_id = start(tier, body, 'admin')['run_id']
    tier.executions.run()
    assert all(side['status'] == expected for side in detail(tier, run_id)['sides'].values())


def test_stale_running_history_remains_visible_and_does_not_restart(tier):
    body = setup(tier)
    run_id = start(tier, body, 'admin')['run_id']
    for side in ('baseline', 'candidate'):
        row, token = tier.prompts.storage.read(side_key(run_id, side))
        tier.prompts.storage.write(side_key(run_id, side), dict(row, deadline_at=time.time() - 1), token)
    assert all(side['status'] == 'interrupted' for side in detail(tier, run_id)['sides'].values())
    assert len(tier.executions.work) == 2


def test_same_versions_invalid_scenario_and_stale_tokens_make_no_paid_calls(tier):
    body = setup(tier)
    for changes in ({'candidate': 'active'}, {'scenario_id': 'unknown'}, {'draft_etag': 'stale'}, {'active_etag': 'stale'}, {'candidate': 'made-up-release'}):
        with pytest.raises(ValueError):
            start(tier, dict(body, **changes), 'admin')
    assert not tier.executions.work
    assert not any(p['action'] == 'generate_sets' for p, kw in tier.runtime.calls)


def test_real_background_execution_finishes_after_post_returns(client, tier):
    from web.execution_manager import ExecutionManager
    register(client)
    body = setup(tier)
    tier.executions = ExecutionManager()
    response = client.post('/api/admin/prompt-scenarios', json=body)
    assert response.status_code == 200
    run_id = response.json()['run_id']
    for _ in range(100):
        record = detail(tier, run_id)
        if record['status'] != 'running':
            break
        time.sleep(.01)
    assert record['status'] == 'completed'
    assert client.get('/api/admin/prompt-scenarios').json()['items'][0]['run_id'] == run_id
    assert client.get('/api/admin/prompt-scenarios/' + run_id).status_code == 200


def test_storage_failure_before_start_never_invokes_generation(tier, monkeypatch):
    body = setup(tier)
    original = tier.prompts.storage.write
    def fail(key, value, expected=None):
        if key.endswith('/candidate.json') and value['status'] == 'queued':
            raise RuntimeError('storage unavailable')
        return original(key, value, expected)
    monkeypatch.setattr(tier.prompts.storage, 'write', fail)
    with pytest.raises(RuntimeError):
        start(tier, body, 'admin')
    assert not tier.executions.work
    assert tier.prompts.storage.read('scenario-lease.json')[0]['expires'] == 0


def test_historical_versions_can_be_compared_without_changing_active(tier):
    body = setup(tier)
    b1 = tier.prompts.publish('admin', None, draft_etag=body['draft_etag'])
    values = dict(b1['values']); values['role.audit'] += '\nB2 rules'
    state = tier.prompts.state()
    tier.prompts.save_draft(values, 'admin', state['draft_etag'])
    b2 = tier.prompts.publish('admin', state['active_etag'], draft_etag=tier.prompts.state()['draft_etag'])
    run_id = start(tier, dict(body, baseline=b1['release_id'], candidate=b2['release_id']), 'admin')['run_id']
    tier.executions.run()
    run = detail(tier, run_id)
    assert run['versions']['baseline']['source_release'] == b1['release_id']
    assert run['versions']['candidate']['source_release'] == b2['release_id']
    assert tier.prompts.resolve()['release_id'] == b2['release_id']
