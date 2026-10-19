#!/usr/bin/env python3
"""Read-only BYB retrieval regression checks; never creates canary captures."""
import argparse
import json
import os
import time
from pathlib import Path
from sqlmodel import Session
from db.engine import engine
from packages.memory_retrieval.ranking import search_records

def evaluate(path=None,user_id='greg'):
    path=path or Path(__file__).resolve().parents[1]/'eval/memory_queries.json'
    cases=json.loads(Path(path).read_text());results=[]
    with Session(engine) as s:
        for case in cases:
            start=time.monotonic()
            rows=search_records(case['q'],5,case.get('project'),s,user_id,include_operational=case.get('operational',False))
            ids=[r['capture_id'] for r in rows]
            okay=(not rows) if case.get('empty') else bool(rows)
            if 'expected_ids' in case:okay=okay and bool(set(ids)&set(case['expected_ids']))
            if 'contains' in case:okay=okay and any(any(t.lower() in r['raw_content'].lower() for t in case['contains']) for r in rows)
            if 'forbidden_ids' in case:okay=okay and not (set(ids)&set(case['forbidden_ids']))
            okay=okay and len(ids)==len(set(ids))
            results.append(dict(label=case['label'],passed=bool(okay),capture_ids=ids,seconds=round(time.monotonic()-start,3)))
    return dict(total=len(results),passed=sum(r['passed'] for r in results),results=results)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output');a=p.parse_args();r=evaluate()
    if a.output: Path(a.output).write_text(json.dumps(r,indent=2))
    print(json.dumps(r));raise SystemExit(0 if r['passed']==r['total'] else 1)
