"""Read-only evaluation of fixed, source-grounded everyday questions.

The benchmark measures evidence retrieval and usable excerpts. It does NOT grade
an assistant's answers or turn a successful fetch into claimed rule compliance.
"""
import argparse
import hashlib
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session
from db.schema import Capture
from db.engine import engine
from packages.memory_retrieval.ranking import search_records

DEFAULT_CASES = Path(__file__).resolve().parents[1] / 'eval/real_questions.json'


def grade(case, rows):
    ids = [r['capture_id'] for r in rows]
    if case['kind'] == 'empty':
        return dict(hit=not rows, excerpt_supported=not rows, reciprocal_rank=0,
                    stale_exposed=False, passed=not rows)
    expected = set(case['expected_ids'])
    relevant = [r for r in rows if r['capture_id'] in expected]
    excerpt = ' '.join(r.get('content') or '' for r in relevant).lower()
    supported = bool(relevant) and all(any(term.lower() in excerpt for term in group)
                                         for group in case['evidence_terms'])
    stale = bool(set(ids) & set(case.get('forbidden_ids', [])))
    rank = next((i + 1 for i, cid in enumerate(ids) if cid in expected), None)
    return dict(hit=bool(rank), excerpt_supported=supported,
                reciprocal_rank=1 / rank if rank else 0, stale_exposed=stale,
                passed=bool(rank) and supported and not stale)


def evaluate(path=DEFAULT_CASES, user_id='greg', split=None):
    raw = Path(path).read_bytes()
    fixture = json.loads(raw)
    results = []
    source_drift=[]
    with Session(engine) as session:
        manifest=Path(path).with_name('real_question_sources.json')
        if manifest.exists():
            referenced={cid for c in fixture['cases'] for cid in c['expected_ids']}
            for cid,expected in json.loads(manifest.read_text()).items():
                if int(cid) not in referenced:continue
                source=session.get(Capture,int(cid))
                if (not source or source.user_id!=user_id or source.memory_status!='active'
                    or hashlib.sha256(source.raw_content.encode()).hexdigest()!=expected['sha256']):
                    source_drift.append(int(cid))
        for case in fixture['cases']:
            if split and case['split'] != split:
                continue
            start = time.monotonic()
            rows = search_records(case['q'], 5, case['project'], session, user_id)
            results.append(dict(id=case['id'], q=case['q'], split=case['split'],
                                kind=case['kind'], critical=case['critical'],
                                expected_ids=case['expected_ids'],
                                capture_ids=[r['capture_id'] for r in rows],
                                excerpts=[dict(capture_id=r['capture_id'], content=r.get('content'),
                                               matched_by=r.get('matched_by')) for r in rows],
                                seconds=round(time.monotonic()-start, 3), **grade(case, rows)))
    answerable = [r for r in results if r['kind'] == 'answer']
    negative = [r for r in results if r['kind'] == 'empty']
    def rate(rows, field):
        return round(sum(bool(r[field]) for r in rows) / len(rows), 4) if rows else None
    return dict(checked_at=datetime.now(timezone.utc).isoformat(),
                fixture_sha256=hashlib.sha256(raw).hexdigest(),
                source_drift=source_drift,
                scope='Evidence retrieval and excerpt coverage; agent behavior is not measured here.',
                total=len(results), passed=sum(r['passed'] for r in results),
                recall_at_5=rate(answerable, 'hit'), excerpt_coverage=rate(answerable, 'excerpt_supported'),
                abstention_accuracy=rate(negative, 'passed'),
                critical_failures=sum(r['critical'] and not r['passed'] for r in results),
                mean_reciprocal_rank=round(statistics.mean(r['reciprocal_rank'] for r in answerable),4) if answerable else None,
                latency_p50_seconds=statistics.median(r['seconds'] for r in results) if results else None,
                splits={s: dict(total=sum(r['split']==s for r in results),
                                passed=sum(r['split']==s and r['passed'] for r in results)) for s in ('development','holdout')},
                results=results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--split', choices=['development', 'holdout'])
    args = parser.parse_args()
    result = evaluate(split=args.split)
    Path(args.output).write_text(json.dumps(result, indent=2, default=str)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k != 'results'}))
