"""Scoped candidate retrieval with reciprocal-rank fusion and relevance gates."""
import re
from collections import defaultdict
from sqlalchemy import text
from .embeddings import compute_embedding
from .intake import project_slug as normalize_project
from .curation import current_cards

STOP = set("a an the what which who when where why how is are was were be been do does did can could should would will i we our my your you it its this that these those to of for from in on at by with and or as now about me tell please have has had our us system".split())
MIN_SIMILARITY = 0.65

def terms(query):
    return list(dict.fromkeys(t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) > 1 and t.lower() not in STOP))

def fuse(groups, limit, query):
    fused = {}
    scores = defaultdict(float)
    for name, weight, rows in groups:
        seen = set()
        for rank, row in enumerate(rows):
            cid = row['capture_id']
            if cid in seen:
                continue
            seen.add(cid)
            scores[cid] += weight / (60 + rank + 1)
            is_curated = name.startswith('curated_')
            if cid not in fused or is_curated or (name == 'card_lexical' and fused[cid].get('match_type') != 'exact' and not fused[cid].get('reviewed_decision')):
                previous_matches = fused.get(cid, {}).get('matched_by', [])
                fused[cid] = dict(row, match_type=name, matched_by=previous_matches.copy())
            fused[cid].setdefault('matched_by', [])
            if name not in fused[cid]['matched_by']:
                fused[cid]['matched_by'].append(name)
            if row.get('similarity') is not None:
                fused[cid]['similarity'] = max(row['similarity'], fused[cid].get('similarity', 0))
    qt = terms(query)
    identifiers = re.findall(r'\b(?:PR\s*#?\s*\d+|T[- ]?\d+|\d{6,})\b', query, re.I)
    normalized_ids = [re.sub(r'[^a-z0-9]', '', value.lower()) for value in identifiers]
    for cid, row in fused.items():
        raw = row['raw_content']
        lexical = set(terms(raw))
        coverage = sum(term in lexical for term in qt) / max(1, len(qt))
        score = scores[cid] * (0.7 + 0.6 * coverage)
        if any(m in row['matched_by'] for m in ('exact', 'alias')):
            score += 0.08
        if normalized_ids:
            normalized_body = re.sub(r'[^a-z0-9]', '', raw.lower())
            hits = sum(value in normalized_body for value in normalized_ids)
            if not hits and 'alias' not in row['matched_by']:
                score *= 0.25
            else:
                score += 0.02 * hits
        if row.get('historical_until_verified') or str(row.get('source','')).startswith('historical-import'):
            score *= 0.4
        email_demoted = (row.get('source') == 'claude-desktop-1' or raw.startswith('[email]')) and set(row['matched_by']) <= {'semantic','card_semantic'}
        if email_demoted:
            score *= 0.65
        if row.get('memory_visibility') == 'policy':
            score *= 1.08
        content = row.get('content') or raw
        if len(content) > 1600:
            position = content.lower().find(query.lower())
            start = max(0, position - 200) if position >= 0 else 0
            row['content'] = content[start:start + 1600]
            row['source_excerpt'] = True
        row['score'] = round(score, 6)
        row['rank_reason'] = 'reciprocal-rank fusion: ' + ', '.join(row['matched_by'])
        if email_demoted:
            row['rank_reason'] += '; demoted x0.65 (email index capture)'
        row['memory_system'] = 'BYB Shared Memory'
        row['id'] = cid
    return sorted(fused.values(), key=lambda r: (r['score'], str(r.get('created_at') or '')), reverse=True)[:limit]

def collapse_equivalent(rows, limit):
    """One recall hit per identical source observation, retaining all capture IDs."""
    unique = {}
    for row in rows:
        key = tuple(row.get(k) for k in ('source','source_path','project_slug','source_event_id','event_type','observed_at','raw_content'))
        if key in unique:
            unique[key].setdefault('equivalent_capture_ids', []).append(row['capture_id'])
        else:
            unique[key] = row
    return list(unique.values())[:limit]


def search_records(q, limit, project, session, user_id, include_semantic=True, include_operational=False):
    project = normalize_project(project)
    params = dict(q=q.strip(), user_id=user_id, project=project, n=max(40, min(limit*8, 200)), limit=limit)
    filters = "c.user_id=:user_id AND coalesce(c.memory_status,'active')='active'"
    if project:
        filters += " AND (c.project_slug=:project OR EXISTS (SELECT 1 FROM chunk pc JOIN project p ON pc.project_id=p.id WHERE pc.capture_id=c.id AND p.slug=:project) OR EXISTS (SELECT 1 FROM memoryfactcard pm WHERE pm.source_capture_id=c.id AND pm.project_slug=:project AND pm.memory_status='active'))"
    if not include_operational:
        filters += " AND c.memory_visibility IN ('knowledge','policy') AND NOT EXISTS (SELECT 1 FROM memoryfactcard excluded WHERE excluded.source_capture_id=c.id AND excluded.memory_visibility IN ('automation_heartbeat','operational_audit') AND excluded.memory_status='active')"
    cols = "c.id AS capture_id,c.raw_content,c.created_at,c.source,c.source_system,c.source_path,c.source_type,c.observed_at,c.historical_until_verified,c.memory_status,c.revision,c.superseded_by_capture_id,c.memory_visibility,c.project_slug,c.policy_scope,c.source_event_id,c.event_type"
    curated = current_cards(session, user_id)
    params['curated_ids'] = [card.id for values in curated.values() for card,_ in values] or [-1]
    params['curated_sources'] = list(curated) or [-1]
    current_card_filter = "(m.source_system IS DISTINCT FROM 'reviewed-decision-v1' OR m.id=ANY(:curated_ids)) AND (m.source_capture_id<>ALL(:curated_sources) OR m.id=ANY(:curated_ids))"
    reviewed_candidates = []
    def finish(groups):
        rows = collapse_equivalent(fuse(groups, params['n'], q), limit)
        for row in rows:
            if row.get('reviewed_decision'):
                row['evidence_kind'] = 'reviewed_decision'
                candidates = {r['card_id']:r for r in reviewed_candidates if r['capture_id']==row['capture_id']}
                best = sorted(candidates.values(),key=lambda r:r['excerpt_relevance'],reverse=True)
                chosen=[];size=0
                for item in best[:3]:
                    if chosen and size+len(item['content'])>1800:break
                    chosen.append(item);size+=len(item['content'])
                if chosen:
                    row['content']='\n\n'.join(r['content'] for r in chosen)
                    row['card_id']=chosen[0]['card_id']
                    row['card_ids']=[r['card_id'] for r in chosen]
                    provenance={c.id:p for c,p in curated[row['capture_id']]}
                    row['decisions']=[dict(card_id=r['card_id'],content=r['content'],
                                           sources=provenance[r['card_id']]['sources']) for r in chosen]
                    row['source_capture_ids']=sorted({ref['capture_id'] for r in chosen for ref in provenance[r['card_id']]['sources']})
        return rows
    def run(sql):
        return [dict(r) for r in session.execute(text(sql), params).mappings()]
    if not q.strip():
        rows = run(f"SELECT {cols},c.raw_content AS content FROM capture c WHERE {filters} ORDER BY c.created_at DESC,c.id DESC LIMIT :limit")
        return [dict(r,id=r['capture_id'],match_type='recent',score=0,memory_system='BYB Shared Memory') for r in rows]
    exact = run(f"SELECT {cols},c.raw_content AS content FROM capture c WHERE {filters} AND strpos(lower(c.raw_content),lower(:q))>0 ORDER BY c.created_at DESC,c.id DESC LIMIT :n")
    aliases = run(f"SELECT {cols},m.id AS card_id,m.content FROM memoryfactcard m JOIN capture c ON c.id=m.source_capture_id WHERE {filters} AND m.memory_status='active' AND {current_card_filter} AND lower(:q)=ANY(string_to_array(lower(coalesce(m.aliases_text,'')), E'\\n')) ORDER BY c.created_at DESC LIMIT :n")
    # Opaque identifiers require a literal match; unrelated nearest vectors are not evidence.
    if re.fullmatch(r'[A-Za-z0-9_:/@.\-]+', q) and (re.search(r'[_:/@\-]',q) or re.fullmatch(r'\d{6,}',q)):
        return finish([('exact',3,exact),('alias',3,aliases)])
    qt = terms(q)
    if not qt and not exact and not aliases:
        return []
    lexical, cards = [], []
    if qt:
        params['tsq'] = ' | '.join("'"+t+"'" for t in qt)
        lexical = run(f"SELECT {cols},c.raw_content AS content FROM capture c WHERE {filters} AND to_tsvector('simple',coalesce(c.raw_content,'')) @@ to_tsquery('simple',:tsq) ORDER BY ts_rank_cd(to_tsvector('simple',coalesce(c.raw_content,'')),to_tsquery('simple',:tsq)) DESC,c.created_at DESC LIMIT :n")
        cards = run(f"SELECT {cols},m.id AS card_id,m.content FROM memoryfactcard m JOIN capture c ON c.id=m.source_capture_id WHERE {filters} AND m.memory_status='active' AND {current_card_filter} AND to_tsvector('simple',coalesce(m.content,'') || ' ' || coalesce(m.aliases_text,'')) @@ to_tsquery('simple',:tsq) ORDER BY ts_rank_cd(to_tsvector('simple',coalesce(m.content,'') || ' ' || coalesce(m.aliases_text,'')),to_tsquery('simple',:tsq)) DESC,c.created_at DESC LIMIT :n")
        # Require a reasonable share of query concepts for lexical-only candidates.
        minimum = 1 if len(qt)<=2 else (len(qt)*3+4)//5
        lexical = [r for r in lexical if sum(t in set(terms(r['raw_content'])) for t in qt)>=minimum]
        cards = [r for r in cards if sum(t in set(terms(r['content'])) for t in qt)>=minimum]
    semantic, card_semantic = [], []
    if include_semantic and not (len(qt)==1 and not lexical and not cards and not exact):
        params['vector'] = '['+','.join(str(x) for x in compute_embedding(q))+']'
        params['threshold'] = MIN_SIMILARITY
        semantic = run(f"SELECT {cols},ch.id AS chunk_id,ch.content,1-(ch.embedding <=> CAST(:vector AS vector)) AS similarity FROM chunk ch JOIN capture c ON c.id=ch.capture_id WHERE {filters} AND 1-(ch.embedding <=> CAST(:vector AS vector))>=:threshold ORDER BY ch.embedding <=> CAST(:vector AS vector) LIMIT :n")
        card_semantic = run(f"SELECT {cols},m.id AS card_id,m.content,1-(m.embedding <=> CAST(:vector AS vector)) AS similarity FROM memoryfactcard m JOIN capture c ON c.id=m.source_capture_id WHERE {filters} AND m.memory_status='active' AND {current_card_filter} AND 1-(m.embedding <=> CAST(:vector AS vector))>=:threshold ORDER BY m.embedding <=> CAST(:vector AS vector) LIMIT :n")
    # Short reviewed decisions should not be crowded out by long raw notes
    # before reranking. English stemming handles save/saving, cancel/cancellation,
    # and singular/plural forms without a list of benchmark-specific synonyms.
    reviewed_lexical=[];reviewed_semantic=[]
    if curated and qt:
        lexemes=session.execute(text("SELECT tsvector_to_array(to_tsvector('english',:q))"),params).scalar_one()
        params['review_tsq']=' | '.join("'"+t.replace("'", "''")+"'" for t in lexemes)
        if lexemes:
            reviewed_lexical=run(f"SELECT {cols},m.id AS card_id,m.content,m.memory_visibility AS card_visibility,tsvector_to_array(to_tsvector('english',m.content)) AS lexemes FROM memoryfactcard m JOIN capture c ON c.id=m.source_capture_id WHERE {filters} AND m.id=ANY(:curated_ids) AND to_tsvector('english',m.content) @@ to_tsquery('english',:review_tsq) ORDER BY ts_rank_cd(to_tsvector('english',m.content),to_tsquery('english',:review_tsq)) DESC LIMIT :n")
            minimum=min(2,len(lexemes))
            reviewed_lexical=[r for r in reviewed_lexical if len(set(r['lexemes'])&set(lexemes))>=minimum]
        if include_semantic and 'vector' in params:
            reviewed_semantic=run(f"SELECT {cols},m.id AS card_id,m.content,m.memory_visibility AS card_visibility,tsvector_to_array(to_tsvector('english',m.content)) AS lexemes,1-(m.embedding <=> CAST(:vector AS vector)) AS similarity FROM memoryfactcard m JOIN capture c ON c.id=m.source_capture_id WHERE {filters} AND m.id=ANY(:curated_ids) AND 1-(m.embedding <=> CAST(:vector AS vector))>=0.60 ORDER BY m.embedding <=> CAST(:vector AS vector) LIMIT :n")
        for row in reviewed_lexical+reviewed_semantic:
            row['reviewed_decision']=True
            row['excerpt_relevance']=(row.get('similarity',0.55)+0.04*len(set(row['lexemes'])&set(lexemes))
                                      +(0.04 if row['card_visibility']=='policy' else 0))
        reviewed_candidates=reviewed_lexical+reviewed_semantic
    return finish([('exact',3,exact),('alias',3,aliases),('lexical',1,lexical),
                   ('card_lexical',1,cards),('semantic',1,semantic),('card_semantic',1,card_semantic),
                   ('curated_lexical',2,reviewed_lexical),('curated_semantic',2,reviewed_semantic)])
