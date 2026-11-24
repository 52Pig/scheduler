from __future__ import annotations

import codecs
import locale
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, wait
from concurrent.futures import ThreadPoolExecutor as WorkerPool
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from scheduler.config import Settings
from scheduler.database import RunStore, make_engine
from scheduler.models import JobInput

_EXECUTION_SLOTS: dict[str, threading.BoundedSemaphore] = {}
_ACTIVE_RUNS: dict[tuple[str, int], _RunControl] = {}
_ACTIVE_RUNS_LOCK = threading.Lock()
_CATCH_UP_DATES: set[str] = set()
_CATCH_UP_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)


class _RunControl:
    def __init__(self) -> None:
        self.stop_requested = threading.Event()
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.finished = False

    def attach_process(self, process: subprocess.Popen) -> None:
        with self.lock:
            self.process = process
        if self.stop_requested.is_set():
            _terminate_process_tree(process)

    def request_stop(self) -> bool:
        with self.lock:
            if self.finished:
                return False
            self.stop_requested.set()
            process = self.process
        if process is not None:
            _terminate_process_tree(process)
        return True

    def finish(
        self,
        store: RunStore,
        run_id: int,
        status: str,
        exit_code: int | None,
        output: str,
    ) -> str:
        with self.lock:
            if self.stop_requested.is_set() and status != "cancelled":
                status = "cancelled"
                if "Stopped by operator" not in output:
                    output += "\nStopped by operator."
            store.finish(run_id, status, exit_code, output)
            self.finished = True
        return status


def _register_run(database_url: str, run_id: int) -> _RunControl:
    control = _RunControl()
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS[(database_url, run_id)] = control
    return control


def _unregister_run(database_url: str, run_id: int) -> None:
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS.pop((database_url, run_id), None)


def _request_run_stop(database_url: str, run_id: int) -> bool:
    with _ACTIVE_RUNS_LOCK:
        control = _ACTIVE_RUNS.get((database_url, run_id))
    if control is None:
        return False
    return control.request_stop()


class SchedulerService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.started_at: datetime | None = None
        _EXECUTION_SLOTS[settings.database_url] = threading.BoundedSemaphore(settings.max_workers)
        self.runs = RunStore(settings.database_url)
        self.scheduler = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(engine=make_engine(settings.database_url))},
            executors={"default": ThreadPoolExecutor(settings.max_workers)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
            timezone=settings.timezone,
        )

    def start(self) -> None:
        if not self.scheduler.running:
            self.started_at = datetime.now(timezone.utc)
            self.runs.recover_interrupted_runs()
            # Do not fire persisted triggers before job definitions are rebuilt.
            self.scheduler.start(paused=True)
            self._migrate_legacy_jobs()
            self._restore_job_schedules()
            self.runs.prune_runs()
            self.scheduler.add_job(
                prune_run_history,
                trigger=IntervalTrigger(hours=1, timezone=self.settings.timezone),
                id="system-run-retention",
                kwargs={"database_url": self.settings.database_url},
                replace_existing=True,
            )
            self.scheduler.resume()

    def health(self) -> dict[str, Any]:
        database_ok = False
        jobstore_ok = False
        scheduled_jobs = 0
        try:
            self.runs.check()
            database_ok = True
            if self.scheduler.running:
                scheduled_jobs = len(self.scheduler.get_jobs())
                jobstore_ok = True
        except Exception:
            pass
        scheduler_running = self.scheduler.running
        uptime_seconds = (
            max(0, int((datetime.now(timezone.utc) - self.started_at).total_seconds()))
            if self.started_at
            else 0
        )
        return {
            "scheduler_running": scheduler_running,
            "database": "ok" if database_ok else "unavailable",
            "jobstore": "ok" if jobstore_ok else "unavailable",
            "scheduled_jobs": scheduled_jobs,
            "uptime_seconds": uptime_seconds,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }

    def _restore_job_schedules(self) -> None:
        for definition in self.runs.list_jobs():
            self.add(JobInput.model_validate(definition))

    def _migrate_legacy_jobs(self) -> None:
        for job in self.scheduler.get_jobs():
            if self.runs.get_job(job.id) is not None or not isinstance(job.kwargs, dict):
                continue
            if "command" not in job.kwargs or "job_id" not in job.kwargs:
                continue
            trigger = job.trigger
            common = {
                "id": job.id,
                "name": job.name or job.id,
                "command": job.kwargs["command"],
                "working_directory": job.kwargs.get("working_directory"),
                "timeout_seconds": job.kwargs.get("timeout_seconds", self.settings.default_timeout),
                "timezone": str(trigger.timezone),
            }
            if isinstance(trigger, IntervalTrigger):
                data = JobInput(
                    **common,
                    schedule_type="interval",
                    interval_seconds=max(1, int(trigger.interval.total_seconds())),
                )
            elif isinstance(trigger, CronTrigger):
                fields = {field.name: str(field) for field in trigger.fields}
                cron = " ".join(
                    fields[name] for name in ("minute", "hour", "day", "month", "day_of_week")
                )
                data = JobInput(**common, schedule_type="cron", cron=cron)
            elif isinstance(trigger, DateTrigger):
                data = JobInput(
                    **common,
                    schedule_type="date",
                    run_at=trigger.run_date.isoformat(),
                )
            else:
                continue
            self.add(data)
            try:
                self.scheduler.remove_job(job.id)
            except JobLookupError:
                pass

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def add(self, data: JobInput) -> dict[str, Any]:
        candidate = data.model_dump()
        self._validate_graph(candidate)
        trigger = self._make_trigger(data) if data.schedule_type is not None else None
        self.runs.save_job(candidate)
        scheduler_id = self._scheduler_id(data.id)
        if trigger is None:
            try:
                self.scheduler.remove_job(scheduler_id)
            except JobLookupError:
                pass
        else:
            self.scheduler.add_job(
                execute_workflow,
                trigger=trigger,
                id=scheduler_id,
                name=data.name,
                replace_existing=True,
                kwargs={
                    "root_id": data.id,
                    "database_url": self.settings.database_url,
                    "max_workers": self.settings.max_workers,
                    "scheduled": True,
                    "default_timezone": self.settings.timezone,
                },
                misfire_grace_time=3600,
            )
        return self.get(data.id)

    @staticmethod
    def _scheduler_id(job_id: str) -> str:
        return f"job-{job_id}"

    def _make_trigger(self, data: JobInput):
        try:
            zone = ZoneInfo(data.timezone or self.settings.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown timezone: {data.timezone}") from error
        if data.schedule_type == "interval":
            if data.interval_seconds is None:
                raise ValueError("interval_seconds is required for interval schedules")
            return IntervalTrigger(seconds=data.interval_seconds, timezone=zone)
        if data.schedule_type == "cron":
            if not data.cron:
                raise ValueError("cron is required for cron schedules")
            fields = data.cron.split()
            fields[4] = _apscheduler_weekday_field(fields[4])
            return CronTrigger.from_crontab(" ".join(fields), timezone=zone)
        if data.schedule_type == "date":
            if not data.run_at:
                raise ValueError("run_at is required for date schedules")
            run_at = datetime.fromisoformat(data.run_at.replace("Z", "+00:00"))
            return DateTrigger(run_date=run_at, timezone=zone)
        raise ValueError("scheduled jobs require an interval, cron, or date schedule")

    def queue_catch_up_today(self) -> dict[str, Any]:
        if not self.scheduler.running:
            raise ValueError("scheduler is not running")

        now = datetime.now(timezone.utc)
        definitions = {job["id"]: job for job in self.runs.list_jobs()}
        candidates: list[tuple[int, datetime, str]] = []

        def depth(job_id: str) -> int:
            parents = [
                parent_id
                for parent_id in definitions[job_id]["depends_on"]
                if parent_id in definitions
            ]
            return 0 if not parents else 1 + max(depth(parent_id) for parent_id in parents)

        for job_id, definition in definitions.items():
            if definition["schedule_type"] != "cron":
                continue
            scheduled_job = self.scheduler.get_job(self._scheduler_id(job_id))
            if scheduled_job is None or not isinstance(scheduled_job.trigger, CronTrigger):
                continue

            try:
                zone = ZoneInfo(definition.get("timezone") or self.settings.timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError(
                    f"unknown timezone for {job_id}: {definition.get('timezone')}"
                ) from error
            local_now = now.astimezone(zone)
            local_start = datetime.combine(local_now.date(), datetime.min.time(), zone)
            next_fire = scheduled_job.trigger.get_next_fire_time(
                None, local_start - timedelta(microseconds=1)
            )
            last_due = None
            for _ in range(1500):
                if next_fire is None or next_fire > local_now:
                    break
                last_due = next_fire
                next_fire = scheduled_job.trigger.get_next_fire_time(next_fire, next_fire)
            else:
                raise ValueError(f"too many cron occurrences to inspect for {job_id}")

            if last_due is None:
                continue
            day_start, day_end = _same_day_window(definition, self.settings.timezone)
            if self.runs.latest_status_between(job_id, day_start, day_end) is not None:
                continue
            candidates.append((depth(job_id), last_due, job_id))

        candidates.sort()
        job_ids = [job_id for _, _, job_id in candidates]
        local_day = now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        catch_up_id = f"system-catch-up-{local_day}"
        if not job_ids:
            return {"status": "no_missing_runs", "jobs": []}
        with _CATCH_UP_LOCK:
            if self.scheduler.get_job(catch_up_id) is not None or local_day in _CATCH_UP_DATES:
                return {"status": "already_queued", "jobs": job_ids}
            self.scheduler.add_job(
                _run_scheduled_catch_up,
                trigger=DateTrigger(run_date=now + timedelta(seconds=1), timezone=timezone.utc),
                id=catch_up_id,
                kwargs={
                    "job_ids": job_ids,
                    "database_url": self.settings.database_url,
                    "max_workers": self.settings.max_workers,
                    "default_timezone": self.settings.timezone,
                    "catch_up_day": local_day,
                },
                misfire_grace_time=60,
            )
            _CATCH_UP_DATES.add(local_day)
        return {"status": "queued", "jobs": job_ids}

    def _validate_graph(self, candidate: dict) -> None:
        jobs = {job["id"]: job for job in self.runs.list_jobs()}
        jobs[candidate["id"]] = candidate
        for parent_id in candidate["depends_on"]:
            if parent_id == candidate["id"]:
                raise ValueError("a job cannot depend on itself")
            if parent_id not in jobs:
                raise ValueError(f"dependency does not exist: {parent_id}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(job_id: str) -> None:
            if job_id in visiting:
                raise ValueError("dependency graph cannot contain a cycle")
            if job_id in visited:
                return
            visiting.add(job_id)
            for parent_id in jobs[job_id]["depends_on"]:
                if parent_id in jobs:
                    visit(parent_id)
            visiting.remove(job_id)
            visited.add(job_id)

        for job_id in jobs:
            visit(job_id)

        if candidate["depends_on"]:

            def roots(job_id: str, seen: set[str] | None = None) -> set[str]:
                seen = seen or set()
                if job_id in seen:
                    return set()
                seen.add(job_id)
                parents = jobs[job_id]["depends_on"]
                if not parents:
                    return {job_id}
                result: set[str] = set()
                for parent_id in parents:
                    result.update(roots(parent_id, seen))
                return result

            root_ids: set[str] = set()
            for parent_id in candidate["depends_on"]:
                root_ids.update(roots(parent_id))
            if len(root_ids) != 1:
                raise ValueError("all dependencies must belong to the same workflow root")

    def get(self, job_id: str) -> dict[str, Any]:
        definition = self.runs.get_job(job_id)
        if definition is None:
            raise KeyError(job_id)
        job = self.scheduler.get_job(self._scheduler_id(job_id))
        is_root = not definition["depends_on"]
        is_scheduled = definition["schedule_type"] is not None
        if definition["schedule_type"] == "interval":
            schedule = f"Every {definition['interval_seconds']} seconds"
        elif definition["schedule_type"] == "cron":
            schedule = f"Cron {definition['cron']}"
        elif definition["schedule_type"] == "date":
            schedule = f"Once at {definition['run_at']}"
        else:
            schedule = "When dependencies succeed" if definition["depends_on"] else "Manual only"
        next_run_time = (
            job.next_run_time.isoformat() if job and getattr(job, "next_run_time", None) else None
        )
        return {
            **definition,
            **(self.runs.job_metadata(job_id) or {}),
            "schedule": schedule,
            "next_run_time": next_run_time,
            "timezone": definition["timezone"] or self.settings.timezone,
            "timeout_seconds": definition["timeout_seconds"],
            "is_root": is_root,
            "is_scheduled": is_scheduled,
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.get(job["id"]) for job in self.runs.list_jobs()]

    def remove(self, job_id: str) -> None:
        if self.runs.get_job(job_id) is None:
            raise KeyError(job_id)
        children = [job["id"] for job in self.runs.list_jobs() if job_id in job["depends_on"]]
        if children:
            raise ValueError(f"remove dependent jobs first: {', '.join(children)}")
        try:
            self.scheduler.remove_job(self._scheduler_id(job_id))
        except JobLookupError:
            pass
        self.runs.delete_job(job_id)

    def run_now(self, job_id: str, scope: str = "downstream") -> None:
        definition = self.runs.get_job(job_id)
        if definition is None:
            raise KeyError(job_id)
        if scope not in {"self", "downstream"}:
            raise ValueError("scope must be 'self' or 'downstream'")
        self.scheduler.add_job(
            execute_workflow,
            id=f"manual-{job_id}-{uuid.uuid4()}",
            kwargs={
                "root_id": job_id,
                "database_url": self.settings.database_url,
                "max_workers": self.settings.max_workers,
                "scope": scope,
                "scheduled": False,
                "default_timezone": self.settings.timezone,
            },
            misfire_grace_time=60,
        )

    def stop_run(self, run_id: int) -> None:
        run = self.runs.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] != "running":
            raise ValueError("run is not running")
        if not _request_run_stop(self.settings.database_url, run_id):
            raise ValueError("run is not active in this scheduler process")


def _same_day_window(job: dict, default_timezone: str) -> tuple[str, str]:
    timezone_name = job.get("timezone") or default_timezone
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown timezone: {timezone_name}") from error
    now = datetime.now(zone)
    local_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).isoformat(),
        local_end.astimezone(timezone.utc).isoformat(),
    )


def _apscheduler_weekday_field(field: str) -> str:
    """Translate standard crontab weekdays (Sunday=0/7) to APScheduler names."""
    names = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
    translated = []
    for part in field.split(","):
        base, separator, raw_step = part.partition("/")
        step = int(raw_step) if separator else 1
        if step < 1:
            raise ValueError("cron weekday step must be positive")
        if base == "*":
            values = range(0, 7, step) if separator else None
        elif "-" in base:
            start, end = base.split("-", 1)
            if start.isdecimal() and end.isdecimal():
                first, last = int(start), int(end)
                if not 0 <= first <= last <= 7:
                    raise ValueError("cron weekday numbers must be between 0 and 7")
                values = range(first, last + 1, step)
            else:
                values = None
        elif base.isdecimal():
            first = int(base)
            if not 0 <= first <= 7:
                raise ValueError("cron weekday numbers must be between 0 and 7")
            values = range(first % 7, 7, step) if separator else (first,)
        else:
            values = None

        if values is None:
            translated.append(part)
        else:
            translated.extend(names[value % 7] for value in values)
    return ",".join(translated)


def execute_workflow(
    root_id: str,
    database_url: str,
    max_workers: int = 10,
    scope: str = "downstream",
    scheduled: bool = False,
    default_timezone: str = "UTC",
) -> None:
    store = RunStore(database_url)
    definitions = {job["id"]: job for job in store.list_jobs()}
    if root_id not in definitions:
        return
    if scope not in {"self", "downstream"}:
        raise ValueError("scope must be 'self' or 'downstream'")
    children: dict[str, list[str]] = {job_id: [] for job_id in definitions}
    for job in definitions.values():
        for parent_id in job["depends_on"]:
            if parent_id in children:
                children[parent_id].append(job["id"])
    reachable: set[str] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        if scope == "downstream":
            stack.extend(
                child_id
                for child_id in children[current]
                if not scheduled or definitions[child_id]["schedule_type"] is None
            )

    workflow_run_id = str(uuid.uuid4())
    states: dict[str, str] = {}
    pending = set(reachable)
    revisions = {
        job_id: (store.job_metadata(job_id) or {}).get("revision", 1) for job_id in reachable
    }
    ancestors: set[str] = set()
    to_visit = list(definitions[root_id]["depends_on"])
    while to_visit:
        parent_id = to_visit.pop()
        if parent_id in ancestors or parent_id not in definitions:
            continue
        ancestors.add(parent_id)
        to_visit.extend(definitions[parent_id]["depends_on"])
    if scheduled and definitions[root_id]["depends_on"]:
        failed = []
        for parent_id in ancestors:
            bounds = _same_day_window(definitions[parent_id], default_timezone)
            if store.latest_status_between(parent_id, *bounds) != "succeeded":
                failed.append(parent_id)
        if failed:
            run_id = store.start(root_id, workflow_run_id, revisions[root_id])
            store.finish(
                run_id,
                "skipped",
                None,
                "Scheduled run skipped; dependencies have no successful run today: "
                + ", ".join(sorted(failed)),
            )
            return
    with WorkerPool(max_workers=max_workers) as workers:
        futures = {}
        while pending or futures:
            for job_id in tuple(pending):
                parents = [
                    parent_id
                    for parent_id in definitions[job_id]["depends_on"]
                    if parent_id in reachable
                ]
                unselected_parent = job_id != root_id and any(
                    parent_id not in reachable and not (scheduled and parent_id in ancestors)
                    for parent_id in definitions[job_id]["depends_on"]
                )
                failed_parent = any(
                    states.get(parent) in {"failed", "timed_out", "skipped", "cancelled"}
                    for parent in parents
                )
                if failed_parent or unselected_parent:
                    run_id = store.start(job_id, workflow_run_id, revisions[job_id])
                    reason = (
                        "An upstream dependency was outside the selected run scope"
                        if unselected_parent
                        else "An upstream dependency did not succeed"
                    )
                    store.finish(run_id, "skipped", None, reason)
                    states[job_id] = "skipped"
                    pending.remove(job_id)
                elif all(states.get(parent) == "succeeded" for parent in parents):
                    pending.remove(job_id)
                    futures[
                        workers.submit(
                            execute_node,
                            definitions[job_id],
                            database_url,
                            workflow_run_id,
                            revisions[job_id],
                        )
                    ] = job_id
            if futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    job_id = futures.pop(future)
                    try:
                        states[job_id] = future.result()
                    except Exception:
                        states[job_id] = "failed"
            elif pending:
                break


def _run_scheduled_catch_up(
    job_ids: list[str],
    database_url: str,
    max_workers: int,
    default_timezone: str,
    catch_up_day: str,
) -> None:
    try:
        zone = ZoneInfo(default_timezone)
        if datetime.now(zone).date().isoformat() != catch_up_day:
            _LOGGER.info("Discarding stale catch-up request for %s", catch_up_day)
            return
        store = RunStore(database_url)
        definitions = {job["id"]: job for job in store.list_jobs()}
        for job_id in job_ids:
            definition = definitions.get(job_id)
            if definition is None:
                continue
            day_start, day_end = _same_day_window(definition, default_timezone)
            if store.latest_status_between(job_id, day_start, day_end) is not None:
                continue
            _LOGGER.info("Catching up missed scheduled run for %s on %s", job_id, catch_up_day)
            try:
                execute_workflow(
                    job_id,
                    database_url,
                    max_workers=max_workers,
                    scheduled=True,
                    default_timezone=default_timezone,
                )
            except Exception:
                _LOGGER.exception("Catch-up failed before a run record was written for %s", job_id)
    finally:
        with _CATCH_UP_LOCK:
            _CATCH_UP_DATES.discard(catch_up_day)


def execute_node(
    definition: dict, database_url: str, workflow_run_id: str, revision: int = 1
) -> str:
    store = RunStore(database_url)
    run_id = store.start(definition["id"], workflow_run_id, revision)
    control = _register_run(database_url, run_id)
    command = definition["command"]
    try:
        if control.stop_requested.is_set():
            return control.finish(
                store, run_id, "cancelled", None, "Stopped by operator before execution."
            )
        if not command:
            return control.finish(store, run_id, "succeeded", 0, "Logical node completed")
        slot = _EXECUTION_SLOTS.get(database_url) or threading.BoundedSemaphore(10)
        if not _acquire_execution_slot(slot, control):
            return control.finish(
                store, run_id, "cancelled", None, "Stopped by operator before execution."
            )
        try:
            if control.stop_requested.is_set():
                return control.finish(
                    store, run_id, "cancelled", None, "Stopped by operator before execution."
                )
            return_code, timed_out, cancelled = _run_streaming_command(
                command,
                definition["working_directory"],
                definition["timeout_seconds"],
                store,
                run_id,
                control,
            )
        finally:
            slot.release()
        if cancelled:
            return control.finish(store, run_id, "cancelled", return_code, "\nStopped by operator.")
        if timed_out:
            return control.finish(store, run_id, "timed_out", None, "\nCommand timed out.")
        status = "succeeded" if return_code == 0 else "failed"
        return control.finish(store, run_id, status, return_code, "")
    except Exception as error:
        status = "cancelled" if control.stop_requested.is_set() else "failed"
        message = "\nStopped by operator." if status == "cancelled" else str(error)
        return control.finish(store, run_id, status, None, message)
    finally:
        _unregister_run(database_url, run_id)


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _acquire_execution_slot(slot: threading.BoundedSemaphore, control: _RunControl) -> bool:
    while not control.stop_requested.is_set():
        if slot.acquire(timeout=0.1):
            return True
    return False


def _run_streaming_command(
    command: list[str],
    working_directory: str | None,
    timeout_seconds: int,
    store: RunStore,
    run_id: int,
    control: _RunControl,
) -> tuple[int, bool, bool]:
    process_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        command,
        cwd=working_directory,
        env={**os.environ, "PYTHONUNBUFFERED": os.environ.get("PYTHONUNBUFFERED", "1")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        **process_options,
    )
    control.attach_process(process)
    reader_errors: list[Exception] = []

    def stream_output() -> None:
        decoder = codecs.getincrementaldecoder(locale.getpreferredencoding(False))(errors="replace")
        try:
            while chunk := process.stdout.read1(4096):
                text = decoder.decode(chunk)
                if text:
                    try:
                        store.append_output(run_id, text)
                    except Exception as error:
                        reader_errors.append(error)
            tail = decoder.decode(b"", final=True)
            if tail:
                store.append_output(run_id, tail)
        except Exception as error:
            reader_errors.append(error)

    reader = threading.Thread(target=stream_output, name=f"run-output-{run_id}", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    cancelled = False
    while True:
        if control.stop_requested.is_set():
            _terminate_process_tree(process)
            return_code = process.wait()
            cancelled = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_tree(process)
            return_code = process.wait()
            timed_out = True
            break
        try:
            return_code = process.wait(timeout=min(0.1, remaining))
            cancelled = control.stop_requested.is_set()
            break
        except subprocess.TimeoutExpired:
            continue
    reader.join()
    if reader_errors:
        raise RuntimeError(f"failed to persist task output: {reader_errors[0]}") from reader_errors[
            0
        ]
    return return_code, timed_out, cancelled


def execute_command(
    job_id: str,
    command: list[str],
    working_directory: str | None,
    timeout_seconds: int,
    database_url: str,
) -> None:
    store = RunStore(database_url)
    run_id = store.start(job_id)
    control = _register_run(database_url, run_id)
    try:
        return_code, timed_out, cancelled = _run_streaming_command(
            command, working_directory, timeout_seconds, store, run_id, control
        )
        status = (
            "cancelled"
            if cancelled
            else "timed_out"
            if timed_out
            else "succeeded"
            if return_code == 0
            else "failed"
        )
        output = (
            "\nStopped by operator." if cancelled else "\nCommand timed out." if timed_out else ""
        )
        control.finish(store, run_id, status, None if timed_out else return_code, output)
    except Exception as error:
        status = "cancelled" if control.stop_requested.is_set() else "failed"
        message = "\nStopped by operator." if status == "cancelled" else str(error)
        control.finish(store, run_id, status, None, message)
    finally:
        _unregister_run(database_url, run_id)


def prune_run_history(database_url: str) -> None:
    RunStore(database_url).prune_runs()
