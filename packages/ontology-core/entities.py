"""Domain entity definitions (Pydantic models for API validation, distinct from DB models)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    slug: str
    title: str
    status: str = "Active"
    priority: str = "Normal"


class ProjectOut(BaseModel):
    id: int
    slug: str
    title: str
    status: str
    priority: str


class TaskCreate(BaseModel):
    description: str
    status: str = "To Do"
    snooze_until: Optional[datetime] = None
    due_date: Optional[datetime] = None
    project_id: Optional[int] = None


class TaskOut(BaseModel):
    id: int
    description: str
    status: str
    snooze_until: Optional[datetime] = None
    due_date: Optional[datetime] = None
    project_id: Optional[int] = None


class TaskStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(To Do|In Progress|Complete|Deferred)$")


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
