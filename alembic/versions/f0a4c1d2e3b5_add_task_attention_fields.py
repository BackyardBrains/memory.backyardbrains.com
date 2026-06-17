"""add task attention fields

Revision ID: f0a4c1d2e3b5
Revises: a3e9d54c1f72, b7f93c2d4a10
Create Date: 2026-06-15 00:00:00.000000
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f0a4c1d2e3b5"
down_revision: Union[str, Sequence[str], None] = ("a3e9d54c1f72", "b7f93c2d4a10")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WATSON_TASK_STATE_DB = Path("/var/www/openclaw.backyardbrains.com/memory/watson_task_state.sqlite3")
VALID_BLOCKER_TYPES = {"person", "date", "dependency", "evidence", "external", "decision"}


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        clean = str(value).strip()
        if not clean:
            return None
        return int(clean)
    except (TypeError, ValueError):
        return None


def _parse_json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        parsed = value
    else:
        clean = str(value).strip()
        if not clean:
            return None
        try:
            parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed and parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _capture_id_from_pointer(value: Any) -> int | None:
    if not value:
        return None
    pointer = str(value).strip()
    if pointer.startswith("memory_capture:"):
        return _safe_int(pointer.split(":", 1)[1])
    return None


def _normalize_blocker_type(value: Any) -> str | None:
    if value is None:
        return None
    blocker_type = str(value).strip().lower()
    return blocker_type if blocker_type in VALID_BLOCKER_TYPES else None


def _watson_update_from_disposition(row: dict[str, Any]) -> dict[str, Any] | None:
    task_id = _safe_int(row.get("task_id"))
    disposition = str(row.get("disposition") or "").strip().lower()
    if task_id is None or disposition not in {"snoozed", "deferred"}:
        return None

    metadata = _parse_json_object(row.get("metadata_json"))
    update = {
        "task_id": task_id,
        "attention_state": disposition,
        "attention_reason": row.get("reason") or f"Migrated from Watson {disposition} disposition",
        "snooze_until": None,
        "blocker_type": None,
        "blocker_label": None,
        "blocker_capture_id": None,
        "attention_updated_at": _parse_datetime(row.get("updated_at")),
        "attention_updated_by": row.get("actor") or row.get("source") or "watson",
    }

    if disposition == "snoozed":
        update["snooze_until"] = _parse_datetime(
            metadata.get("snooze_until_date")
            or metadata.get("snooze_until")
            or metadata.get("snoozed_until")
        )
        return update

    blocker_label = metadata.get("blocker_label") or metadata.get("waiting_on")
    update["blocker_label"] = str(blocker_label).strip() if blocker_label else None
    update["blocker_type"] = _normalize_blocker_type(metadata.get("blocker_type")) or (
        "person" if metadata.get("waiting_on") else None
    )
    update["blocker_capture_id"] = (
        _safe_int(metadata.get("evidence_capture_id"))
        or _safe_int(metadata.get("blocker_capture_id"))
        or _capture_id_from_pointer(row.get("source_pointer"))
    )
    return update


def _read_watson_task_dispositions(path: Path = WATSON_TASK_STATE_DB) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT task_id, disposition, reason, actor, source, source_pointer, updated_at, metadata_json
            FROM task_dispositions
            WHERE lower(disposition) IN ('snoozed', 'deferred')
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _watson_attention_updates(path: Path = WATSON_TASK_STATE_DB) -> list[dict[str, Any]]:
    updates = []
    for row in _read_watson_task_dispositions(path):
        update = _watson_update_from_disposition(row)
        if update:
            updates.append(update)
    return updates


def _terminal_status_filter_sql() -> str:
    return """
        lower(coalesce(status, '')) NOT LIKE '%complete%'
        AND lower(coalesce(status, '')) NOT LIKE '%done%'
        AND lower(coalesce(status, '')) NOT LIKE '%dropped%'
        AND lower(coalesce(status, '')) NOT LIKE '%canceled%'
        AND lower(coalesce(status, '')) NOT LIKE '%cancelled%'
        AND lower(coalesce(status, '')) NOT LIKE '%closed%'
        AND coalesce(status, '') NOT LIKE '%✅%'
    """


def _capture_exists(bind, capture_id: int | None) -> bool:
    if capture_id is None:
        return False
    return bool(bind.execute(sa.text("SELECT 1 FROM capture WHERE id = :id"), {"id": capture_id}).first())


def upgrade() -> None:
    op.add_column("task", sa.Column("attention_state", sa.Text(), nullable=False, server_default="active"))
    op.add_column("task", sa.Column("attention_reason", sa.Text(), nullable=True))
    op.add_column("task", sa.Column("blocker_type", sa.Text(), nullable=True))
    op.add_column("task", sa.Column("blocker_label", sa.Text(), nullable=True))
    op.add_column("task", sa.Column("blocker_task_id", sa.Integer(), nullable=True))
    op.add_column("task", sa.Column("blocker_capture_id", sa.Integer(), nullable=True))
    op.add_column("task", sa.Column("attention_updated_at", sa.DateTime(), nullable=True))
    op.add_column("task", sa.Column("attention_updated_by", sa.String(), nullable=True))
    op.create_foreign_key("fk_task_blocker_task_id_task", "task", "task", ["blocker_task_id"], ["id"])
    op.create_foreign_key("fk_task_blocker_capture_id_capture", "task", "capture", ["blocker_capture_id"], ["id"])
    op.create_index("ix_task_attention_state", "task", ["attention_state"])
    op.create_index("ix_task_blocker_task_id", "task", ["blocker_task_id"])
    op.create_index("ix_task_blocker_capture_id", "task", ["blocker_capture_id"])

    op.execute(
        """
        UPDATE task
        SET attention_state = 'deferred',
            attention_reason = coalesce(attention_reason, 'Migrated from legacy Deferred status'),
            status = 'To Do'
        WHERE lower(coalesce(status, '')) LIKE '%deferred%'
        """
    )
    op.execute(
        f"""
        UPDATE task
        SET attention_state = 'snoozed',
            attention_reason = coalesce(attention_reason, 'Migrated from legacy snooze_until')
        WHERE snooze_until IS NOT NULL
          AND snooze_until > CURRENT_TIMESTAMP
          AND attention_state <> 'deferred'
          AND {_terminal_status_filter_sql()}
        """
    )

    bind = op.get_bind()
    for update in _watson_attention_updates():
        if update["blocker_capture_id"] is not None and not _capture_exists(bind, update["blocker_capture_id"]):
            update["blocker_capture_id"] = None
        bind.execute(
            sa.text(
                """
                UPDATE task
                SET attention_state = :attention_state,
                    attention_reason = :attention_reason,
                    snooze_until = :snooze_until,
                    blocker_type = coalesce(:blocker_type, blocker_type),
                    blocker_label = coalesce(:blocker_label, blocker_label),
                    blocker_capture_id = :blocker_capture_id,
                    attention_updated_at = coalesce(:attention_updated_at, attention_updated_at),
                    attention_updated_by = coalesce(:attention_updated_by, attention_updated_by)
                WHERE id = :task_id
                """
            ),
            update,
        )


def downgrade() -> None:
    op.drop_index("ix_task_blocker_capture_id", table_name="task")
    op.drop_index("ix_task_blocker_task_id", table_name="task")
    op.drop_index("ix_task_attention_state", table_name="task")
    op.drop_constraint("fk_task_blocker_capture_id_capture", "task", type_="foreignkey")
    op.drop_constraint("fk_task_blocker_task_id_task", "task", type_="foreignkey")
    op.drop_column("task", "attention_updated_by")
    op.drop_column("task", "attention_updated_at")
    op.drop_column("task", "blocker_capture_id")
    op.drop_column("task", "blocker_task_id")
    op.drop_column("task", "blocker_label")
    op.drop_column("task", "blocker_type")
    op.drop_column("task", "attention_reason")
    op.drop_column("task", "attention_state")
