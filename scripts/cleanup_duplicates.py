#!/usr/bin/env python3
"""Archive a reviewed exact-event manifest; preserve IDs and transactionally undo it.

DATABASE_URL selects the target. Default is dry-run. Rollback refuses records
edited since cleanup. Archive tables retain original rows and vector chunks.
"""
import argparse
import hashlib
import json
import uuid
from pathlib import Path
from sqlalchemy import text
from db.engine import engine

BATCH = 'cortex-exact-events-20261015'
FIELDS = 'memory_status,superseded_by_capture_id,revision,updated_at,revision_reason,revision_actor,revision_source_ids_json,index_state'.split(',')

def run(manifest, apply=False, rollback=False):
    groups = [g for g in json.loads(Path(manifest).read_text())['groups'] if g['eligible']]
    mapping = [dict(id=i, canonical=g['canonical_id'], user=g['user_id']) for g in groups for i in g['duplicate_ids']]
    with engine.begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(61015,0)'))
        if rollback:
            conn.execute(text("LOCK TABLE capture,chunk IN SHARE ROW EXCLUSIVE MODE"))
            state = conn.execute(text('SELECT status FROM memory_maintenance.cleanup_batch WHERE batch=:b FOR UPDATE'),dict(b=BATCH)).scalar_one()
            if state != 'applied':
                raise RuntimeError('Batch is not applied')
            changed = conn.execute(text("""SELECT count(*) FROM memory_maintenance.capture_archive a JOIN capture c ON c.id=a.id
                WHERE a.batch=:b AND (c.memory_status<>'duplicate' OR c.revision<>(a.snapshot->>'revision')::int+1)"""),dict(b=BATCH)).scalar_one()
            if changed:
                raise RuntimeError('Archived records were revised after cleanup; manual reconciliation required')
            conn.execute(text("""INSERT INTO memoryrevision(target_type,target_id,user_id,action,before_json,after_json,reason,actor,created_at)
              SELECT 'capture',c.id,c.user_id,'cleanup_rollback',row_to_json(c)::text,a.snapshot::text,
              'Restore exact-event cleanup batch','memory-maintenance',now()
              FROM capture c JOIN memory_maintenance.capture_archive a ON c.id=a.id WHERE a.batch=:b"""),dict(b=BATCH))
            assignments=','.join(f'{f}=r.{f}' for f in FIELDS)
            conn.execute(text(f"""UPDATE capture c SET {assignments}
              FROM memory_maintenance.capture_archive a, LATERAL jsonb_populate_record(NULL::capture,a.snapshot) r
              WHERE c.id=a.id AND a.batch=:b"""),dict(b=BATCH))
            conn.execute(text("""INSERT INTO chunk SELECT r.* FROM memory_maintenance.chunk_archive a,
                LATERAL jsonb_populate_record(NULL::chunk,a.snapshot) r WHERE a.batch=:b"""),dict(b=BATCH))
            conn.execute(text("UPDATE memory_maintenance.cleanup_batch SET status='rolled_back' WHERE batch=:b"),dict(b=BATCH))
            return dict(mode='rolled_back', restored_captures=len(mapping))
        conn.execute(text('CREATE TEMP TABLE cleanup_plan(id integer PRIMARY KEY, canonical integer, "user" text) ON COMMIT DROP'))
        conn.execute(text('INSERT INTO cleanup_plan VALUES (:id,:canonical,:user)'), mapping)
        # Lock both tables so intake/revisions cannot alter the validated set mid-cleanup.
        conn.execute(text('LOCK TABLE capture,chunk IN SHARE ROW EXCLUSIVE MODE'))
        canon = dict(conn.execute(text('SELECT id,raw_content FROM capture WHERE id=ANY(:ids)'),dict(ids=[g['canonical_id'] for g in groups])).all())
        for g in groups:
            if hashlib.sha256(canon[g['canonical_id']].encode()).hexdigest()!=g['sha256']:
                raise RuntimeError('Canonical content changed')
        same_metadata=' AND '.join(f'c.{f} IS NOT DISTINCT FROM k.{f}' for f in ('source_system','source_path','source_type','observed_at','imported_at','content_hash','import_batch_id','historical_until_verified'))
        valid = conn.execute(text(f"""SELECT count(*) FROM cleanup_plan p JOIN capture c ON c.id=p.id JOIN capture k ON k.id=p.canonical
          WHERE c.user_id=p."user" AND k.user_id=p."user" AND c.source='cortex-board' AND k.source=c.source
          AND c.raw_content=k.raw_content AND c.memory_status='active' AND k.memory_status='active'
          AND c.superseded_by_capture_id IS NULL AND k.superseded_by_capture_id IS NULL AND {same_metadata}""")).scalar_one()
        if valid!=len(mapping):
            raise RuntimeError(f'Manifest changed: {valid}/{len(mapping)} eligible rows')
        references = conn.execute(text("""SELECT
          (SELECT count(*) FROM memoryfactcard m JOIN cleanup_plan p ON p.id=m.source_capture_id)+
          (SELECT count(*) FROM capture c JOIN cleanup_plan p ON p.id=c.superseded_by_capture_id)+
          (SELECT count(*) FROM task t JOIN cleanup_plan p ON p.id=t.blocker_capture_id)+
          (SELECT count(*) FROM chunk c JOIN cleanup_plan p ON p.id=c.capture_id WHERE c.task_id IS NOT NULL OR c.project_id IS NOT NULL OR c.event_id IS NOT NULL)""")).scalar_one()
        if references:
            raise RuntimeError(f'Unexpected dependent references: {references}')
        n_chunks=conn.execute(text('SELECT count(*) FROM chunk c JOIN cleanup_plan p ON p.id=c.capture_id')).scalar_one()
        if not apply:
            return dict(mode='validated_dry_run', duplicate_captures=valid, derived_chunks=n_chunks, groups=len(groups))
        conn.execute(text('CREATE SCHEMA IF NOT EXISTS memory_maintenance'))
        conn.execute(text('CREATE TABLE IF NOT EXISTS memory_maintenance.cleanup_batch(batch text PRIMARY KEY,status text,created_at timestamp DEFAULT now())'))
        conn.execute(text('CREATE TABLE IF NOT EXISTS memory_maintenance.capture_archive(batch text,id integer,snapshot jsonb,PRIMARY KEY(batch,id))'))
        conn.execute(text('CREATE TABLE IF NOT EXISTS memory_maintenance.chunk_archive(batch text,id integer,snapshot jsonb,PRIMARY KEY(batch,id))'))
        state=conn.execute(text('SELECT status FROM memory_maintenance.cleanup_batch WHERE batch=:b'),dict(b=BATCH)).scalar_one_or_none()
        if state=='applied':
            raise RuntimeError('Already applied')
        conn.execute(text("INSERT INTO memory_maintenance.cleanup_batch VALUES (:b,'applied',now()) ON CONFLICT(batch) DO UPDATE SET status='applied'"),dict(b=BATCH))
        for table,key in [('capture','id'),('chunk','capture_id')]:
            conn.execute(text(f"INSERT INTO memory_maintenance.{table}_archive SELECT :b,c.id,to_jsonb(c) FROM {table} c JOIN cleanup_plan p ON p.id=c.{key} ON CONFLICT(batch,id) DO NOTHING"),dict(b=BATCH))
        conn.execute(text("""UPDATE capture c SET memory_status='duplicate',superseded_by_capture_id=p.canonical,
          revision=c.revision+1,updated_at=now(),revision_reason='Exact repeated Cortex event; canonical capture retained',
          revision_actor='memory-maintenance',revision_source_ids_json=json_build_array('capture:'||p.canonical)::text,index_state='archived'
          FROM cleanup_plan p WHERE c.id=p.id"""))
        conn.execute(text("""INSERT INTO memoryrevision(target_type,target_id,user_id,action,before_json,after_json,reason,actor,source_ids_json,idempotency_key,created_at)
          SELECT 'capture',c.id,c.user_id,'archive_duplicate',a.snapshot::text,
          json_build_object('memory_status',c.memory_status,'superseded_by_capture_id',c.superseded_by_capture_id,'revision',c.revision)::text,
          c.revision_reason,c.revision_actor,c.revision_source_ids_json,:operation,now()
          FROM capture c JOIN memory_maintenance.capture_archive a ON c.id=a.id WHERE a.batch=:b"""),dict(b=BATCH,operation=BATCH+':'+str(uuid.uuid4())))
        conn.execute(text('DELETE FROM chunk c USING cleanup_plan p WHERE c.capture_id=p.id'))
        return dict(mode='applied', archived_captures=valid, removed_derived_chunks=n_chunks, groups=len(groups), batch=BATCH)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('manifest');p.add_argument('--apply',action='store_true');p.add_argument('--rollback',action='store_true')
    args=p.parse_args();print(json.dumps(run(args.manifest,args.apply,args.rollback)))
