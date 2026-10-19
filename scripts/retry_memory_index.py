#!/usr/bin/env python3
"""Bounded, restart-safe indexing retries and evidence-card backfill."""
import argparse
import json
import time
from sqlalchemy import text
from db.engine import engine
from packages.memory_retrieval.indexer import process_capture

# Capture IDs are positive; maintenance locks must use a separate key.
WORKER_LOCK_ID = -1

PENDING = """c.memory_status='active' AND (
 (c.index_state IN ('queued','error') AND (c.index_updated_at IS NULL OR c.index_updated_at<now()-interval '5 minutes'))
 OR (c.memory_visibility IN ('knowledge','policy') AND c.index_state='ready'
   AND NOT EXISTS(SELECT 1 FROM memoryfactcard m WHERE m.source_capture_id=c.id AND m.memory_status='active'
    AND (m.source_system='extractive-card-v1' OR length(c.raw_content)<1500))))"""

def seed_existing_vectors(limit=15000):
    """Reuse complete, unchanged short source vectors for missing evidence cards."""
    from packages.memory_retrieval.embeddings import get_model
    with engine.connect() as c:
        rows=c.execute(text("""SELECT c.id,ch.content FROM capture c JOIN chunk ch ON ch.capture_id=c.id
          WHERE c.memory_status='active' AND c.index_state='ready' AND c.memory_visibility IN ('knowledge','policy')
          AND ch.content=c.raw_content AND length(ch.content)<=1500
          AND NOT EXISTS(SELECT 1 FROM memoryfactcard m WHERE m.source_capture_id=c.id AND m.memory_status='active')
          AND (SELECT count(*) FROM chunk x WHERE x.capture_id=c.id)=1
          ORDER BY c.created_at DESC LIMIT :n"""),dict(n=limit)).all()
    if not rows: return 0
    tokenized=get_model().tokenizer([r.content for r in rows],add_special_tokens=False,truncation=False)['input_ids']
    ids=[row.id for row,tokens in zip(rows,tokenized) if len(tokens)<=360]
    added=0
    for start in range(0,len(ids),300):
        with engine.begin() as c:
            batch=ids[start:start+300]
            c.execute(text('SELECT pg_advisory_xact_lock(61015,id) FROM capture WHERE id=ANY(:ids) ORDER BY id'),dict(ids=batch))
            added+=c.execute(text("""INSERT INTO memoryfactcard(source_capture_id,content,project_slug,source_system,source_type,source_path,observed_at,historical_status,memory_visibility,memory_status,revision,provenance_json,embedding,created_at)
              SELECT c.id,ch.content,c.project_slug,'extractive-card-v1','extractive',c.source_path,coalesce(c.observed_at,c.created_at),
                CASE WHEN c.historical_until_verified THEN 'historical' ELSE 'current' END,c.memory_visibility,'active',1,
                json_build_object('capture_id',c.id,'capture_revision',c.revision,'section',0,'extraction','verbatim excerpt','vector_reused',true)::text,ch.embedding,now()
              FROM capture c JOIN chunk ch ON ch.capture_id=c.id
              WHERE c.id=ANY(:ids) AND c.memory_status='active' AND c.index_state='ready' AND ch.content=c.raw_content
              AND NOT EXISTS(SELECT 1 FROM memoryfactcard m WHERE m.source_capture_id=c.id AND m.memory_status='active')"""),dict(ids=batch)).rowcount
    return added


def run(seconds=45, limit=200):
    result=dict(indexed=0,failed=0)
    started=time.monotonic()
    with engine.connect() as lock:
        if not lock.execute(text('SELECT pg_try_advisory_lock(61015,:worker)'),dict(worker=WORKER_LOCK_ID)).scalar_one():
            return dict(status='already_running')
        try:
            result['indexed'] += seed_existing_vectors()
            with engine.connect() as c:
                ids=c.execute(text(f"SELECT c.id FROM capture c WHERE {PENDING} ORDER BY (c.index_state<>'ready') DESC,c.created_at DESC LIMIT :n"),dict(n=limit)).scalars().all()
            for cid in ids:
                if time.monotonic()-started>seconds: break
                try:
                    process_capture(cid);result['indexed']+=1
                except Exception:
                    result['failed']+=1
                if sum(result.values())%25==0: print(json.dumps(result),flush=True)
            with engine.connect() as c:
                result['remaining']=c.execute(text(f'SELECT count(*) FROM capture c WHERE {PENDING}')).scalar_one()
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(61015,:worker)'),dict(worker=WORKER_LOCK_ID))
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=int,default=45);p.add_argument('--limit',type=int,default=200)
    a=p.parse_args();r=run(a.seconds,a.limit);print(json.dumps(r));raise SystemExit(1 if r.get('failed') else 0)
