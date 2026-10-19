"""Behavioral checks against a disposable PostgreSQL restore, never production."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session
from db.engine import engine
from db.schema import Capture, Chunk, MemoryFactCard
from services.memory_api import main
from packages.memory_retrieval.intake import intake_metadata, classify
from packages.memory_retrieval.ranking import search_records, fuse

@pytest.fixture
def dbclient(monkeypatch):
    if engine.url.database != 'byb_memory_repair_test_20261015':
        pytest.skip('Requires disposable restore byb_memory_repair_test_20261015')
    uid='repair-test'
    main.app.dependency_overrides[main.get_user_from_api_key]=lambda: uid
    monkeypatch.setattr(main,'process_capture',lambda _: None)
    def clear():
        with engine.begin() as c:
            c.execute(text("DELETE FROM memoryfactcard WHERE source_capture_id IN (SELECT id FROM capture WHERE user_id=:u)"),dict(u=uid))
            c.execute(text('DELETE FROM chunk WHERE user_id=:u'),dict(u=uid))
            c.execute(text('DELETE FROM memoryrevision WHERE user_id=:u'),dict(u=uid))
            c.execute(text('UPDATE capture SET superseded_by_capture_id=NULL WHERE user_id=:u'),dict(u=uid))
            c.execute(text('DELETE FROM capture WHERE user_id=:u'),dict(u=uid))
    clear()
    yield TestClient(main.app),uid
    clear()
    main.app.dependency_overrides.clear()

def post(client,raw='Decision: the classroom recording protocol uses the green cable.',**kw):
    r=client.post('/v1/captures',json=dict(raw_content=raw,source='repair-test',**kw))
    assert r.status_code==200,r.text
    return r.json()

@pytest.mark.parametrize('kwargs', [{},{'source_event_id':'stable-event'},{'idempotency_key':'request-key'},{'raw':'[cortex-feedback] event_id="stable-embedded" action="done" completed classroom recording task.'}])
def test_repeated_capture_returns_one_id(dbclient,kwargs):
    client,uid=dbclient
    first=post(client,**kwargs);second=post(client,**kwargs)
    assert first['capture_id']==second['capture_id']
    assert not first['duplicate'] and second['duplicate']


def test_concurrent_retries_database_constraint(dbclient):
    _,uid=dbclient
    def request(_):
        with Session(engine) as s:
            return main.create_capture(main.CaptureBody(raw_content='Decision: concurrent event stores precisely one durable observation.',source='repair-test',source_event_id='concurrent'),BackgroundTasks(),idempotency_key_header=None,session=s,user_id=uid)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(request,range(24)))
    assert len({r['capture_id'] for r in results})==1
    assert sum(not r['duplicate'] for r in results)==1


def test_identity_conflicting_content_rejected(dbclient):
    c,_=dbclient;post(c,source_event_id='event-conflict')
    r=c.post('/v1/captures',json=dict(raw_content='Decision: different content for same event should require an explicit revision.',source='repair-test',source_event_id='event-conflict'))
    assert r.status_code==409


def test_distinct_events_with_same_content_are_preserved(dbclient):
    c,_=dbclient
    assert post(c,source_event_id='one')['id']!=post(c,source_event_id='two')['id']


def test_distinct_event_phases_are_preserved(dbclient):
    c,_=dbclient
    assert post(c,source_event_id='one',event_type='request')['id']!=post(c,source_event_id='one',event_type='ack')['id']


def test_observations_on_different_dates_preserved(dbclient):
    c,_=dbclient
    assert post(c,observed_at='2026-10-01T00:00:00')['id']!=post(c,observed_at='2026-10-02T00:00:00')['id']


def test_inactive_retry_does_not_resurrect(dbclient):
    c,_=dbclient;a=post(c,source_event_id='inactive')
    r=c.patch(f"/v1/captures/{a['id']}",json=dict(memory_status='retracted',revision_reason='Incorrect classroom note'))
    assert r.status_code==200
    b=post(c,source_event_id='inactive')
    assert b['id']==a['id'] and b['memory_status']=='retracted'


def test_rules_all_active_and_scoped(dbclient):
    c,_=dbclient
    ids=[post(c,raw=f'coach:watson Decision: global classroom instruction number {i} remains applicable.',memory_visibility='policy')['id'] for i in range(8)]
    project=post(c,raw='coach:watson Decision: use the USB board for the Spike Recorder classroom setup.',project_slug='spike-recorder')['id']
    other=post(c,raw='coach:watson Decision: shipping classroom kits uses the inventory checklist.',project_slug='shipping')['id']
    r=c.get('/v1/rules',params={'project_slug':'spikerecorder'}).json()
    assert not r['truncated'] and r['total']==9
    assert set(ids+[project])=={x['capture_id'] for x in r['rules']}
    assert other not in {x['capture_id'] for x in r['rules']}


def test_supersession_removes_old_rule_preserves_unrelated_rule(dbclient):
    c,_=dbclient
    old=post(c,raw='coach:watson Decision: temporarily hold the classroom recording experiment.')
    unrelated=post(c,raw='coach:watson Decision: keep USB board calibration enabled during recordings.')
    new=post(c,raw='coach:watson Decision: classroom recording hold lifted after calibration verification.',supersedes_capture_ids=[old['id']],revision_reason='Verified completion lifts this one hold')
    ids={r['capture_id'] for r in c.get('/v1/rules').json()['rules']}
    assert ids=={new['id'],unrelated['id']}
    assert c.get(f"/v1/captures/{old['id']}").json()['canonical_capture']['capture_id']==new['id']


def test_supersession_invalid_target_is_atomic(dbclient):
    c,uid=dbclient
    r=c.post('/v1/captures',json=dict(raw_content='coach:watson Decision: this invalid replacement must not be saved.',source='repair-test',supersedes_capture_ids=[2147483600],revision_reason='Invalid target'))
    assert r.status_code==404
    with engine.connect() as s: assert s.execute(text('SELECT count(*) FROM capture WHERE user_id=:u'),dict(u=uid)).scalar_one()==0


def test_index_failure_not_reported_as_verified(dbclient,monkeypatch):
    c,_=dbclient
    def fail(_): raise RuntimeError('temporary failure')
    monkeypatch.setattr(main,'process_capture',fail)
    r=c.post('/v1/captures?sync=true',json=dict(raw_content='Decision: failed indexing must remain stored and retryable without a false success.',source='repair-test')).json()
    assert r['status']=='stored_pending_index_retry' and not r['verified'] and not r['indexed']
    from packages.memory_retrieval.indexer import process_capture
    process_capture(r['id'])
    assert c.get(f"/v1/captures/{r['id']}").json()['index_state']=='ready'


def test_long_capture_tail_is_indexed_and_evidenced(dbclient):
    c,uid=dbclient
    raw=('The classroom protocol begins with a cable check and calibration of the amplifier. '*100)+' Decision: tail-marker-831992 uses the violet cable for the final connection.'
    r=post(c,raw=raw)
    from packages.memory_retrieval.indexer import process_capture,source_sections
    from packages.memory_retrieval.embeddings import get_model
    process_capture(r['id'])
    with engine.connect() as s:
        chunks=s.execute(text('SELECT content FROM chunk WHERE capture_id=:id'),dict(id=r['id'])).scalars().all()
        cards=s.execute(text('SELECT content,provenance_json FROM memoryfactcard WHERE source_capture_id=:id'),dict(id=r['id'])).all()
    assert len(chunks)>1 and any('tail-marker-831992' in s for s in chunks)
    assert all(len(get_model().tokenizer.encode(s,add_special_tokens=False))<=362 for s in chunks)
    assert len(cards)==len(chunks) and all('capture_revision' in row[1] for row in cards)
    with Session(engine) as s:
        found=search_records('tail-marker-831992',5,None,s,uid)
    assert found[0]['capture_id']==r['id']

@pytest.mark.parametrize('visibility', ['operational_audit','automation_heartbeat'])
def test_normal_recall_excludes_audit_opt_in_recovers_it(dbclient,visibility):
    c,uid=dbclient
    note=post(c,raw='Decision: diagnostic audit-marker-77219 recorded the completed classroom status.',memory_visibility=visibility)
    assert c.get('/v1/search',params={'q':'audit-marker-77219'}).json()==[]
    got=c.get('/v1/search',params={'q':'audit-marker-77219','include_operational':True}).json()
    assert got[0]['capture_id']==note['id']

@pytest.mark.parametrize('query', ['nonexistent-marker-818126','839477319199','%','zqxvnonexistent','who invented the impossible quasar marmalade protocol'])
def test_irrelevant_queries_return_empty(dbclient,query):
    c,uid=dbclient;post(c)
    with Session(engine) as s: assert search_records(query,5,None,s,uid)==[]


def test_project_and_tenant_isolation(dbclient):
    c,uid=dbclient
    note=post(c,raw='Decision: project-marker-88921 belongs only to the classroom Spike Recorder project.',project_slug='spike-recorder')
    with Session(engine) as s:
        assert search_records('project-marker-88921',5,'spikerecorder',s,uid)[0]['capture_id']==note['id']
        assert search_records('project-marker-88921',5,'shipping',s,uid)==[]
        assert search_records('project-marker-88921',5,None,s,'different-user')==[]


def test_patch_reindexes_new_evidence_and_keeps_revision(dbclient):
    c,uid=dbclient;note=post(c)
    r=c.patch(f"/v1/captures/{note['id']}",json=dict(raw_content='Decision: corrected-marker-77232 classroom protocol uses a violet cable.',revision_reason='Corrected cable color',expected_revision=1))
    assert r.status_code==200,r.text
    assert r.json()['revision']==2 and r.json()['reindexed']
    with engine.connect() as s:
        assert s.execute(text('SELECT count(*) FROM memoryrevision WHERE target_id=:id AND user_id=:u'),dict(id=note['id'],u=uid)).scalar_one()==1
    assert c.get('/v1/search',params={'q':'corrected-marker-77232'}).json()[0]['capture_id']==note['id']


def test_stale_expected_revision_rejected(dbclient):
    c,_=dbclient;note=post(c)
    r=c.patch(f"/v1/captures/{note['id']}",json=dict(raw_content='Decision: stale edit must not overwrite current classroom calibration.',revision_reason='stale write',expected_revision=0))
    assert r.status_code==409

@pytest.mark.parametrize('raw,source,expected',[
 ('[cortex-feedback] event_id="x" action="done"','cortex-board','operational_audit'),
 ('coach:watson Keep the durable correction.','server-claude','policy'),
 ('Decision: classroom experiment uses a USB board.','server-claude','knowledge'),
 ('{"kind":"agent-dashboard-snapshot"}','agent:worker','operational_audit')])
def test_intake_routing(raw,source,expected): assert classify(raw,source)==expected


def test_fusion_one_result_per_capture_and_all_match_evidence():
    r=dict(capture_id=12,raw_content='classroom amplifier calibration',content='classroom amplifier calibration')
    got=fuse([('lexical',1,[r,r]),('card_lexical',1,[r]),('semantic',1,[r,r])],5,'classroom calibration')
    assert len(got)==1
    assert got[0]['matched_by']==['lexical','card_lexical','semantic']


def test_rejected_ack_attempts_preserved_and_retries_deduped():
    one=intake_metadata('[cortex-feedback-ack-rejected] event_id="e1" ack="first attempt"','cortex-board')
    again=intake_metadata('[cortex-feedback-ack-rejected] event_id="e1" ack="first attempt"','cortex-board')
    two=intake_metadata('[cortex-feedback-ack-rejected] event_id="e1" ack="second attempt"','cortex-board')
    assert one['ingestion_key']==again['ingestion_key']!=two['ingestion_key']


def test_daily_receipt_quoting_coach_is_not_a_standing_instruction():
    assert classify('[task-truth][coach:watson] morning-briefing live verification','server-claude')=='operational_audit'
    assert classify('{"kind":"agent-dashboard-snapshot","content":"coach:watson"}','agent:watson')=='operational_audit'


def test_mcp_http_forwards_caller_identity(monkeypatch):
    from services.memory_mcp import server
    from starlette.requests import Request
    request=Request({'type':'http','headers':[(b'x-api-key',b'caller-key')]})
    monkeypatch.setattr(server,'get_http_request',lambda:request)
    monkeypatch.setattr(server,'MEMORY_API_KEY','configured-local-key')
    assert server._api_headers()['X-API-Key']=='caller-key'


def test_mcp_anonymous_http_does_not_borrow_local_credentials(monkeypatch):
    from services.memory_mcp import server
    from starlette.requests import Request
    monkeypatch.setattr(server,'get_http_request',lambda:Request({'type':'http','headers':[]}))
    monkeypatch.setattr(server,'MEMORY_API_KEY','configured-local-key')
    with pytest.raises(ValueError):server._api_headers()


def test_coach_loader_failure_is_nonzero(monkeypatch,capsys):
    from integrations.openclaw.scripts import prefetch_coach
    monkeypatch.delenv('MEMORY_API_KEY',raising=False)
    assert prefetch_coach.main()==1
    assert 'Coach correction retrieval failed' in capsys.readouterr().err


def test_coach_loader_rejects_truncated_rules(monkeypatch):
    from integrations.openclaw.scripts import prefetch_coach
    import io
    monkeypatch.setenv('MEMORY_API_KEY','test-only')
    monkeypatch.setattr(prefetch_coach.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(b'{"status":"ok","truncated":true,"total":0,"rules":[]}'))
    with pytest.raises(RuntimeError):prefetch_coach.fetch_rules()


def test_identical_legacy_observations_do_not_crowd_recall():
    from packages.memory_retrieval.ranking import collapse_equivalent
    first=dict(capture_id=12,raw_content='classroom calibration',source='watson')
    copy=dict(first,capture_id=13)
    distinct_event=dict(first,capture_id=14,source_event_id='distinct-observation')
    results=collapse_equivalent([first,copy,distinct_event],5)
    assert [r['capture_id'] for r in results]==[12,14]
    assert results[0]['equivalent_capture_ids']==[13]


def test_backfill_reuses_complete_source_vector(dbclient):
    c,uid=dbclient;note=post(c)
    from packages.memory_retrieval.indexer import process_capture
    from scripts.retry_memory_index import seed_existing_vectors
    process_capture(note['id'])
    with engine.begin() as s:
        s.execute(text('DELETE FROM memoryfactcard WHERE source_capture_id=:id'),dict(id=note['id']))
    assert seed_existing_vectors(limit=1)==1
    with engine.connect() as s:
        row=s.execute(text('SELECT m.content,m.provenance_json,(m.embedding <=> ch.embedding) AS distance FROM memoryfactcard m JOIN chunk ch ON ch.capture_id=m.source_capture_id WHERE m.source_capture_id=:id'),dict(id=note['id'])).one()
    assert row.content==note['raw_content'] and 'vector_reused' in row.provenance_json
    assert abs(row.distance)<0.000001


def test_worker_lock_cannot_block_the_first_capture(dbclient):
    from scripts.retry_memory_index import WORKER_LOCK_ID
    with engine.connect() as worker,engine.connect() as capture:
        assert worker.execute(text('SELECT pg_try_advisory_lock(61015,:key)'),dict(key=WORKER_LOCK_ID)).scalar_one()
        try:
            acquired=capture.execute(text('SELECT pg_try_advisory_lock(61015,1)')).scalar_one()
            assert acquired
            capture.execute(text('SELECT pg_advisory_unlock(61015,1)'))
        finally:
            worker.execute(text('SELECT pg_advisory_unlock(61015,:key)'),dict(key=WORKER_LOCK_ID))


def test_reindex_preserves_revised_card_as_auditable_evidence(dbclient):
    c,uid=dbclient;note=post(c)
    from packages.memory_retrieval.indexer import process_capture
    process_capture(note['id'])
    with engine.connect() as s:
        old_id=s.execute(text('SELECT id FROM memoryfactcard WHERE source_capture_id=:id'),dict(id=note['id'])).scalar_one()
    r=c.patch(f"/v1/captures/{note['id']}",json=dict(raw_content='Decision: updated classroom cable requirement now uses the violet wire.',revision_reason='Correct old cable decision',expected_revision=1))
    assert r.status_code==200,r.text
    with engine.connect() as s:
        old=s.execute(text('SELECT memory_status,revision FROM memoryfactcard WHERE id=:id'),dict(id=old_id)).one()
        active=s.execute(text("SELECT count(*) FROM memoryfactcard WHERE source_capture_id=:id AND memory_status='active'"),dict(id=note['id'])).scalar_one()
    assert old.memory_status=='stale' and old.revision==2 and active==1
