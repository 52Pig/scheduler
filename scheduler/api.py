from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scheduler.config import Settings
from scheduler.models import DependencyUpdate, JobInput
from scheduler.service import SchedulerService

settings = Settings.from_env()
service = SchedulerService(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    yield
    service.shutdown()


app = FastAPI(title="Scheduler", version="0.1.0", lifespan=lifespan)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(static_dir / "index.html")


@app.get("/logs", include_in_schema=False)
def logs_page():
    return FileResponse(static_dir / "logs.html")


@app.get("/logs/{run_id}", include_in_schema=False)
def run_log_page(run_id: int):
    return FileResponse(static_dir / "logs.html")


@app.get("/api/health")
def health():
    details = service.health()
    healthy = (
        details["scheduler_running"] and details["database"] == "ok" and details["jobstore"] == "ok"
    )
    return {"status": "ok" if healthy else "degraded", **details}


@app.get("/api/jobs")
def list_jobs():
    return service.list()


@app.post("/api/jobs", status_code=201)
def create_job(payload: JobInput):
    try:
        return service.add(payload)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.put("/api/jobs/{job_id}")
def update_job(job_id: str, payload: JobInput):
    if job_id != payload.id:
        raise HTTPException(status_code=422, detail="job ID cannot be changed")
    if service.runs.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        return service.add(payload)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    try:
        service.remove(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Job not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.patch("/api/jobs/{job_id}/dependencies")
def update_dependencies(job_id: str, payload: DependencyUpdate):
    try:
        definition = service.runs.get_job(job_id)
        if definition is None:
            raise KeyError(job_id)
        definition["depends_on"] = payload.depends_on
        return service.add(JobInput.model_validate(definition))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Job not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/jobs/{job_id}/run", status_code=202)
def run_job(job_id: str, scope: Literal["self", "downstream"] = "downstream"):
    try:
        service.run_now(job_id, scope)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Job not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "queued", "scope": scope}


@app.get("/api/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    job_id: str | None = None,
    offset: int = Query(default=0, ge=0),
):
    return service.runs.list(limit, job_id, include_output=False, offset=offset)


@app.get("/api/runs/recent")
def recent_runs():
    return service.runs.recent_by_job(days=5)


@app.post("/api/runs/catch-up-today", status_code=202)
def catch_up_today():
    try:
        return service.queue_catch_up_today()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _run_date_bounds(run_date: str | None) -> tuple[str | None, str | None]:
    if not run_date:
        return None, None
    try:
        if len(run_date) != 10 or run_date[4] != "-" or run_date[7] != "-":
            raise ValueError("invalid date format")
        day = date.fromisoformat(run_date)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="date must use YYYY-MM-DD") from error
    try:
        zone = ZoneInfo(service.settings.timezone)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(status_code=500, detail="server timezone is invalid") from error
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()


@app.get("/api/runs/search")
def search_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    job_id: str | None = None,
    date_filter: str | None = Query(default=None, alias="date"),
    status: Literal["running", "succeeded", "failed", "timed_out", "skipped", "cancelled"]
    | None = None,
):
    started_after, started_before = _run_date_bounds(date_filter)
    filters = {
        "job_id": job_id,
        "started_after": started_after,
        "started_before": started_before,
        "status": status,
    }
    return {
        "items": service.runs.list(
            limit,
            include_output=False,
            offset=offset,
            **filters,
        ),
        "total": service.runs.count_runs(**filters),
        "retention_days": 5,
    }


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = service.runs.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.post("/api/runs/{run_id}/stop", status_code=202)
def stop_run(run_id: int):
    try:
        service.stop_run(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Run not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "stopping", "run_id": run_id}
