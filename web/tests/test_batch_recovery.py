import time
from types import SimpleNamespace

import pytest
from prompt_config.store import MemoryStore, PromptStore
from web.batch_history import BatchHistory
from web.batch_store import InMemoryBatchStore
from web.batch_recovery import BatchRecovery, RecoveryBusy
from web.execution_manager import ExecutionManager


@pytest.fixture
def setup():
    storage = InMemoryBatchStore()
    record = dict(batch_id='web-recovery', owner='teacher@example.com', created_at=time.time(),
                  updated_at=time.time(), state='complete', requested_total=9,
                  scenarios=[{'scenario_key': 'booking-hotel', 'count': 9}],
                  model_id='original-model', materials=[dict(material_id='m%d' % n,
                    scenario_key='booking-hotel', index=n-1, slot_id='slot-%d' % n) for n in range(1, 8)])
    storage.save_index('web-recovery', record)
    docs = {f'web-recovery-slot-{n}': dict(status='incomplete', model_id='original-model',
                slots=[{'slot_id':'slot-1','state':'material_done','material_id':f'pending-{n}'}]) for n in (8,9)}
    tier = SimpleNamespace(history=BatchHistory(storage), prompts=PromptStore(MemoryStore()),
        executions=ExecutionManager(), fanout_concurrency=20,
        slot_state=SimpleNamespace(load_request=lambda key: docs.get(key)))
    calls = []
    tier._start_batch_refill = lambda *args, **kwargs: calls.append((args, kwargs))
    return tier, BatchRecovery(tier), docs, calls, record


def test_seven_delivered_seats_are_untouched_and_resume_uses_original_request_ids(setup):
    tier, service, docs, calls, original = setup
    plan = service.plan('web-recovery','teacher@example.com')
    assert plan['delivered']==7 and [t['action'] for t in plan['targets']]==['resume','resume']
    result=service.start('web-recovery','teacher@example.com','a'*32)
    assert [t['request_id'] for t in calls[0][0][2]]==['web-recovery-slot-8','web-recovery-slot-9']
    assert calls[0][1]['model_id']=='original-model'
    assert tier.history.store.load_index('web-recovery')['materials']==original['materials']
    assert service.start('web-recovery','teacher@example.com','a'*32)['reused']
    assert len(calls)==1
    with pytest.raises(RecoveryBusy):service.start('web-recovery','teacher@example.com','b'*32)
    # Replays after terminal output still find the old acknowledgement.
    tier.history.fail_refill('web-recovery',result['execution_id'],'paused again')
    calls[0][1]['on_finished']()
    assert service.start('web-recovery','teacher@example.com','a'*32)['execution_id']==result['execution_id']
    assert len(calls)==1
    service.start('web-recovery','teacher@example.com','b'*32)
    assert [t['request_id'] for t in calls[1][0][2]]==['web-recovery-slot-8','web-recovery-slot-9']


def test_only_uncheckpointed_missing_seat_gets_a_new_request(setup):
    tier, service, docs, calls, _=setup
    docs['web-recovery-slot-9']=None
    service.start('web-recovery','teacher@example.com','a'*32)
    targets=calls[0][0][2]
    assert targets[0]['request_id']=='web-recovery-slot-8'
    assert targets[1]['request_id']=='web-recovery-refill-1-slot-9'
    calls[0][1]['on_finished']()
    tier.history.fail_refill('web-recovery','web-recovery-refill-1','paused')
    docs[targets[1]['request_id']]={'status':'incomplete','slots':[{'state':'questions_pending'}]}
    assert service.plan('web-recovery','teacher@example.com')['targets'][1]['request_id']==targets[1]['request_id']


def test_start_failure_releases_reservation_and_same_request_reports_failure(setup):
    tier, service, docs, calls, _=setup
    def fail(*args,**kwargs):raise RuntimeError('thread start failed')
    tier._start_batch_refill=fail
    with pytest.raises(RuntimeError):service.start('web-recovery','teacher@example.com','a'*32)
    assert tier.history.store.load_index('web-recovery')['refill_execution']['status']=='failed'
    assert service.start('web-recovery','teacher@example.com','a'*32)['status']=='failed'
    tier._start_batch_refill=lambda *args,**kwargs:calls.append(args)
    service.start('web-recovery','teacher@example.com','b'*32)
    assert len(calls)==1


def test_checkpoint_read_error_never_turns_into_regeneration(setup):
    tier, service, docs, calls, _=setup
    def fail(key):raise RuntimeError('storage unavailable')
    tier.slot_state.load_request=fail
    with pytest.raises(RuntimeError):service.start('web-recovery','teacher@example.com','a'*32)
    assert not calls


@pytest.mark.parametrize('status', ['running','system_failure'])
def test_running_and_system_failed_requests_do_not_launch(setup,status):
    tier, service, docs, calls, _=setup
    for doc in docs.values():doc.update(status=status,updated_at=time.time())
    with pytest.raises(ValueError):service.start('web-recovery','teacher@example.com','a'*32)
    assert not calls


def test_owner_and_submitted_expired_batches_cannot_resume(setup):
    tier, service, docs, calls, record=setup
    with pytest.raises(PermissionError):service.start('web-recovery','other@example.com','a'*32)
    for changes in [{'submitted_at':time.time()}, {'created_at':time.time()-40*86400}]:
        tier.history.store.save_index('web-recovery',dict(record,**changes))
        assert service.plan('web-recovery','teacher@example.com')['status']=='readonly'
        with pytest.raises(ValueError):service.start('web-recovery','teacher@example.com','a'*32)
    assert not calls


def test_quiet_original_execution_cannot_be_restarted_using_ui_stale_threshold(setup):
    tier, service, docs, calls, record=setup
    tier.history.store.save_index('web-recovery',dict(record,state='running',updated_at=time.time()-1200))
    assert service.plan('web-recovery','teacher@example.com')['status']=='running'
    assert not calls


def test_stale_refill_without_a_live_lease_can_resume_after_runtime_stops(setup):
    tier, service, docs, calls, record=setup
    record.update(state='running',created_at=time.time()-8000,updated_at=time.time()-5000,
                  execution_deadline_at=time.time()-100,
                  refill_execution={'execution_id':'web-recovery-refill-1','status':'running','targets':[]})
    tier.history.store.save_index('web-recovery',record)
    service.start('web-recovery','teacher@example.com','a'*32)
    assert len(calls)==1


@pytest.mark.parametrize("retry_fault", [False, True])
def test_real_web_recovery_endpoint_resumes_two_children_and_reuses_completed_acknowledgement(
    auth, static_dir, fanout_runtime, setup, retry_fault,
):
    from fastapi.testclient import TestClient
    from web.app import WebTier
    from .conftest import register
    from .test_batch_history import material_event, request_completed, _await_materials
    original_tier, _, docs, _, original = setup
    if retry_fault:
        docs['web-recovery-slot-8']={'status':'system_failure','model_id':'original-model','slots':[dict(
            state='exhausted',system_fault=True,material_id='qualified',last_failure={
                'reason':'question_stage_crashed','detail':'ModelCallError: APIError'})]}
    original['owner']='a@amazon.com'
    original_tier.history.store.save_index('web-recovery',original)
    tier=WebTier(auth,fanout_runtime,str(static_dir),history=original_tier.history,
                 slot_state=SimpleNamespace(load_request=lambda key:docs.get(key)))
    tier.prompts=original_tier.prompts
    with TestClient(tier.app) as client:
        register(client,'a@amazon.com')
        read=client.get('/api/batch-history/web-recovery/recovery')
        assert read.status_code==200 and read.json()['delivered']==7
        first=client.post('/api/batch-history/web-recovery/recovery',json={'request_id':'a'*32})
        assert first.status_code==202
        for slot in ('slot-8','slot-9'):
            body=fanout_runtime.body_for(slot)
            body.push_event(material_event('slot-1','booking-hotel','m-new-'+slot))
            body.push_event(request_completed('m-new-'+slot))
            body.finish()
        _await_materials(tier.history.store,'web-recovery',9)
        deadline=time.monotonic()+2
        while tier.executions.is_running('batch-refill:'+first.json()['execution_id']) and time.monotonic()<deadline:
            time.sleep(.01)
        second=client.post('/api/batch-history/web-recovery/recovery',json={'request_id':'a'*32})
        assert second.status_code==202 and second.json()['reused']
        assert {call['batch_id'] for call in fanout_runtime.calls}=={'web-recovery-slot-8','web-recovery-slot-9'}
        assert {call['model_id'] for call in fanout_runtime.calls}=={'original-model'}
        assert len(fanout_runtime.calls)==2
        if retry_fault:
            assert next(c for c in fanout_runtime.calls if c['batch_id'].endswith('slot-8'))['retry_token']=='a'*32
            assert 'retry_token' not in next(c for c in fanout_runtime.calls if c['batch_id'].endswith('slot-9'))
        after=client.get('/api/batch-history/web-recovery/recovery').json()
        assert after['status']=='complete' and after['delivered']==9
        assert original['materials']==tier.history.store.load_index('web-recovery')['materials'][:7]
        client.cookies.clear()
        register(client,'other@amazon.com')
        assert client.get('/api/batch-history/web-recovery/recovery').status_code==403
        assert client.post('/api/batch-history/web-recovery/recovery',json={'request_id':'b'*32}).status_code==403


def test_custom_runtime_hash_does_not_make_delivered_seats_missing(setup):
    tier, service, docs, calls, record = setup
    record['scenarios'] = [{'scenario_key':'custom','count':9}]
    record['custom_label'] = 'A custom booking scenario'
    for row in record['materials']:
        row['scenario_key'] = 'custom-0f3506d3'
    tier.history.store.save_index('web-recovery', record)
    assert [t['slot_id'] for t in service.plan('web-recovery','teacher@example.com')['targets']] == ['slot-8','slot-9']
    service.start('web-recovery','teacher@example.com','a'*32)
    assert [t['slot_id'] for t in calls[0][0][2]] == ['slot-8','slot-9']
    assert tier.history.store.load_index('web-recovery')['materials'] == record['materials']


def test_model_fault_has_explicit_retry_preserving_request_and_token(setup):
    tier, service, docs, calls, record = setup
    row = dict(slot_id='slot-1',state='exhausted',system_fault=True,material_id='qualified',
               last_failure={'reason':'question_stage_crashed','detail':'ModelCallError: question audit call failed: APIError'})
    docs['web-recovery-slot-8']={'status':'system_failure','slots':[row]}
    plan=service.plan('web-recovery','teacher@example.com')
    assert [t['action'] for t in plan['targets']]==['retry','resume']
    service.start('web-recovery','teacher@example.com','a'*32)
    target=calls[0][0][2][0]
    assert target['request_id']=='web-recovery-slot-8' and target['retry_token']=='a'*32
    assert 'retry_token' not in calls[0][0][2][1]
    assert service.start('web-recovery','teacher@example.com','a'*32)['reused']
    assert len(calls)==1


@pytest.mark.parametrize('changes', [{}, {'material_id':None}, {'system_fault':False},
    {'replaced_by':'slot-1r1'}, {'state':'complete'},
    {'last_failure':{'reason':'material_unreadable','detail':'ModelCallError: APIError'}},
    {'last_failure':{'reason':'question_stage_crashed','detail':'TypeError: programming error'}}])
def test_runtime_and_web_retry_allowlist_match(changes):
    from backend.orchestration.delivery import retryable_question_fault as runtime_check
    from backend.orchestration.slot_store import SlotRecord
    from web.batch_recovery import retryable_question_fault as web_check
    row=dict(batch_id='b',slot_id='slot-1',scenario_id='booking-hotel',state='exhausted',
        system_fault=True,material_id='m',last_failure={'reason':'question_stage_crashed','detail':'ModelCallError: APIError'})
    row.update(changes)
    assert web_check(row)==runtime_check(SlotRecord.from_record(row))==(not changes)
