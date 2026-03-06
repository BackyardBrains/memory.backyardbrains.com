"""JSON-LD import/export utilities for ontology interoperability."""

from typing import Any, Optional


def project_to_jsonld(project: Any) -> dict:
    """Convert a Project model to JSON-LD structure."""
    return {
        "@type": "Project",
        "@id": f"memory://project/{project.slug}",
        "slug": project.slug,
        "title": project.title,
        "status": project.status,
        "priority": project.priority,
    }


def task_to_jsonld(task: Any, project_slug: Optional[str] = None) -> dict:
    """Convert a Task model to JSON-LD structure."""
    out: dict = {
        "@type": "Task",
        "@id": f"memory://task/{task.id}",
        "description": task.description,
        "status": task.status,
    }
    if task.due_date:
        out["dueDate"] = task.due_date.isoformat()
    if task.snooze_until:
        out["snoozeUntil"] = task.snooze_until.isoformat()
    if project_slug:
        out["project"] = {"@id": f"memory://project/{project_slug}"}
    return out
