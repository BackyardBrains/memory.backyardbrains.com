"""Apply reviewed projections without rewriting source captures; dry-run by default."""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, select
from db.engine import engine
from db.schema import Capture, MemoryFactCard, MemoryRevision
from packages.memory_retrieval.curation import CURATED_SOURCE
from packages.memory_retrieval.embeddings import compute_embeddings, get_model

DEFAULT = Path(__file__).resolve().parents[1] / 'data/reviewed_decisions.json'


def apply(manifest=DEFAULT, user_id='greg', commit=False):
    data = json.loads(Path(manifest).read_text())
    cards = data['cards']
    keys = [c['key'] for c in cards]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate reviewed decision keys')
    with Session(engine) as s:
        s.execute(text('SELECT pg_advisory_xact_lock(61015,-2)'))
        # Protect source snapshots from concurrent corrections until this batch commits.
        ids = sorted({r['capture_id'] for c in cards for r in c['sources']})
        for cid in ids:
            s.execute(text('SELECT pg_advisory_xact_lock(61015,:id)'), {'id':cid})
        owned = {c.id:c for c in s.exec(select(Capture).where(Capture.user_id==user_id, Capture.id.in_(ids))).all()}
        for c in cards:
            if c['source_capture_id'] not in {r['capture_id'] for r in c['sources']}:
                raise ValueError('Source anchor missing from provenance')
            for ref in c['sources']:
                source = owned.get(ref['capture_id'])
                if (not source or source.memory_status != 'active' or source.revision != ref['revision']
                    or hashlib.sha256(source.raw_content.encode()).hexdigest() != ref['sha256']):
                    raise ValueError(f"Source changed or unavailable: {ref['capture_id']}")
            if len(get_model().tokenizer.encode(c['content'], add_special_tokens=False)) > 360:
                raise ValueError('Reviewed decision exceeds embedding token budget')
        existing = {c.source_path:c for c in s.exec(select(MemoryFactCard).join(
            Capture, Capture.id==MemoryFactCard.source_capture_id).where(
            Capture.user_id==user_id, MemoryFactCard.source_system==CURATED_SOURCE)).all()}
        pending=[]
        for c in cards:
            key='curation:'+c['key']
            if key in existing:
                old=existing[key]
                if old.content!=c['content'] or old.memory_status!='active':
                    raise ValueError('Existing reviewed decision differs; requires an explicit revision')
            else:
                pending.append(c)
        result=dict(batch=data['batch'], source_count=len(owned), decisions=len(cards),
                    to_create=len(pending), applied=commit, card_ids=[])
        if not commit:
            return result
        vectors=compute_embeddings([c['content'] for c in pending])
        for c,vector in zip(pending,vectors):
            source=owned[c['source_capture_id']]
            provenance={k:c[k] for k in ('key','sources','complete_policy_projection')}
            provenance.update(batch=data['batch'], derivation='reviewed source-backed decision',
                              reviewed_at=datetime.utcnow().isoformat())
            card=MemoryFactCard(source_capture_id=source.id,content=c['content'],embedding=vector,
                source_system=CURATED_SOURCE,source_type='reviewed_decision',source_path='curation:'+c['key'],
                project_slug=source.project_slug,memory_visibility=c['memory_visibility'],
                historical_status='current',observed_at=source.observed_at or source.created_at,
                revision_actor='server-claude',revision_reason=data['review_basis'],
                revision_source_ids_json=json.dumps([f"capture:{r['capture_id']}" for r in c['sources']]),
                provenance_json=json.dumps(provenance))
            s.add(card);s.flush()
            s.add(MemoryRevision(target_type='memory_fact_card',target_id=card.id,user_id=user_id,
                                action='create_reviewed_decision',after_json=json.dumps(dict(content=card.content,provenance=provenance)),
                                reason=data['review_basis'],actor='server-claude',source_ids_json=card.revision_source_ids_json,
                                idempotency_key=data['batch']+':'+c['key']))
            result['card_ids'].append(card.id)
        s.commit()
        return result


def rollback(receipt, user_id='greg'):
    data=json.loads(Path(receipt).read_text())
    with Session(engine) as s:
        s.execute(text('SELECT pg_advisory_xact_lock(61015,-2)'))
        rows=s.exec(select(MemoryFactCard).join(Capture,Capture.id==MemoryFactCard.source_capture_id).where(
            Capture.user_id==user_id,MemoryFactCard.id.in_(data['card_ids']))).all()
        if len(rows)!=len(data['card_ids']):raise ValueError('Rollback receipt does not match owned cards')
        for c in rows:
            if c.source_system!=CURATED_SOURCE or c.revision!=1 or c.memory_status!='active':
                raise ValueError('Card revised since curation; do not overwrite it')
            c.memory_status='retracted';c.revision+=1;c.updated_at=datetime.utcnow()
            s.add(c)
            s.add(MemoryRevision(target_type='memory_fact_card',target_id=c.id,user_id=user_id,
                action='rollback_reviewed_decision',reason='Reversible rollout rollback; original sources unchanged',
                actor='server-claude',before_json='{"memory_status":"active","revision":1}',
                after_json='{"memory_status":"retracted","revision":2}'))
        s.commit()
    return dict(retracted_card_ids=data['card_ids'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--output',required=True)
    p.add_argument('--rollback');a=p.parse_args()
    result=rollback(a.rollback) if a.rollback else apply(commit=a.apply)
    Path(a.output).write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
