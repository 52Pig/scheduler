from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class JobInput(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    name: str = Field(min_length=1, max_length=160)
    command: list[str] | None = Field(default=None, min_length=1, max_length=64)
    depends_on: list[str] = Field(default_factory=list, max_length=500)
    schedule_type: Literal["interval", "cron", "date"] | None = None
    interval_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    cron: str | None = Field(default=None, max_length=160)
    run_at: str | None = None
    timezone: str | None = Field(default=None, max_length=64)
    working_directory: str | None = Field(default=None, max_length=1024)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)

    @field_validator("command")
    @classmethod
    def command_has_executable(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value[0].strip() or any("\x00" in part for part in value):
            raise ValueError("command must have a non-empty executable and no null bytes")
        return value

    @field_validator("depends_on")
    @classmethod
    def dependencies_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("depends_on entries must be unique")
        return value

    @field_validator("cron")
    @classmethod
    def cron_has_five_fields(cls, value: str | None) -> str | None:
        if value is not None and len(value.split()) != 5:
            raise ValueError("cron must contain five fields: minute hour day month weekday")
        return value


class JobView(BaseModel):
    id: str
    name: str
    command: list[str]
    schedule: str
    next_run_time: str | None
    timezone: str
    timeout_seconds: int


class RunView(BaseModel):
    id: int
    job_id: str
    started_at: str
    finished_at: str | None
    status: str
    exit_code: int | None
    output: str


class DependencyUpdate(BaseModel):
    depends_on: list[str] = Field(max_length=500)
