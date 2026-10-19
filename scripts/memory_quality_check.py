#!/usr/bin/env python3
"""Daily read-only quality report; failures go to systemd, never to memory intake."""
import json
import os
from datetime import datetime,timezone
from pathlib import Path
from sqlmodel import Session
from services.memory_api.main import memory_quality_health
from scripts.evaluate_memory import evaluate
from db.engine import engine
from scripts.evaluate_real_questions import evaluate as evaluate_real
from packages.memory_retrieval.context import pilot_active, pilot_metrics


def run():
    user=os.environ.get('MEMORY_QUALITY_USER','greg')
    with Session(engine) as s:
        health=memory_quality_health(session=s,user_id=user)
        pilot=pilot_metrics(s,user)
    checks=evaluate(user_id=user)
    result=dict(checked_at=datetime.now(timezone.utc).isoformat(),health=health,retrieval=checks,
                passed=health['status']=='ok' and checks['passed']==checks['total'])
    result['pilot']=pilot
    if pilot_active():
        real=evaluate_real(user_id=user)
        result['everyday_questions']=real
        result['pilot_targets_met']=(not real['source_drift'] and real['recall_at_5']>=0.9 and real['critical_failures']==0
                                      and real['abstention_accuracy']==1.0)
        result['passed']=result['passed'] and result['pilot_targets_met']
    directory=Path(os.environ.get('MEMORY_QUALITY_DIR','/var/lib/byb-memory-health'))
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=directory/'latest.tmp';temp.write_text(json.dumps(result,indent=2));temp.chmod(0o600);temp.replace(directory/'latest.json')
    if 'everyday_questions' in result:
        # At most one report per day; private local diagnostics, never captures.
        history=directory/'pilot-history';history.mkdir(mode=0o700,exist_ok=True)
        day=history/(datetime.now(timezone.utc).date().isoformat()+'.json')
        report=dict(checked_at=result['checked_at'],pilot=pilot,pilot_targets_met=result['pilot_targets_met'],
                    everyday_questions={k:v for k,v in result['everyday_questions'].items() if k!='results'})
        temp=history/'report.tmp';temp.write_text(json.dumps(report,indent=2));temp.chmod(0o600);temp.replace(day)
    elif pilot.get('status')=='ended' and not (directory/'pilot-final.json').exists():
        history=directory/'pilot-history'
        days=sorted(history.glob('*.json')) if history.exists() else []
        final=dict(checked_at=result['checked_at'],pilot=pilot,observed_daily_reports=len(days),
                   last_daily_report=json.loads(days[-1].read_text()) if days else None,
                   interpretation='Retrieval results and client reports do not establish actual answer/action compliance. Review observed outputs before claiming success.')
        temp=directory/'pilot-final.tmp';temp.write_text(json.dumps(final,indent=2));temp.chmod(0o600);temp.replace(directory/'pilot-final.json')
    print(json.dumps(dict(passed=result['passed'],health=health,retrieval_passed=checks['passed'],retrieval_total=checks['total'])))
    return result['passed']

if __name__=='__main__':raise SystemExit(0 if run() else 1)
