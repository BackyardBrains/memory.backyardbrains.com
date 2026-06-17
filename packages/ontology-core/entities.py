"""Domain entity definitions (Pydantic models for API validation, distinct from DB models)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    slug: str
    title: str
    status: str = "Active"
    priority: str = "Normal"
    lifecycle_state: str = "active"


class ProjectOut(BaseModel):
    id: int
    slug: str
    title: str
    status: str
    priority: str
    lifecycle_state: str = "active"


class TaskCreate(BaseModel):
    description: str
    status: str = "To Do"
    snooze_until: Optional[datetime] = None
    due_date: Optional[datetime] = None
    project_id: Optional[int] = None
    attention_state: str = "active"
    attention_reason: Optional[str] = None
    blocker_type: Optional[str] = None
    blocker_label: Optional[str] = None
    blocker_task_id: Optional[int] = None
    blocker_capture_id: Optional[int] = None
    attention_updated_at: Optional[datetime] = None
    attention_updated_by: Optional[str] = None


class TaskOut(BaseModel):
    id: int
    description: str
    status: str
    snooze_until: Optional[datetime] = None
    due_date: Optional[datetime] = None
    project_id: Optional[int] = None
    attention_state: str = "active"
    attention_reason: Optional[str] = None
    blocker_type: Optional[str] = None
    blocker_label: Optional[str] = None
    blocker_task_id: Optional[int] = None
    blocker_capture_id: Optional[int] = None
    attention_updated_at: Optional[datetime] = None
    attention_updated_by: Optional[str] = None


class TaskStatusUpdate(BaseModel):
    status: Optional[str] = None
    snooze_until: Optional[datetime] = None
    attention_state: Optional[str] = Field(default=None, pattern="^(active|snoozed|deferred)$")
    attention_reason: Optional[str] = None
    blocker_type: Optional[str] = Field(default=None, pattern="^(person|date|dependency|evidence|external|decision)$")
    blocker_label: Optional[str] = None
    blocker_task_id: Optional[int] = None
    blocker_capture_id: Optional[int] = None
    attention_updated_at: Optional[datetime] = None
    attention_updated_by: Optional[str] = None


class PersonCreate(BaseModel):
    slug: str
    name: str
    role: Optional[str] = None


class PersonOut(BaseModel):
    id: int
    slug: str
    name: str
    role: Optional[str] = None


class EventCreate(BaseModel):
    label: str
    date_time: datetime
    location: Optional[str] = None


class EventOut(BaseModel):
    id: int
    label: str
    date_time: datetime
    location: Optional[str] = None


class CaptureCreate(BaseModel):
    raw_content: str
    source: str = Field(..., description="e.g., claude-ios, watson-cron, slack")
