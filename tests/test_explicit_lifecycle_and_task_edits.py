"""Projects close only when asked to, and task edits are applied instead of dropped.

Regressions covered: a status update mentioning a "merged" pull request, a
"canceled" meeting or a "disclosed" conflict closed the project it described,
and PATCH /v1/tasks returned 200 while ignoring description, due date and
project. Uses a fake session; no database.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from db.schema import MemoryRevision, Project, Task
from services.memory_api.main import app, get_db_session, get_user_from_api_key


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Session:
    def __init__(self, *, projects=(), task=None, revisions=()):
        self.projects = {project.slug: project for project in projects}
        self.task = task
        self.revisions = list(revisions)
        self.added = []
        self.statements = []

    def get(self, model, obj_id):
        if model is Task:
            return self.task
        return next((project for project in self.projects.values() if project.id == obj_id), None)

    def exec(self, stmt):
        self.statements.append(str(stmt))
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Project:
            slug = stmt.whereclause.right.value
            return _Result([self.projects[slug]] if slug in self.projects else [])
        if entity is MemoryRevision:
            return _Result(self.revisions)
        if entity is Task:
            return _Result([self.task] if self.task else [])
        raise AssertionError(f"unexpected query for {entity}")

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def refresh(self, obj):
        pass


def _client(session: _Session) -> TestClient:
    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_user_from_api_key] = lambda: "greg"
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def _patch_project(project: Project, body: dict):
    return _client(_Session(projects=[project])).patch(f"/v1/projects/{project.slug}", json={"revision_reason": "true-up", **body})


@pytest.mark.parametrize(
    "status",
    [
        "2026-10-12 afternoon: PR 529 is merged; the unmerged fix waits on Stan.",
        "2026-11-04 afternoon: Elisabeth canceled the Fri Nov 6 scope discussion.",
        "2026-11-20: Greg is accepting the UM IRB role; the conflict was disclosed to Luca.",
        "2026-11-11: Guram posted an updated version; one duplicate slide remains.",
        "The Spike server cutover is complete: the old harness is retired.",
    ],
)
def test_status_prose_does_not_close_a_live_project(status):
    project = Project(id=7, slug="spikerecorder", title="SpikeRecorder", status="Active", priority="High", lifecycle_state="active")

    resp = _patch_project(project, {"status": status})

    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle_state"] == "active"
    assert resp.json()["status"] == status
    assert "warnings" not in resp.json()


@pytest.mark.parametrize("status", ["Closed at Greg's direction.", "2026-11-20 afternoon: Closed at Greg's direction (via Cortex)."])
def test_closing_words_without_lifecycle_state_warn_instead_of_closing(status):
    project = Project(id=17, slug="nsf-sbir-embrained", title="NSF SBIR", status="Active", priority="Medium", lifecycle_state="active")

    resp = _patch_project(project, {"status": status})

    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle_state"] == "active"
    assert 'lifecycle_state="closed"' in resp.json()["warnings"][0]


def test_explicit_lifecycle_state_closes_a_project():
    project = Project(id=17, slug="nsf-sbir-embrained", title="NSF SBIR", status="Active", priority="Medium", lifecycle_state="active")

    resp = _patch_project(project, {"status": "Closed at Greg's direction.", "lifecycle_state": "closed"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle_state"] == "closed"
    assert "warnings" not in resp.json()


def test_priority_closed_still_closes_a_project():
    project = Project(id=12, slug="byb-serbia-workshops", title="Workshops", status="Active", priority="Normal", lifecycle_state="active")

    resp = _patch_project(project, {"priority": "Closed"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle_state"] == "closed"


def test_closed_project_stays_closed_on_a_status_update():
    project = Project(id=13, slug="swarm-biotactics", title="Swarm", status="Closed at Greg's direction", priority="Low", lifecycle_state="closed")

    resp = _patch_project(project, {"status": "2026-11-21: Elisabeth wrote again; no reply planned."})

    assert resp.status_code == 200, resp.text
    assert resp.json()["lifecycle_state"] == "closed"


def test_project_revisions_are_listed_newest_first_with_before_and_after():
    project = Project(id=8, slug="urop", title="UROP", status="disclosed", priority="Medium", lifecycle_state="closed")
    revision = MemoryRevision(
        id=901, target_type="project", target_id=8, user_id="greg", action="update", actor="grokbot", reason="true-up",
        before_json='{"lifecycle_state": "active"}', after_json='{"lifecycle_state": "closed"}',
        created_at=datetime(2026, 11, 20, 11, 53),
    )

    session = _Session(projects=[project], revisions=[revision])
    resp = _client(session).get("/v1/projects/urop/revisions")

    assert resp.status_code == 200, resp.text
    assert "ORDER BY memoryrevision.id DESC" in session.statements[-1]
    data = resp.json()
    assert data["slug"] == "urop" and data["lifecycle_state"] == "closed"
    assert data["revisions"][0]["before"] == {"lifecycle_state": "active"}
    assert data["revisions"][0]["after"] == {"lifecycle_state": "closed"}
    assert data["revisions"][0]["actor"] == "grokbot"


def test_patch_task_applies_description_due_date_and_project():
    task = Task(id=638, description="Email MIT Press", status="To Do", project_id=None)
    project = Project(id=3, slug="mit-book", title="MIT book")

    resp = _client(_Session(projects=[project], task=task)).patch(
        "/v1/tasks/638",
        json={"description": "Email MIT Press for the new editor and a deadline", "dueDate": "2026-11-24", "project_slug": "mit-book"},
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["description"] == "Email MIT Press for the new editor and a deadline"
    assert data["due_date"] == "2026-11-24T00:00:00"
    assert data["project_id"] == 3
    assert data["status"] == "To Do"  # untouched by partial patch


def test_patch_task_can_clear_due_date_and_move_by_project_id():
    task = Task(id=638, description="Email MIT Press", status="To Do", project_id=3, due_date=datetime(2026, 11, 24))
    project = Project(id=15, slug="byb-ops", title="BYB ops")

    resp = _client(_Session(projects=[project], task=task)).patch("/v1/tasks/638", json={"due_date": None, "project_id": 15})

    assert resp.status_code == 200, resp.text
    assert resp.json()["due_date"] is None
    assert resp.json()["project_id"] == 15


@pytest.mark.parametrize(
    "body",
    [{"project_slug": "no-such-project"}, {"project_id": 999}, {"description": "   "}, {"project_slug": "mit-book", "project_id": 15}],
)
def test_patch_task_rejects_edits_it_cannot_apply(body):
    task = Task(id=638, description="Email MIT Press", status="To Do")
    project = Project(id=3, slug="mit-book", title="MIT book")

    resp = _client(_Session(projects=[project], task=task)).patch("/v1/tasks/638", json=body)

    assert resp.status_code == 422, resp.text
    assert task.description == "Email MIT Press" and task.project_id is None


def test_task_list_asks_for_newest_rows_first():
    # The fake session cannot sort, so this checks the query itself: with the
    # 200-row cap, ascending order returned the oldest tasks and cut the new ones.
    session = _Session(task=Task(id=638, description="Email MIT Press", status="To Do"))

    resp = _client(session).get("/v1/tasks", params={"include_complete": "false", "limit": 200})

    assert resp.status_code == 200, resp.text
    assert "ORDER BY task.id DESC" in session.statements[-1]
    assert [row["id"] for row in resp.json()] == [638]
