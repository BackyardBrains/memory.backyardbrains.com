"""Conservative, reviewed archival of three legacy routing variants."""
import argparse
import hashlib
import json
import re
from pathlib import Path

from sqlmodel import Session
from db.engine import engine
from db.schema import Capture
from services.memory_api.main import CapturePatchBody,patch_capture

MANIFEST=Path(__file__).resolve().parents[1]/'data/legacy_review.json'


def review(apply=False,user_id='greg'):
    manifest=json.loads(MANIFEST.read_text())
    results=[]
    with Session(engine) as s:
        for item in manifest['event_reviews']:
            if item['disposition']!='archive_earlier_routing_variant':continue
            old_id,new_id=item['ids']
            old,new=s.get(Capture,old_id),s.get(Capture,new_id)
            if not old or not new or old.user_id!=user_id or new.user_id!=user_id:
                raise ValueError('Missing or unowned review target')
            if [hashlib.sha256(c.raw_content.encode()).hexdigest() for c in (old,new)]!=item['content_sha256']:
                raise ValueError('Reviewed source changed')
            a=dict(re.findall(r'(\w+)="([^"\n]*)"',old.raw_content))
            b=dict(re.findall(r'(\w+)="([^"\n]*)"',new.raw_content))
            keys=('event_id','action','actor','recorded_at','task_id','project')
            if any(not a.get(k) or a.get(k)!=b.get(k) for k in keys):
                raise ValueError('Stable event fields differ')
            if not (a.get('dashboard_agent')=='unknown' and b.get('dashboard_agent')=='watson'):
                raise ValueError('Not the reviewed routing enrichment')
            if new.memory_status!='active':raise ValueError('Canonical source is no longer active')
            if old.memory_status=='duplicate' and old.superseded_by_capture_id==new.id:
                results.append(dict(id=old.id,canonical_id=new.id,already_applied=True));continue
            if old.memory_status!='active':raise ValueError('Earlier source is no longer active')
            if apply:
                patch_capture(old.id,CapturePatchBody(memory_status='duplicate',superseded_by_capture_id=new.id,
                    expected_revision=old.revision,revision_reason=item['reason'],revision_actor='server-claude',
                    source_message_ids=[f'capture:{new.id}'],idempotency_key=f'phase2-route-review:{old.id}:revision:{old.revision}'),
                    session=s,user_id=user_id)
            results.append(dict(id=old.id,canonical_id=new.id,applied=apply))
    return dict(routing_variants=results,retained_distinct_event_groups=8,retained_unidentified_groups=9)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--output',required=True);a=p.parse_args()
    result=review(a.apply);Path(a.output).write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
