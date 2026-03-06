#!/usr/bin/env python3
"""
Migrate Watson's JSON-LD ontology to memory-api.

Usage:
    MEMORY_API_URL=https://memory.backyardbrains.com \\
    MEMORY_API_KEY=sk_byb_greg_local \\
    PYTHONPATH=/var/www/memory.backyardbrains.com \\
    python scripts/migrate_ontology_from_jsonld.py \\
        /var/www/openclaw.backyardbrains.com/workspaces/main/memory/ontology/entities \\
        [--clear]

With --clear: truncates existing projects, tasks, events, persons before importing.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx

MEMORY_API_URL = os.getenv("MEMORY_API_URL", "https://memory.backyardbrains.com")
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "sk_byb_greg_local")
HEADERS = {"X-API-Key": MEMORY_API_KEY, "Content-Type": "application/json"}


def load_jsonld(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("@graph", data) if isinstance(data, dict) else (data if isinstance(data, list) else [])


def slug_from_id(id_str: str, prefix: str) -> str:
    """Extract slug from byb:type/slug."""
    if not id_str or not id_str.startswith(prefix):
        return ""
    return id_str[len(prefix) :].strip()


def parse_date(s: str | None) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    if len(s) <= 10:  # YYYY-MM-DD
        return f"{s}T12:00:00"
    return s


def run_migrate(entities_dir: Path, clear_first: bool) -> None:
    entities_dir = Path(entities_dir)
    if not entities_dir.is_dir():
        print(f"Error: {entities_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    if clear_first:
        print("Clearing existing ontology via direct DB...")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from db.engine import engine
        from db.schema import Event, Person, Project, Task

        from sqlmodel import Session, select

        with Session(engine) as session:
            from db.schema import Link
            for table, model in [(Task, Task), (Event, Event), (Person, Person), (Link, Link), (Project, Project)]:
                rows = list(session.exec(select(model)).all())
                for row in rows:
                    session.delete(row)
                session.commit()
                print(f"  Cleared {len(rows)} from {model.__tablename__}")

    projects_data = load_jsonld(entities_dir / "projects.jsonld")
    tasks_data = load_jsonld(entities_dir / "tasks.jsonld")
    events_data = load_jsonld(entities_dir / "events.jsonld")
    persons_data = load_jsonld(entities_dir / "persons.jsonld")
    links_data = load_jsonld(entities_dir / "links.jsonld")

    slug_to_project_id: dict[str, int] = {}

    # 1. Projects
    print(f"Importing {len(projects_data)} projects...")
    for p in projects_data:
        slug = slug_from_id(p.get("@id", ""), "byb:project/")
        if not slug:
            continue
        try:
            r = httpx.post(
                f"{MEMORY_API_URL}/v1/projects",
                json={
                    "slug": slug,
                    "title": p.get("title", slug),
                    "status": p.get("status", "Active"),
                    "priority": p.get("priority", "Normal"),
                },
                headers=HEADERS,
                timeout=30,
            )
            if r.status_code == 409:
                # Fetch existing to get id
                r2 = httpx.get(f"{MEMORY_API_URL}/v1/projects/{slug}", headers=HEADERS, timeout=30)
                if r2.status_code == 200:
                    slug_to_project_id[slug] = r2.json()["id"]
                continue
            r.raise_for_status()
            slug_to_project_id[slug] = r.json()["id"]
        except Exception as e:
            print(f"  Error project {slug}: {e}")

    # 2. Persons
    print(f"Importing {len(persons_data)} persons...")
    for p in persons_data:
        slug = slug_from_id(p.get("@id", ""), "byb:person/")
        if not slug:
            continue
        try:
            r = httpx.post(
                f"{MEMORY_API_URL}/v1/persons",
                json={
                    "slug": slug,
                    "name": p.get("name", slug),
                    "role": p.get("role"),
                },
                headers=HEADERS,
                timeout=30,
            )
            if r.status_code == 409:
                continue
            r.raise_for_status()
        except Exception as e:
            print(f"  Error person {slug}: {e}")

    # 3. Events
    print(f"Importing {len(events_data)} events...")
    for e in events_data:
        dt = parse_date(e.get("date") or e.get("dateTime"))
        if not dt:
            continue
        try:
            r = httpx.post(
                f"{MEMORY_API_URL}/v1/events",
                json={"label": e.get("label", ""), "date_time": dt, "location": e.get("location")},
                headers=HEADERS,
                timeout=30,
            )
            r.raise_for_status()
        except Exception as ex:
            print(f"  Error event {e.get('label', '?')}: {ex}")

    # 4. Tasks
    print(f"Importing {len(tasks_data)} tasks...")
    for t in tasks_data:
        part_of = t.get("partOf", "")
        proj_slug = slug_from_id(part_of, "byb:project/")
        project_id = slug_to_project_id.get(proj_slug) if proj_slug else None

        desc = t.get("description", "")
        note = t.get("note", "")
        if note:
            desc = f"{desc}\n\nNote: {note}"
        status = t.get("status", "To Do")

        payload = {
            "description": desc[:10000],  # reasonable limit
            "status": status,
            "project_id": project_id,
        }
        if t.get("dueDate"):
            payload["due_date"] = parse_date(t["dueDate"])

        try:
            r = httpx.post(f"{MEMORY_API_URL}/v1/tasks", json=payload, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except Exception as ex:
            print(f"  Error task {t.get('description', '?')[:50]}: {ex}")

    # 5. Links
    print(f"Importing {len(links_data)} links...")
    for lnk in links_data:
        slug = slug_from_id(lnk.get("@id", ""), "byb:link/")
        if not slug:
            continue
        related = lnk.get("relatedTo", [])
        proj_slug = None
        if related:
            first = related[0] if isinstance(related, list) else related
            proj_slug = slug_from_id(str(first), "byb:project/")
        payload = {
            "slug": slug,
            "label": lnk.get("label", slug),
            "url": lnk.get("description", ""),
            "folder_id": lnk.get("folderId"),
            "note": lnk.get("note"),
            "policy": lnk.get("policy"),
            "project_slug": proj_slug,
        }
        try:
            r = httpx.post(f"{MEMORY_API_URL}/v1/links", json=payload, headers=HEADERS, timeout=30)
            if r.status_code == 409:
                continue
            r.raise_for_status()
        except Exception as ex:
            print(f"  Error link {slug}: {ex}")

    print("Migration complete.")


def main():
    parser = argparse.ArgumentParser(description="Migrate JSON-LD ontology to memory-api")
    parser.add_argument("entities_dir", help="Path to ontology/entities directory")
    parser.add_argument("--clear", action="store_true", help="Clear existing data before import")
    args = parser.parse_args()
    run_migrate(Path(args.entities_dir), args.clear)


if __name__ == "__main__":
    main()
