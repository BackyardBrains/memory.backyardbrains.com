"""The brief is the context a chat assistant can actually read: small, with the owner's rules whole."""

import json
from datetime import datetime

from fastapi.testclient import TestClient

from db.schema import Capture
from packages.memory_retrieval import context
from services.memory_api.main import app, get_db_session, get_user_from_api_key


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, policies):
        self.policies = policies

    def exec(self, stmt):
        return _Result(self.policies)


def _policy(cid, text, scope="global", project=None, day=1):
    return Capture(id=cid, raw_content=text, source="server-claude", user_id="greg", memory_status="active",
                   memory_visibility="policy", policy_scope=scope, project_slug=project,
                   created_at=datetime(2026, 11, day, 12, 0))


POLICIES = [
    _policy(113900, "[coach:watson][greg-vocabulary] \"My share\" means share.backyardbrains.com.", day=21),
    _policy(113950, "[coach:watson] the [standing-rules] note should also say dates carry a weekday.", day=22),
    _policy(113418, "[coach:watson][standing-rules] 1. Watson is the single source of truth. 2. No guessing.", day=16),
    _policy(113001, "[coach:watson][standing-rules] an older list that was replaced", day=2),
    _policy(112152, "[coach:watson] HARD: release means merge, deploy and verify live.", day=3),
    _policy(111059, "[coach:watson][decision] Recording browser: tap opens it immediately. " + "x" * 2000, scope="project", project="spikerecorder", day=4),
]


def _search(query, limit, project, session, user_id, **_):
    assert limit >= 11
    return [
        dict(capture_id=113418, memory_visibility="policy", content="standing rules again", created_at=None, source="claude"),
        dict(capture_id=112152, memory_visibility="policy", content=POLICIES[3].raw_content, created_at=None, source="claude", project_slug=None),
        dict(capture_id=111059, memory_visibility="policy", content=POLICIES[4].raw_content, created_at=None, source="server-claude", project_slug="spikerecorder"),
        dict(capture_id=12000, memory_visibility="knowledge", content="share.backyardbrains.com is the public agent-drop surface. " + "y" * 3000, created_at=None, source="spike"),
    ]


def test_brief_returns_standing_rules_whole_and_everything_else_as_excerpts(monkeypatch):
    monkeypatch.setattr(context, "search_records", _search)

    brief = context.brief_packet(_Session(POLICIES), "greg", "add a doc to my share", client="claude-duet")

    assert brief["mode"] == "brief" and brief["status"] == "ok"
    assert brief["standing_rules"]["capture_id"] == 113418, "the newest standing-rules capture, in full"
    assert brief["standing_rules"]["content"] == POLICIES[1].raw_content
    assert brief["vocabulary"]["capture_id"] == 113900
    # The standing rules are not repeated among the task rules.
    assert [r["capture_id"] for r in brief["rules_for_task"]] == [112152, 111059]
    assert [r["capture_id"] for r in brief["evidence"]] == [12000]
    assert len(brief["rules_for_task"][1]["content"]) <= context.BRIEF_RULE_CHARS + 2
    assert len(brief["evidence"][0]["content"]) <= context.BRIEF_EVIDENCE_CHARS + 2
    assert brief["rule_counts"] == {"all": 5, "standing": 4, "this_project": 0}
    assert len(json.dumps(brief, default=str)) < 6000


def test_brief_without_a_question_still_carries_rules_and_vocabulary(monkeypatch):
    monkeypatch.setattr(context, "search_records", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no search without a question")))

    brief = context.brief_packet(_Session(POLICIES), "greg", "", project="spikerecorder")

    assert brief["standing_rules"]["capture_id"] == 113418 and brief["vocabulary"]["capture_id"] == 113900
    assert brief["rules_for_task"] == [] and brief["evidence"] == []
    assert brief["evidence_status"] == "not_requested"
    assert brief["rule_counts"]["this_project"] == 1


def test_brief_endpoint_is_served(monkeypatch):
    monkeypatch.setattr(context, "search_records", _search)
    app.dependency_overrides[get_db_session] = lambda: _Session(POLICIES)
    app.dependency_overrides[get_user_from_api_key] = lambda: "greg"
    try:
        resp = TestClient(app).get("/v1/brief", params={"q": "add a doc to my share", "client": "claude-duet"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json()["vocabulary"]["capture_id"] == 113900
