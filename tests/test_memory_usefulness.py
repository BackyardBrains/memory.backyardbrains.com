"""Usefulness contracts on a disposable restore; no real agent actions or messages."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime,timedelta,timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session,select
from db.engine import engine
from db.schema import Capture,MemoryFactCard,MemoryUseEvent
from services.memory_api import main
from packages.memory_retrieval.curation import CURATED_SOURCE,active_rules,current_cards
from packages.memory_retrieval.context import pilot_metrics

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def client(monkeypatch,tmp_path):
    if engine.url.database!='byb_memory_repair_test_20261015':pytest.skip('Disposable restore required')
    uid='usefulness-test'
    main.app.dependency_overrides[main.get_user_from_api_key]=lambda:uid
    monkeypatch.setattr(main,'process_capture',lambda _:None)
    p=tmp_path/'pilot.json';now=datetime.now(timezone.utc)
    p.write_text(json.dumps(dict(starts_at=(now-timedelta(hours=1)).isoformat(),ends_at=(now+timedelta(days=14)).isoformat())))
    monkeypatch.setenv('MEMORY_PILOT_CONFIG',str(p))
    def clear():
        with engine.begin() as s:
            s.execute(text('DELETE FROM memoryuseevent WHERE user_id=:u AND parent_id IS NOT NULL'),dict(u=uid))
            s.execute(text('DELETE FROM memoryuseevent WHERE user_id=:u'),dict(u=uid))
            s.execute(text('DELETE FROM memoryfactcard WHERE source_capture_id IN (SELECT id FROM capture WHERE user_id=:u)'),dict(u=uid))
            s.execute(text('DELETE FROM chunk WHERE user_id=:u'),dict(u=uid))
            s.execute(text('DELETE FROM memoryrevision WHERE user_id=:u'),dict(u=uid))
            s.execute(text('UPDATE capture SET superseded_by_capture_id=NULL WHERE user_id=:u'),dict(u=uid))
            s.execute(text('DELETE FROM capture WHERE user_id=:u'),dict(u=uid))
    clear();yield TestClient(main.app),uid;clear();main.app.dependency_overrides.clear()


def capture(c,content,**kw):
    r=c.post('/v1/captures',json=dict(raw_content='Decision: '+content,source='usefulness-test',**kw))
    assert r.status_code==200,r.text
    return r.json()


def card(source,content,refs=None,key='test-rule'):
    refs=refs or [source]
    provenance=dict(key=key,complete_policy_projection=True,sources=[dict(capture_id=r['id'],revision=r['revision'],sha256=hashlib.sha256(r['raw_content'].encode()).hexdigest()) for r in refs])
    with Session(engine) as s:
        row=MemoryFactCard(source_capture_id=source['id'],content=content,embedding=[0.0]*384,
            source_system=CURATED_SOURCE,source_path='curation:'+key,memory_visibility='policy',
            project_slug=source.get('project_slug'),provenance_json=json.dumps(provenance))
        s.add(row);s.commit();s.refresh(row);return row.id


def test_context_has_complete_scoped_rules_and_no_false_use(client):
    c,u=client
    ids=[capture(c,f'coach:watson Durable instruction {i}: calibrate the classroom amplifier.')['id'] for i in range(8)]
    own=capture(c,'coach:watson Project recording rule: keep the calibration setting.',project_slug='spikerecorder')
    other=capture(c,'coach:watson Shipping rule: keep packaging choices.',project_slug='byb-shipping')
    r=c.get('/v1/context',params={'project_slug':'spike-recorder','client':'unit-test'}).json()
    assert r['rules']['truncated'] is False
    assert {x['capture_id'] for x in r['rules']['rules']}==set(ids+[own['id']])
    assert r['telemetry']=='context_loaded_only' and not r['feedback_recorded']
    assert r['evidence']==[] and r['evidence_status']=='not_requested'
    assert other['id'] not in {x['capture_id'] for x in r['rules']['rules']}
    with Session(engine) as s:
        metrics=pilot_metrics(s,u)
        assert metrics['counts']['context_loaded']==1 and metrics['counts']['used']==0


def test_feedback_is_retry_safe_and_does_not_create_memory(client):
    c,u=client;note=capture(c,'coach:watson Standing decision: retain the classroom calibration.')
    context=c.get('/v1/context').json()
    body=dict(context_id=context['context_id'],outcome='used',capture_ids=[note['id']])
    a=c.post('/v1/feedback',json=body);b=c.post('/v1/feedback',json=body)
    assert a.status_code==b.status_code==200
    assert a.json()['feedback_id']==b.json()['feedback_id'] and a.json()['capture_created'] is False
    assert a.json()['assessment']=='reported_by_client'
    with Session(engine) as s:
        assert len(s.exec(select(Capture).where(Capture.user_id==u)).all())==1
        assert pilot_metrics(s,u)['counts']['used']==1


@pytest.mark.parametrize('body',[dict(outcome='used',capture_ids=[]),dict(outcome='made_up_success',capture_ids=[]),dict(outcome='missed',capture_ids=[2147483600])])
def test_invalid_feedback_rejected(client,body):
    c,_=client;ctx=c.get('/v1/context').json()
    assert c.post('/v1/feedback',json=dict(context_id=ctx['context_id'],**body)).status_code==422


def test_feedback_cannot_claim_an_unseen_source_was_used(client):
    c,_=client;ctx=c.get('/v1/context').json()
    new=capture(c,'Decision: unrelated calibration evidence was not supplied to the earlier context.')
    assert c.post('/v1/feedback',json=dict(context_id=ctx['context_id'],outcome='used',capture_ids=[new['id']])).status_code==422


def test_context_tenant_isolation(client):
    c,_=client;ctx=c.get('/v1/context').json()
    main.app.dependency_overrides[main.get_user_from_api_key]=lambda:'other-tenant'
    assert c.post('/v1/feedback',json=dict(context_id=ctx['context_id'],outcome='abstained')).status_code==404


def test_pilot_expiry_stops_new_telemetry(client,monkeypatch,tmp_path):
    c,u=client;p=tmp_path/'ended.json';now=datetime.now(timezone.utc)
    p.write_text(json.dumps(dict(starts_at=(now-timedelta(days=15)).isoformat(),ends_at=(now-timedelta(days=1)).isoformat())))
    monkeypatch.setenv('MEMORY_PILOT_CONFIG',str(p))
    ctx=c.get('/v1/context').json()
    assert ctx['context_id'] is None and ctx['telemetry']=='disabled'
    with Session(engine) as s:assert not s.exec(select(MemoryUseEvent).where(MemoryUseEvent.user_id==u)).all()


def test_partial_policy_curation_preserves_independent_rule(client):
    c,u=client
    old=capture(c,'coach:watson Old orchestrator allocation. Independent instruction: preserve the amplifier calibration.')
    replacement=capture(c,'coach:watson New orchestrator allocation replaces the old allocation only.')
    card(old,'Preserve the amplifier calibration.',refs=[old,replacement])
    with Session(engine) as s:
        r=active_rules(s,u)
        assert r['total']==2
        assert 'Old orchestrator allocation' not in ' '.join(x['content'] for x in r['rules'])
        assert 'Preserve the amplifier calibration.' in ' '.join(x['content'] for x in r['rules'])
    assert c.get(f"/v1/captures/{old['id']}").json()['raw_content']==old['raw_content']


def test_changed_reference_invalidates_entire_projection(client):
    c,u=client
    a=capture(c,'coach:watson Original standing classroom calibration rule.')
    b=capture(c,'coach:watson Current calibration correction from the teacher.')
    card(a,'Follow the current calibration.',refs=[a,b],key='dependent')
    card(a,'Preserve the cable.',key='independent')
    with Session(engine) as s:
        assert len(current_cards(s,u)[a['id']])==2
        row=s.get(Capture,b['id']);row.revision+=1;s.add(row);s.commit()
        assert a['id'] not in current_cards(s,u)
        rules=active_rules(s,u)
        assert next(r for r in rules['rules'] if r['capture_id']==a['id'])['content']==a['raw_content']


def test_forged_provenance_cannot_project_policy(client):
    c,u=client;a=capture(c,'coach:watson Original classroom calibration is required.')
    cid=card(a,'Malicious unsupported replacement.')
    with Session(engine) as s:
        row=s.get(MemoryFactCard,cid);p=json.loads(row.provenance_json);p['sources'][0]['sha256']='wrong';row.provenance_json=json.dumps(p);s.add(row);s.commit()
        assert a['id'] not in current_cards(s,u)
        assert active_rules(s,u)['rules'][0]['content']==a['raw_content']


def test_curation_rollback_is_reversible_and_audited(client,tmp_path,monkeypatch):
    from scripts import curate_decisions
    c,u=client;a=capture(c,'coach:watson Durable rule: preserve the amplifier calibration.')
    manifest=tmp_path/'cards.json';receipt=tmp_path/'receipt.json'
    manifest.write_text(json.dumps(dict(batch='unit-test',review_basis='Explicit source',cards=[dict(key='rollback-test',source_capture_id=a['id'],content='Preserve the amplifier calibration.',memory_visibility='policy',complete_policy_projection=True,sources=[dict(capture_id=a['id'],revision=a['revision'],sha256=hashlib.sha256(a['raw_content'].encode()).hexdigest())])])))
    result=curate_decisions.apply(manifest,u,commit=True);receipt.write_text(json.dumps(result))
    assert curate_decisions.apply(manifest,u,commit=True)['to_create']==0
    curate_decisions.rollback(receipt,u)
    with Session(engine) as s:
        assert not current_cards(s,u)
        assert s.get(Capture,a['id']).raw_content==a['raw_content']
        assert s.get(MemoryFactCard,result['card_ids'][0]).memory_status=='retracted'


def test_reviewed_routing_archive_can_be_undone_and_reapplied():
    if engine.url.database!='byb_memory_repair_test_20261015':pytest.skip('Disposable restore required')
    from scripts.review_legacy_events import review
    pairs=[(12610,12655),(12609,12654),(12608,12653)]
    review(apply=True)
    with Session(engine) as s:
        before={old:s.get(Capture,old).raw_content for old,_ in pairs}
        for old,_ in pairs:
            row=s.get(Capture,old)
            main.patch_capture(old,main.CapturePatchBody(memory_status='active',superseded_by_capture_id=None,
                expected_revision=row.revision,revision_reason='Exercise reversible routing archival in isolated restore'),session=s,user_id='greg')
            assert s.get(Capture,old).memory_status=='active'
    review(apply=True)
    with Session(engine) as s:
        for old,new in pairs:
            row=s.get(Capture,old)
            assert row.memory_status=='duplicate' and row.superseded_by_capture_id==new
            assert row.raw_content==before[old]


def load_client_module(name):
    path=ROOT/'integrations/openclaw/scripts'
    spec=importlib.util.spec_from_file_location(name,path/(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    sys.path.insert(0,str(path))
    try:spec.loader.exec_module(module)
    finally:sys.path.pop(0)
    return module


@pytest.mark.parametrize('data',[{},dict(status='ok',rules=[],total=1,truncated=False),dict(status='ok',rules=[],total=0,truncated=True)])
def test_client_rejects_incomplete_rules(data):
    module=load_client_module('memory_context_client')
    with pytest.raises(RuntimeError):module.validate_rules(data)


def test_session_hook_failure_cannot_claim_rules_loaded(monkeypatch):
    module=load_client_module('claude_memory_start')
    monkeypatch.delenv('API_SECRET',raising=False)
    def fail(**kwargs):raise TimeoutError('private URL or credential must not be printed')
    monkeypatch.setattr(module,'fetch_context',fail)
    result=module.session_context()['hookSpecificOutput']['additionalContext']
    assert 'NOT loaded' in result and 'loaded successfully' not in result
    assert 'private URL' not in result


@pytest.mark.parametrize('runner',['run_routine.sh','run_routine_codex.sh'])
@pytest.mark.parametrize('fetch_ok',[True,False])
def test_routine_injects_complete_context_or_stops_before_model(tmp_path,runner,fetch_ok):
    repo=tmp_path/'repo';(repo/'scripts/telemetry').mkdir(parents=True);(repo/'routines').mkdir();(repo/'logs').mkdir();(repo/'bin').mkdir()
    (repo/'.env').write_text('MEMORY_API_KEY=fake-unit-test\n')
    (repo/'routines/check.md').write_text('A routine fixture, no actions authorized.')
    (repo/'scripts/prefetch_coach.py').write_text('print(\'COMPLETE-RULES-FIXTURE\')' if fetch_ok else 'raise SystemExit(1)')
    for name in ['log_routine_run.py','log_routine_run_codex.py']:(repo/'scripts/telemetry'/name).write_text('pass')
    for command in ['codex','claude']:
        p=repo/'bin'/command;p.write_text('#!/bin/bash\ncat > '+str(repo/'received.txt')+'\n');p.chmod(0o755)
    source=(ROOT/'integrations/openclaw/scripts'/runner).read_text()
    source=source.replace('REPO=/var/www/openclaw.backyardbrains.com','REPO='+str(repo))
    source=source.replace('PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin"','PATH="'+str(repo/'bin')+':/usr/bin:/bin"')
    p=repo/'run.sh';p.write_text(source)
    result=subprocess.run(['bash',str(p),'check','fixture-model'],capture_output=True,text=True)
    if fetch_ok:
        assert result.returncode==0,result.stderr
        assert 'COMPLETE-RULES-FIXTURE' in (repo/'received.txt').read_text()
    else:
        assert result.returncode!=0 and not (repo/'received.txt').exists()
