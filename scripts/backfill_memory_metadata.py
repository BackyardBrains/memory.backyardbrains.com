#!/usr/bin/env python3
"""Route existing captures and assign retry identities without merging observations."""
import json
from collections import Counter
from sqlalchemy import text
from db.engine import engine
from packages.memory_retrieval.intake import intake_metadata

def run():
    counts=Counter(); conflicts=[]
    with engine.begin() as conn:
        conn.execute(text('LOCK TABLE capture IN SHARE ROW EXCLUSIVE MODE'))
        conn.execute(text('CREATE SCHEMA IF NOT EXISTS memory_maintenance'))
        conn.execute(text('CREATE TABLE IF NOT EXISTS memory_maintenance.metadata_archive(id integer PRIMARY KEY,snapshot jsonb,created_at timestamp DEFAULT now())'))
        conn.execute(text("INSERT INTO memory_maintenance.metadata_archive(id,snapshot) SELECT id,to_jsonb(c) FROM capture c WHERE memory_status='active' ON CONFLICT(id) DO NOTHING"))
        rows=conn.execute(text("SELECT * FROM capture WHERE memory_status='active' ORDER BY created_at,id")).mappings().all()
        used={(r.user_id,r.ingestion_key):r.id for r in conn.execute(text('SELECT id,user_id,ingestion_key FROM capture WHERE ingestion_key IS NOT NULL')).all()}
        updates=[]
        for r in rows:
            m=intake_metadata(r['raw_content'],r['source'],source_path=r['source_path'],observed_at=r['observed_at'],source_type=r['source_type'])
            known_projects={111022:'spikerecorder',111027:'spikerecorder',110964:'bybsite',110935:'bybsite',110806:'bybsite'}
            if r['id'] in known_projects and m['memory_visibility']=='policy':
                m.update(project_slug=known_projects[r['id']],policy_scope='project')
            identity=(r['user_id'],m['ingestion_key'])
            prior=used.get(identity)
            if prior is not None and prior!=r['id']:
                conflicts.append(dict(capture_id=r['id'],existing_capture_id=prior,has_event_id=bool(m['source_event_id'])))
                m['ingestion_key']=None
            else: used[identity]=r['id']
            # Do not replace explicit identities/visibility on captures written by the new API.
            if r['ingestion_key']:
                continue
            m['content_hash']=r['content_hash'] or m['content_hash']
            m['id']=r['id'];updates.append(m);counts[m['memory_visibility']]+=1
        if updates:
            columns=','.join(f'{k}=:{k}' for k in updates[0] if k!='id')
            conn.execute(text(f'UPDATE capture SET {columns} WHERE id=:id'), updates)
        conn.execute(text("""UPDATE capture c SET memory_visibility=m.memory_visibility FROM memoryfactcard m
          WHERE m.source_capture_id=c.id AND m.memory_status='active' AND c.memory_status='active' AND c.memory_visibility<>'policy'
          AND m.memory_visibility IN ('operational_audit','automation_heartbeat')"""))
    return dict(updated=sum(counts.values()),by_visibility=dict(counts),identity_conflicts_held=conflicts)

if __name__=='__main__': print(json.dumps(run()))
