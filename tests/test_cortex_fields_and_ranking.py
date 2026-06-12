"""Cortex dashboard additions: task/project fields and email-index search demotion."""

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import services.memory_api.main as main
from db.schema import Project, Task
from services.memory_api.main import (
    EMAIL_INDEX_DEMOTION_FACTOR,
    ProjectBody,
    ProjectPatchBody,
    TaskBody,
    TaskStatusBody,
    app,
    demote_email_index_results,
    get_db_session,
    get_user_from_api_key,
    search_memory_records,
)


# ---------------------------------------------------------
# Schema round-trips: tasks
# ---------------------------------------------------------

def test_task_body_accepts_draft_text_and_state():
    body = TaskBody(description="Reply to Moritz", draft_text="Hi Moritz,\n\nYes.", state="Ready")

    assert body.draft_text == "Hi Moritz,\n\nYes."
    assert body.state == "ready"  # known states normalize to lowercase


def test_task_body_state_is_tolerant_of_unknown_short_strings():
    body = TaskBody(description="x", state="snoozed-by-watson")

    assert body.state == "snoozed-by-watson"


def test_task_body_state_rejects_absurdly_long_values():
    with pytest.raises(ValueError):
        TaskBody(description="x", state="y" * 100)


def test_task_patch_body_is_fully_optional_and_partial():
    body = TaskStatusBody(draft_text="Draft body", state="decision")

    dumped = body.model_dump(exclude_unset=True)
    assert dumped == {"draft_text": "Draft body", "state": "decision"}

    # Legacy clients that only send status keep working.
    legacy = TaskStatusBody(status="Complete")
    assert legacy.model_dump(exclude_unset=True) == {"status": "Complete"}


def test_task_model_round_trips_new_fields():
    task = Task(description="x", draft_text="hello", state="deep")

    dumped = task.model_dump()
    assert dumped["draft_text"] == "hello"
    assert dumped["state"] == "deep"

    # Existing rows (fields unset) stay valid and default to None.
    legacy = Task(description="y")
    assert legacy.draft_text is None
    assert legacy.state is None


# ---------------------------------------------------------
# Schema round-trips: projects
# ---------------------------------------------------------

def test_project_body_accepts_new_fields():
    body = ProjectBody(
        slug="songbird",
        title="Songbird",
        category="neuro",
        last_activity_at="2026-06-11T12:00:00Z",
        waiting_on="external:Moritz",
    )

    assert body.category == "neuro"
    assert body.last_activity_at == datetime.fromisoformat("2026-06-11T12:00:00+00:00")
    assert body.waiting_on == "external:Moritz"


def test_project_patch_body_is_partial():
    body = ProjectPatchBody(waiting_on="greg")

    assert body.model_dump(exclude_unset=True) == {"waiting_on": "greg"}


def test_project_model_round_trips_new_fields():
    proj = Project(slug="p", title="P", category="plant", waiting_on="greg")

    dumped = proj.model_dump()
    assert dumped["category"] == "plant"
    assert dumped["waiting_on"] == "greg"
    assert dumped["last_activity_at"] is None


# ---------------------------------------------------------
# PATCH endpoints (FastAPI round-trip with a fake session)
# ---------------------------------------------------------

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeSession:
    def __init__(self, obj):
        self._obj = obj

    def get(self, model, obj_id):
        return self._obj

    def exec(self, stmt):
        return _FakeResult(self._obj)

    def add(self, obj):
        pass

    def commit(self):
        pass

    def refresh(self, obj):
        pass


def _client_with(obj) -> TestClient:
    app.dependency_overrides[get_db_session] = lambda: _FakeSession(obj)
    app.dependency_overrides[get_user_from_api_key] = lambda: "greg"
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_patch_task_accepts_draft_text_and_state():
    task = Task(id=42, description="Reply to Moritz", status="To Do")
    client = _client_with(task)

    resp = client.patch("/v1/tasks/42", json={"draft_text": "Hi Moritz,", "state": "ready"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["draft_text"] == "Hi Moritz,"
    assert data["state"] == "ready"
    assert data["status"] == "To Do"  # untouched by partial patch


def test_patch_task_status_only_still_works():
    task = Task(id=42, description="x", status="To Do", draft_text="keep me")
    client = _client_with(task)

    resp = client.patch("/v1/tasks/42", json={"status": "Complete"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "Complete"
    assert data["draft_text"] == "keep me"


def test_patch_project_new_fields():
    proj = Project(id=7, slug="songbird", title="Songbird")
    client = _client_with(proj)

    resp = client.patch(
        "/v1/projects/songbird",
        json={"category": "neuro", "waiting_on": "external:Moritz", "last_activity_at": "2026-06-11T12:00:00Z"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["category"] == "neuro"
    assert data["waiting_on"] == "external:Moritz"
    assert data["title"] == "Songbird"  # untouched by partial patch


# ---------------------------------------------------------
# Migration sanity
# ---------------------------------------------------------

def test_cortex_migration_extends_current_head():
    versions_dir = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    revisions = {}
    for path in versions_dir.glob("*.py"):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        revisions[module.revision] = module.down_revision

    assert revisions["a3e9d54c1f72"] == "91d2b7c4e6f0"
    # Exactly one head: every down_revision except one is itself a known revision.
    heads = set(revisions) - set(revisions.values())
    assert heads == {"a3e9d54c1f72"}


# ---------------------------------------------------------
# Search ranking: email-index capture demotion
# ---------------------------------------------------------

def _semantic_result(capture_id, raw_content, source, score):
    return {
        "id": capture_id,
        "capture_id": capture_id,
        "chunk_id": capture_id * 10,
        "raw_content": raw_content,
        "content": raw_content,
        "source": source,
        "match_type": "semantic",
        "rank_reason": "vector similarity",
        "score": score,
    }


def _empty_group(*args, **kwargs):
    return []


def test_email_index_capture_ranks_below_comparable_match_but_is_returned(monkeypatch):
    email = _semantic_result(1, "[email][waiting-on-greg] Moritz asking about songbird grant budget", "claude-desktop-1", 100.0)
    organic = _semantic_result(2, "Greg decided the songbird grant budget caps at 40k", "claude-ios", 99.0)

    for name in ("exact_capture_search", "exact_card_alias_search", "lexical_card_search", "lexical_capture_search", "semantic_card_search"):
        monkeypatch.setattr(main, name, _empty_group)
    monkeypatch.setattr(main, "semantic_chunk_search", lambda *a, **k: [email, organic])

    results = search_memory_records(
        q="songbird grant budget", limit=10, project_slug=None, session=None, user_id="greg"
    )

    assert [r["capture_id"] for r in results] == [2, 1]  # demoted below, not dropped
    demoted = results[1]
    assert demoted["score"] == pytest.approx(100.0 * EMAIL_INDEX_DEMOTION_FACTOR)
    assert "demoted" in demoted["rank_reason"]
    assert "demoted" not in results[0]["rank_reason"]


def test_email_index_demotion_also_applies_by_source(monkeypatch):
    by_source = _semantic_result(3, "Thread summary without tag prefix", "claude-desktop-1", 100.0)
    organic = _semantic_result(4, "Organic memory", "slack", 99.0)

    results = demote_email_index_results([by_source, organic])

    assert [r["capture_id"] for r in results] == [4, 3]
    assert "demoted" in results[1]["rank_reason"]


def test_exact_thread_id_lookup_is_not_demoted(monkeypatch):
    thread_id = "19725fbc8d3a41e2"
    email_exact = {
        "id": 5,
        "capture_id": 5,
        "chunk_id": None,
        "raw_content": f"[email][draft-aging] threadId {thread_id} from Moritz",
        "content": f"[email][draft-aging] threadId {thread_id} from Moritz",
        "source": "claude-desktop-1",
        "match_type": "exact",
        "rank_reason": "substring match on identifier",
        "score": 1000.0,
    }

    for name in ("exact_card_alias_search", "lexical_card_search", "lexical_capture_search", "semantic_card_search", "semantic_chunk_search"):
        monkeypatch.setattr(main, name, _empty_group)
    monkeypatch.setattr(main, "exact_capture_search", lambda *a, **k: [email_exact])

    results = search_memory_records(
        q=thread_id, limit=10, project_slug=None, session=None, user_id="greg"
    )

    assert len(results) == 1
    assert results[0]["capture_id"] == 5
    assert results[0]["score"] == 1000.0  # exact/lexical path untouched
    assert "demoted" not in results[0]["rank_reason"]
