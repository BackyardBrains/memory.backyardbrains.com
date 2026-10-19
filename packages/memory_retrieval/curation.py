"""Reviewed, source-linked decisions; raw captures remain the evidence layer."""
import hashlib
import json
from collections import Counter, defaultdict

from sqlmodel import select
from db.schema import Capture, MemoryFactCard
from .intake import project_slug as normalize_project

CURATED_SOURCE = 'reviewed-decision-v1'


def current_cards(session, user_id):
    pairs = session.exec(select(MemoryFactCard, Capture).join(
        Capture, Capture.id == MemoryFactCard.source_capture_id).where(
        Capture.user_id == user_id, Capture.memory_status == 'active',
        MemoryFactCard.memory_status == 'active',
        MemoryFactCard.source_system == CURATED_SOURCE)).all()
    if not pairs:
        return {}
    expected=Counter(source.id for _,source in pairs)
    references = set()
    parsed = []
    for card, source in pairs:
        try:
            provenance = json.loads(card.provenance_json or '{}')
            refs = provenance['sources']
            if not isinstance(refs,list) or any(not isinstance(r,dict) or
                not isinstance(r.get('capture_id'),int) or not isinstance(r.get('revision'),int) or
                not isinstance(r.get('sha256'),str) or len(r['sha256'])!=64 for r in refs):
                continue
            if not isinstance(provenance.get('key'),str):
                continue
            if not refs or not any(r['capture_id'] == source.id for r in refs):
                continue
            references.update(r['capture_id'] for r in refs)
            parsed.append((card, source, provenance))
        except (ValueError, KeyError, TypeError):
            continue
    owned = {c.id: c for c in session.exec(select(Capture).where(
        Capture.user_id == user_id, Capture.id.in_(references))).all()}
    hashes = {cid: hashlib.sha256(c.raw_content.encode()).hexdigest() for cid, c in owned.items()}
    valid = defaultdict(list)
    invalid_sources = set()
    for card, source, provenance in parsed:
        if all(r['capture_id'] in owned and owned[r['capture_id']].memory_status == 'active'
               and owned[r['capture_id']].revision == r['revision']
               and hashes[r['capture_id']] == r['sha256'] for r in provenance['sources']):
            valid[source.id].append((card, provenance))
        else:
            invalid_sources.add(source.id)
    # Never present an incomplete projection as the complete policy for a source.
    return {cid:rows for cid,rows in valid.items() if cid not in invalid_sources and len(rows)==expected[cid]}


def active_rules(session, user_id, project=None):
    project = normalize_project(project)
    stmt = select(Capture).where(Capture.user_id == user_id,
                                 Capture.memory_status == 'active',
                                 Capture.memory_visibility == 'policy')
    if project:
        stmt = stmt.where((Capture.policy_scope == 'global') | (Capture.project_slug == project))
    sources = session.exec(stmt.order_by(Capture.created_at.desc(), Capture.id.desc())).all()
    curated = current_cards(session, user_id)
    rules = []
    for source in sources:
        cards = curated.get(source.id, [])
        # A reviewed complete policy projection can separate a still-valid rule
        # from superseded instructions and dated receipts in the same raw note.
        projected = [(c,p) for c,p in cards if p.get('complete_policy_projection')]
        if projected:
            for card, provenance in projected:
                if card.memory_visibility != 'policy':
                    continue
                rules.append(dict(capture_id=source.id, card_id=card.id,
                                  rule_id=provenance['key'], content=card.content,
                                  scope=source.policy_scope, project_slug=source.project_slug,
                                  created_at=source.created_at, revision=source.revision,
                                  source_ids=[r['capture_id'] for r in provenance['sources']],
                                  evidence_kind='reviewed_decision'))
        else:
            rules.append(dict(capture_id=source.id, content=source.raw_content,
                              scope=source.policy_scope, project_slug=source.project_slug,
                              created_at=source.created_at, revision=source.revision,
                              evidence_kind='source_instruction'))
    return dict(status='ok', memory_system='BYB Shared Memory', total=len(rules),
                source_count=len(sources), truncated=False, rules=rules)
