"""Cortex dashboard additions: task/project fields and email-index search demotion."""

import importlib.util
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import services.memory_api.main as main
from db.schema import Capture, Project, Task
from services.memory_api.main import (
    EMAIL_INDEX_DEMOTION_FACTOR,
    AGENT_DASHBOARD_SNAPSHOT_SOURCE_TYPE,
    AGENT_PROFILE_SOURCE_TYPE,
    AgentDashboardSnapshotBody,
    AgentProfileBody,
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


def test_task_body_accepts_attention_fields_and_date_only_snooze():
    body = TaskBody(
        description="Review NSTA unit",
        snooze_until="2099-01-02",
        attention_reason="Greg snoozed from Cortex",
    )

    assert body.attention_state == "snoozed"
    assert body.snooze_until == datetime(2099, 1, 2, 0, 0, 0)
    assert body.attention_reason == "Greg snoozed from Cortex"


def test_task_body_normalizes_delayed_language_to_deferred_without_date():
    body = TaskBody(description="Review NSTA unit", attention_state="Delayed", blocker_label="Maribel finishes")

    assert body.attention_state == "deferred"


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

    attention = TaskStatusBody(attention_state="Deferred", blocker_type="Person", blocker_label="Maribel")
    assert attention.attention_state == "deferred"
    assert attention.blocker_type == "person"


def test_task_model_round_trips_new_fields():
    task = Task(
        description="x",
        draft_text="hello",
        state="deep",
        attention_state="deferred",
        attention_reason="Waiting on content-lock",
        blocker_type="person",
        blocker_label="Maribel content-lock signal",
        blocker_capture_id=12355,
    )

    dumped = task.model_dump()
    assert dumped["draft_text"] == "hello"
    assert dumped["state"] == "deep"
    assert dumped["attention_state"] == "deferred"
    assert dumped["attention_reason"] == "Waiting on content-lock"
    assert dumped["blocker_type"] == "person"
    assert dumped["blocker_label"] == "Maribel content-lock signal"
    assert dumped["blocker_capture_id"] == 12355

    # Existing rows (fields unset) stay valid and default to None.
    legacy = Task(description="y")
    assert legacy.draft_text is None
    assert legacy.state is None
    assert legacy.attention_state == "active"


# ---------------------------------------------------------
# Schema round-trips: projects
# ---------------------------------------------------------

def test_project_body_accepts_new_fields():
    body = ProjectBody(
        slug="songbird",
        title="Songbird",
        lifecycle_state="Open",
        category="neuro",
        last_activity_at="2026-06-11T12:00:00Z",
        waiting_on="external:Moritz",
    )

    assert body.lifecycle_state == "active"
    assert body.category == "neuro"
    assert body.last_activity_at == datetime.fromisoformat("2026-06-11T12:00:00+00:00")
    assert body.waiting_on == "external:Moritz"


def test_project_patch_body_is_partial():
    body = ProjectPatchBody(waiting_on="greg", lifecycle_state="Closed")

    assert body.model_dump(exclude_unset=True) == {"lifecycle_state": "closed", "waiting_on": "greg"}


def test_project_list_state_defaults_to_active():
    assert main._normalize_project_list_state(None, include_closed=False) == "active"
    assert main._normalize_project_list_state(None, include_closed=True) == "all"
    assert main._normalize_project_list_state("open", include_closed=False) == "active"
    assert main._normalize_project_list_state("archived", include_closed=False) == "archived"


def test_project_lifecycle_legacy_closed_fields_are_normalized():
    assert main._project_lifecycle_from_fields(status_value="Active", priority_value="Closed") == "closed"
    assert main._project_lifecycle_from_fields(status_value="Duplicate - closed 2026-06-12", priority_value="High") == "closed"
    assert (
        main._project_lifecycle_from_fields(
            status_value="Work complete. Project kept open due to clawback risk.",
            priority_value="High",
        )
        == "active"
    )


def test_project_model_round_trips_new_fields():
    proj = Project(slug="p", title="P", lifecycle_state="closed", category="plant", waiting_on="greg")

    dumped = proj.model_dump()
    assert dumped["lifecycle_state"] == "closed"
    assert dumped["category"] == "plant"
    assert dumped["waiting_on"] == "greg"
    assert dumped["last_activity_at"] is None


# ---------------------------------------------------------
# Schema/API round-trips: agent-owned dashboards
# ---------------------------------------------------------

def test_agent_profile_body_accepts_lifecycle_fields():
    body = AgentProfileBody(
        agent_id="addy",
        display_name="Addy",
        role="Meta ads",
        color="Purple",
        lifecycle="Retiring",
        retired_at="2026-06-12T12:00:00Z",
    )

    assert body.color == "purple"
    assert body.lifecycle == "retiring"
    assert body.retired_at == datetime.fromisoformat("2026-06-12T12:00:00+00:00")


def test_agent_snapshot_body_accepts_dashboard_lease():
    body = AgentDashboardSnapshotBody(
        agent_id="addy",
        generated_at="2026-06-12T12:00:00Z",
        expires_at="2026-06-13T12:00:00Z",
        status="Needs_You",
        needs_you_count=1,
        blocks=[{"type": "metric", "title": "Metrics", "items": [{"label": "ROAS", "value": "1.7"}]}],
    )

    assert body.status == "needs_you"
    assert body.needs_you_count == 1
    assert body.expires_at == datetime.fromisoformat("2026-06-13T12:00:00+00:00")


class _FakeAllResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _FakeListSession:
    def __init__(self, values):
        self._values = values

    def exec(self, stmt):
        return _FakeAllResult(self._values)


def _client_with_rows(rows) -> TestClient:
    app.dependency_overrides[get_db_session] = lambda: _FakeListSession(rows)
    app.dependency_overrides[get_user_from_api_key] = lambda: "greg"
    return TestClient(app)


def test_agent_registry_returns_latest_profile_from_captures():
    older = Capture(
        id=1,
        raw_content=json.dumps({"kind": AGENT_PROFILE_SOURCE_TYPE, "agent_id": "addy", "display_name": "Old Addy", "role": "Meta", "color": "purple", "lifecycle": "active", "updated_at": "2026-06-10T12:00:00Z"}),
        source="agent:addy",
        user_id="greg",
        source_type=AGENT_PROFILE_SOURCE_TYPE,
        created_at=datetime(2026, 6, 10, 12, 0, 0),
    )
    newer = Capture(
        id=2,
        raw_content=json.dumps({"kind": AGENT_PROFILE_SOURCE_TYPE, "agent_id": "addy", "display_name": "Addy", "role": "Meta ads", "color": "purple", "lifecycle": "retiring", "updated_at": "2026-06-12T12:00:00Z"}),
        source="agent:addy",
        user_id="greg",
        source_type=AGENT_PROFILE_SOURCE_TYPE,
        created_at=datetime(2026, 6, 12, 12, 0, 0),
    )
    client = _client_with_rows([older, newer])

    resp = client.get("/v1/agents")

    assert resp.status_code == 200
    assert resp.json()["agents"] == [
        {"kind": AGENT_PROFILE_SOURCE_TYPE, "agent_id": "addy", "display_name": "Addy", "role": "Meta ads", "color": "purple", "lifecycle": "retiring", "updated_at": "2026-06-12T12:00:00Z"}
    ]


def test_agent_registry_breaks_profile_time_ties_with_capture_order():
    older_capture = Capture(
        id=10,
        raw_content=json.dumps({"kind": AGENT_PROFILE_SOURCE_TYPE, "agent_id": "addy", "display_name": "Addy", "role": "Meta ads", "color": "orange", "lifecycle": "active", "updated_at": "2026-06-14T15:09:00Z"}),
        source="agent:addy",
        user_id="greg",
        source_type=AGENT_PROFILE_SOURCE_TYPE,
        created_at=datetime(2026, 6, 14, 15, 11, 0),
    )
    newer_capture = Capture(
        id=11,
        raw_content=json.dumps({"kind": AGENT_PROFILE_SOURCE_TYPE, "agent_id": "addy", "display_name": "Addy", "role": "Meta ads", "color": "purple", "lifecycle": "active", "updated_at": "2026-06-14T15:09:00Z"}),
        source="agent:addy",
        user_id="greg",
        source_type=AGENT_PROFILE_SOURCE_TYPE,
        created_at=datetime(2026, 6, 14, 15, 46, 0),
    )
    client = _client_with_rows([newer_capture, older_capture])

    resp = client.get("/v1/agents")

    assert resp.status_code == 200
    assert resp.json()["agents"][0]["color"] == "purple"


def test_agent_status_latest_returns_latest_snapshot_from_captures():
    rows = [
        Capture(
            id=3,
            raw_content=json.dumps({"kind": AGENT_DASHBOARD_SNAPSHOT_SOURCE_TYPE, "agent_id": "addy", "generated_at": "2026-06-11T12:00:00Z", "expires_at": "2026-06-12T12:00:00Z", "blocks": []}),
            source="agent:addy",
            user_id="greg",
            source_type=AGENT_DASHBOARD_SNAPSHOT_SOURCE_TYPE,
            created_at=datetime(2026, 6, 11, 12, 0, 0),
        ),
        Capture(
            id=4,
            raw_content=json.dumps({"kind": AGENT_DASHBOARD_SNAPSHOT_SOURCE_TYPE, "agent_id": "addy", "generated_at": "2026-06-12T12:00:00Z", "expires_at": "2026-06-13T12:00:00Z", "status": "healthy", "needs_you_count": 0, "blocks": []}),
            source="agent:addy",
            user_id="greg",
            source_type=AGENT_DASHBOARD_SNAPSHOT_SOURCE_TYPE,
            created_at=datetime(2026, 6, 12, 12, 0, 0),
        ),
    ]
    client = _client_with_rows(rows)

    resp = client.get("/v1/agents/status/latest")

    assert resp.status_code == 200
    assert resp.json()["statuses"] == [
        {"kind": AGENT_DASHBOARD_SNAPSHOT_SOURCE_TYPE, "agent_id": "addy", "generated_at": "2026-06-12T12:00:00Z", "expires_at": "2026-06-13T12:00:00Z", "status": "healthy", "needs_you_count": 0, "blocks": []}
    ]


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


def test_patch_task_attention_to_deferred():
    task = Task(id=394, description="NSTA Units 2-3: Greg review pass", status="To Do")
    client = _client_with(task)

    resp = client.patch(
        "/v1/tasks/394",
        json={
            "attention_state": "deferred",
            "attention_reason": "Waiting for Maribel to signal NSTA Unit 2 content-lock before Greg review.",
            "blocker_type": "person",
            "blocker_label": "Maribel content-lock signal",
            "blocker_capture_id": 12355,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "To Do"
    assert data["attention_state"] == "deferred"
    assert data["attention_reason"] == "Waiting for Maribel to signal NSTA Unit 2 content-lock before Greg review."
    assert data["blocker_type"] == "person"
    assert data["blocker_label"] == "Maribel content-lock signal"
    assert data["blocker_capture_id"] == 12355
    assert data["attention_updated_by"] == "greg"
    assert data["attention_updated_at"]


def test_patch_task_attention_from_deferred_to_active():
    task = Task(
        id=394,
        description="NSTA Units 2-3: Greg review pass",
        status="To Do",
        attention_state="deferred",
        attention_reason="Waiting on Maribel",
        blocker_type="person",
        blocker_label="Maribel content-lock signal",
    )
    client = _client_with(task)

    resp = client.patch(
        "/v1/tasks/394",
        json={
            "attention_state": "active",
            "attention_reason": None,
            "blocker_type": None,
            "blocker_label": None,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["attention_state"] == "active"
    assert data["attention_reason"] is None
    assert data["blocker_type"] is None
    assert data["blocker_label"] is None


def test_patch_task_snoozed_requires_snooze_until():
    task = Task(id=42, description="x", status="To Do")
    client = _client_with(task)

    resp = client.patch("/v1/tasks/42", json={"attention_state": "snoozed"})

    assert resp.status_code == 422
    assert "snooze_until" in resp.json()["detail"]


def test_patch_task_snooze_date_sets_attention_state():
    task = Task(id=42, description="x", status="To Do")
    client = _client_with(task)

    resp = client.patch("/v1/tasks/42", json={"snooze_until": "2099-01-02"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["attention_state"] == "snoozed"
    assert data["snooze_until"].startswith("2099-01-02T00:00:00")


def test_list_tasks_include_complete_false_keeps_deferred_attention():
    client = _client_with_rows(
        [
            Task(id=1, description="Deferred but open", status="To Do", attention_state="deferred"),
            Task(id=2, description="Done", status="Complete", attention_state="active"),
        ]
    )

    resp = client.get("/v1/tasks?include_complete=false")

    assert resp.status_code == 200
    data = resp.json()
    assert [row["id"] for row in data] == [1]
    assert data[0]["attention_state"] == "deferred"


def test_list_tasks_today_eligible_filters_attention_state():
    client = _client_with_rows(
        [
            Task(id=1, description="Active", status="To Do", attention_state="active"),
            Task(id=2, description="Deferred", status="To Do", attention_state="deferred"),
            Task(id=3, description="Future snooze", status="To Do", attention_state="snoozed", snooze_until=datetime(2099, 1, 2)),
            Task(id=4, description="Expired snooze", status="To Do", attention_state="snoozed", snooze_until=datetime(2000, 1, 2)),
            Task(id=5, description="Done", status="Complete", attention_state="active"),
        ]
    )

    resp = client.get("/v1/tasks?today_eligible=true&include_complete=false")

    assert resp.status_code == 200
    assert [row["id"] for row in resp.json()] == [1, 4]


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
    assert set(revisions["f0a4c1d2e3b5"]) == {"a3e9d54c1f72", "b7f93c2d4a10"}
    assert revisions["c4d9f8a2b731"] == "f0a4c1d2e3b5"
    # Exactly one head: every down_revision except one is itself a known revision.
    down_revisions = set()
    for down_revision in revisions.values():
        if isinstance(down_revision, tuple):
            down_revisions.update(down_revision)
        elif down_revision:
            down_revisions.add(down_revision)
    heads = set(revisions) - down_revisions
    assert heads == {"c4d9f8a2b731"}


def test_watson_task_disposition_migration_maps_task_394(tmp_path):
    db_path = tmp_path / "watson_task_state.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE task_dispositions (
                task_key TEXT PRIMARY KEY,
                task_id TEXT,
                source_fingerprint TEXT,
                disposition TEXT NOT NULL,
                reason TEXT,
                actor TEXT,
                source TEXT,
                project_slug TEXT,
                description TEXT,
                source_pointer TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO task_dispositions (
                task_key, task_id, disposition, reason, actor, source, project_slug,
                description, source_pointer, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "nsta-curriculum:394",
                "394",
                "deferred",
                "Waiting for Maribel to signal NSTA Unit 2 content-lock before Greg review.",
                "Greg",
                "cortex-reconciliation-audit",
                "nsta-curriculum",
                "NSTA Units 2-3: Greg review pass",
                "memory_capture:12355",
                "2026-06-15T17:39:50+00:00",
                "2026-06-15T17:39:50+00:00",
                json.dumps({"evidence_capture_id": 12355, "waiting_on": "Maribel content-lock signal"}),
            ),
        )

    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "f0a4c1d2e3b5_add_task_attention_fields.py"
    spec = importlib.util.spec_from_file_location("task_attention_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    updates = module._watson_attention_updates(db_path)

    assert updates == [
        {
            "task_id": 394,
            "attention_state": "deferred",
            "attention_reason": "Waiting for Maribel to signal NSTA Unit 2 content-lock before Greg review.",
            "snooze_until": None,
            "blocker_type": "person",
            "blocker_label": "Maribel content-lock signal",
            "blocker_capture_id": 12355,
            "attention_updated_at": datetime(2026, 6, 15, 17, 39, 50),
            "attention_updated_by": "Greg",
        }
    ]


def test_watson_task_disposition_migration_maps_snooze_date(tmp_path):
    db_path = tmp_path / "watson_task_state.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE task_dispositions (
                task_key TEXT PRIMARY KEY,
                task_id TEXT,
                source_fingerprint TEXT,
                disposition TEXT NOT NULL,
                reason TEXT,
                actor TEXT,
                source TEXT,
                project_slug TEXT,
                description TEXT,
                source_pointer TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO task_dispositions (
                task_key, task_id, disposition, reason, actor, source, project_slug,
                description, source_pointer, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "demo:7",
                "7",
                "snoozed",
                "Greg snoozed from Cortex",
                "Greg",
                "cortex",
                "demo",
                "Review",
                None,
                "2026-06-15T17:39:50+00:00",
                "2026-06-15T17:39:50+00:00",
                json.dumps({"snooze_until_date": "2099-01-02"}),
            ),
        )

    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "f0a4c1d2e3b5_add_task_attention_fields.py"
    spec = importlib.util.spec_from_file_location("task_attention_migration_snooze", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    updates = module._watson_attention_updates(db_path)

    assert updates[0]["task_id"] == 7
    assert updates[0]["attention_state"] == "snoozed"
    assert updates[0]["snooze_until"] == datetime(2099, 1, 2, 0, 0, 0)
    assert updates[0]["blocker_capture_id"] is None


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
