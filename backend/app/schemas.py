"""Public API schemas. Deliberately excludes paths, commands, worker logs, tokens and lease data."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field



class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterIn(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=80)
    invite_code: str = Field(default="", max_length=128)


class LoginIn(StrictModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class RefreshIn(StrictModel):
    refresh_token: str | None = Field(default=None, max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    display_name: str


class AuthOut(BaseModel):
    user: UserOut
    csrf_token: str | None = None
    # Native clients only (X-Client: native): tokens are returned in the body instead of cookies.
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None


class InspectionCreate(StrictModel):
    url: str = Field(min_length=1, max_length=2048)


class ErrorOut(BaseModel):
    code: str
    message: str


class OptionOut(BaseModel):
    id: str
    label: str
    kind: Literal["video", "audio"]
    height: int | None = None
    ext: str
    estimated_bytes: int | None = None


class InspectionOut(BaseModel):
    id: uuid.UUID
    status: Literal["pending", "running", "succeeded", "failed"]
    url: str
    title: str | None = None
    duration_seconds: int | None = None
    extractor: str | None = None
    options: list[OptionOut] = []
    error: ErrorOut | None = None
    expires_at: datetime


class JobCreate(StrictModel):
    inspection_id: uuid.UUID
    selection: str = Field(min_length=2, max_length=24, pattern=r"^[a-z0-9_]+$")


class FileOut(BaseModel):
    name: str
    size: int
    mime: str


class DeliveryOut(BaseModel):
    """Device transfer, tracked separately from server processing.

    ``ready`` = on the server, not yet fetched. ``transferring`` = a stream is in progress.
    ``saved`` = the client reported it stored the file (informational; the server cannot verify).
    """

    state: Literal["unavailable", "ready", "transferring", "sent", "saved"]
    saved_at: datetime | None = None


class ActionsOut(BaseModel):
    pause: bool
    resume: bool
    cancel: bool
    retry: bool
    delete: bool
    download: bool


class JobOut(BaseModel):
    id: uuid.UUID
    url: str
    host: str
    title: str | None
    duration_seconds: int | None
    selection: str
    status: str
    stage: str | None
    progress_percent: float
    downloaded_bytes: int
    total_bytes: int | None
    speed_bps: int | None
    eta_seconds: int | None
    attempt: int
    max_attempts: int
    retry_at: datetime | None = None
    error: ErrorOut | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime | None
    file: FileOut | None = None
    delivery: DeliveryOut
    actions: ActionsOut


class JobPage(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    page_size: int


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    at: datetime
    kind: str
    from_status: str | None
    to_status: str | None
    message: str


class TicketOut(BaseModel):
    url: str
    expires_at: datetime


class TransferReport(StrictModel):
    state: Literal["saved", "failed"]


class UsageOut(BaseModel):
    used_bytes: int
    quota_bytes: int
    active_jobs: int
    max_active_jobs: int
    completed_retention_hours: int
    max_file_bytes: int
    supported_sites: list[str]
