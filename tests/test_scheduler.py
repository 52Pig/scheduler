import os
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import scheduler.database as database_module
import scheduler.service as service_module
from scheduler.config import Settings
from scheduler.database import RunStore
from scheduler.models import JobInput
from scheduler.service import SchedulerService, execute_command, execute_workflow


def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 'scheduler.db'}", timezone="UTC")


def test_interval_job_persists_and_can_be_removed(tmp_path):
    config = settings(tmp_path)
    service = SchedulerService(config)
    service.start()
    service.add(
        JobInput(
            id="heartbeat",
            name="Heartbeat",
            command=[sys.executable, "-c", "pass"],
            schedule_type="interval",
            interval_seconds=3600,
        )
    )
    service.shutdown()

    restored = SchedulerService(config)
    restored.start()
    assert restored.get("heartbeat")["name"] == "Heartbeat"
    restored.remove("heartbeat")
    assert restored.list() == []
    restored.shutdown()


def test_startup_marks_abandoned_runs_failed_and_reports_health(tmp_path):
    config = settings(tmp_path)
    service = SchedulerService(config)
    run_id = service.runs.start("interrupted-job", "old-workflow")

    service.start()

    run = service.runs.get_run(run_id)
    assert run["status"] == "failed"
    assert run["exit_code"] == -1
    assert "service restarted" in run["output"]
    health = service.health()
    assert health["scheduler_running"] is True
    assert health["database"] == "ok"
    assert health["uptime_seconds"] >= 0
    service.shutdown()


def test_cron_requires_five_fields():
    with pytest.raises(ValueError, match="five fields"):
        JobInput(id="invalid", name="Invalid", command=["echo"], schedule_type="cron", cron="* * *")


def test_cron_numeric_weekdays_follow_standard_crontab_numbering(tmp_path):
    service = SchedulerService(settings(tmp_path))
    job = JobInput(
        id="weekdays",
        name="Weekdays",
        schedule_type="cron",
        cron="30 0 * * 1-5",
        timezone="Asia/Shanghai",
    )
    trigger = service._make_trigger(job)

    zone = ZoneInfo("Asia/Shanghai")
    monday = datetime(2026, 11, 23, 0, 29, tzinfo=zone)
    first = trigger.get_next_fire_time(None, monday)
    assert first == datetime(2026, 11, 23, 0, 30, tzinfo=zone)
    assert first.strftime("%A") == "Monday"

    after_saturday = datetime(2026, 11, 21, 0, 31, tzinfo=zone)
    next_weekday = trigger.get_next_fire_time(None, after_saturday)
    assert next_weekday == datetime(2026, 11, 23, 0, 30, tzinfo=zone)


@pytest.mark.parametrize("sunday", ["0", "7"])
def test_cron_weekday_zero_and_seven_mean_sunday(tmp_path, sunday):
    service = SchedulerService(settings(tmp_path))
    trigger = service._make_trigger(
        JobInput(
            id="sunday",
            name="Sunday",
            schedule_type="cron",
            cron=f"0 0 * * {sunday}",
            timezone="Asia/Shanghai",
        )
    )
    zone = ZoneInfo("Asia/Shanghai")
    after_saturday = datetime(2026, 11, 21, 0, 1, tzinfo=zone)
    assert trigger.get_next_fire_time(None, after_saturday) == datetime(
        2026, 11, 22, 0, 0, tzinfo=zone
    )


def test_catch_up_queues_only_due_cron_jobs_without_today_runs(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 11, 23, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(service_module, "datetime", FixedDateTime)
    monkeypatch.setattr(database_module, "datetime", FixedDateTime)
    config = Settings(
        database_url=f"sqlite:///{tmp_path / 'catch-up.db'}", timezone="Asia/Shanghai"
    )
    service = SchedulerService(config)
    service.start()
    service.add(
        JobInput(
            id="root",
            name="Root",
            schedule_type="cron",
            cron="30 0 * * 1-5",
            timezone="Asia/Shanghai",
        )
    )
    service.add(
        JobInput(
            id="missed",
            name="Missed",
            depends_on=["root"],
            schedule_type="cron",
            cron="30 16 * * 1-5",
            timezone="Asia/Shanghai",
        )
    )
    service.add(
        JobInput(
            id="already-ran",
            name="Already ran",
            depends_on=["root"],
            schedule_type="cron",
            cron="0 15 * * mon-fri",
            timezone="Asia/Shanghai",
        )
    )
    service.add(
        JobInput(
            id="future",
            name="Future",
            depends_on=["root"],
            schedule_type="cron",
            cron="30 21 * * mon-fri",
            timezone="Asia/Shanghai",
        )
    )
    completed_id = service.runs.start("already-ran", "today")
    service.runs.finish(completed_id, "succeeded", 0, "")

    queued = {}
    service.scheduler.pause()

    def capture_add_job(func, **kwargs):
        queued.update(function=func, **kwargs)

    monkeypatch.setattr(service.scheduler, "add_job", capture_add_job)
    result = service.queue_catch_up_today()

    assert result == {"status": "queued", "jobs": ["root", "missed"]}
    assert queued["id"] == "system-catch-up-2026-11-23"
    assert queued["kwargs"]["job_ids"] == ["root", "missed"]
    service_module._run_scheduled_catch_up(**queued["kwargs"])
    latest = {run["job_id"]: run for run in service.runs.list()}
    assert latest["root"]["status"] == "succeeded"
    assert latest["missed"]["status"] == "succeeded"
    assert latest["already-ran"]["status"] == "succeeded"
    assert "future" not in latest
    service.shutdown()
    with service_module._CATCH_UP_LOCK:
        service_module._CATCH_UP_DATES.discard("2026-11-23")


def test_command_execution_records_success_and_failure(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'history.db'}"
    execute_command("ok", [sys.executable, "-c", "print('done')"], None, 5, database_url)
    execute_command("bad", [sys.executable, "-c", "raise SystemExit(7)"], None, 5, database_url)
    runs = RunStore(database_url).list()
    assert [(run["job_id"], run["status"], run["exit_code"]) for run in runs] == [
        ("bad", "failed", 7),
        ("ok", "succeeded", 0),
    ]
    assert runs[1]["output"].strip() == "done"


def test_command_output_is_persisted_while_process_is_running(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'live-history.db'}"
    store = RunStore(database_url)
    worker = threading.Thread(
        target=execute_command,
        args=(
            "streaming",
            [sys.executable, "-c", "import time; print('first chunk', flush=True); time.sleep(1)"],
            None,
            5,
            database_url,
        ),
    )
    worker.start()

    deadline = time.monotonic() + 3
    run = None
    while time.monotonic() < deadline:
        runs = store.list()
        if runs and "first chunk" in runs[0]["output"]:
            run = runs[0]
            break
        time.sleep(0.02)

    assert run is not None
    assert run["status"] == "running"
    worker.join(timeout=5)
    assert not worker.is_alive()
    completed = store.get_run(run["id"])
    assert completed["status"] == "succeeded"
    assert "first chunk" in completed["output"]


def test_running_command_can_be_cancelled_by_run_id(tmp_path):
    config = settings(tmp_path)
    service = SchedulerService(config)
    worker = threading.Thread(
        target=execute_command,
        args=(
            "stoppable",
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "print(f'child:{child.pid}', flush=True); time.sleep(30)",
            ],
            None,
            60,
            config.database_url,
        ),
    )
    worker.start()

    deadline = time.monotonic() + 5
    run = None
    while time.monotonic() < deadline:
        runs = service.runs.list(job_id="stoppable")
        if runs and "child:" in runs[0]["output"]:
            run = runs[0]
            break
        time.sleep(0.02)

    assert run is not None
    assert run["status"] == "running"
    service.stop_run(run["id"])
    worker.join(timeout=5)

    assert not worker.is_alive()
    stopped = service.runs.get_run(run["id"])
    assert stopped["status"] == "cancelled"
    assert "Stopped by operator" in stopped["output"]
    child_pid = int(stopped["output"].split("child:", 1)[1].splitlines()[0])
    deadline = time.monotonic() + 5
    child_alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except OSError:
            child_alive = False
            break
        time.sleep(0.05)
    assert not child_alive


def test_stop_run_rejects_missing_or_completed_runs(tmp_path):
    service = SchedulerService(settings(tmp_path))
    completed_id = service.runs.start("completed")
    service.runs.finish(completed_id, "succeeded", 0, "done")

    with pytest.raises(KeyError):
        service.stop_run(completed_id + 1)
    with pytest.raises(ValueError, match="not running"):
        service.stop_run(completed_id)
    service.shutdown()


def test_cancelled_node_skips_its_downstream_dependents(tmp_path):
    config = settings(tmp_path)
    service = SchedulerService(config)
    service.add(JobInput(id="cancel-root", name="Root"))
    service.add(
        JobInput(
            id="cancel-child",
            name="Long child",
            command=[
                sys.executable,
                "-c",
                "import time; print('child started', flush=True); time.sleep(30)",
            ],
            depends_on=["cancel-root"],
            timeout_seconds=60,
        )
    )
    service.add(JobInput(id="cancel-leaf", name="Leaf", depends_on=["cancel-child"]))
    workflow = threading.Thread(
        target=execute_workflow,
        kwargs={"root_id": "cancel-root", "database_url": config.database_url, "max_workers": 2},
    )
    workflow.start()

    deadline = time.monotonic() + 5
    active_run = None
    while time.monotonic() < deadline:
        runs = service.runs.list(job_id="cancel-child")
        if runs and "child started" in runs[0]["output"]:
            active_run = runs[0]
            break
        time.sleep(0.02)

    assert active_run is not None
    service.stop_run(active_run["id"])
    workflow.join(timeout=5)

    assert not workflow.is_alive()
    latest = {run["job_id"]: run for run in service.runs.list()}
    assert latest["cancel-child"]["status"] == "cancelled"
    assert latest["cancel-leaf"]["status"] == "skipped"
    service.shutdown()


def test_date_schedule_rejects_missing_timestamp(tmp_path):
    service = SchedulerService(settings(tmp_path))
    with pytest.raises(ValueError, match="run_at is required"):
        service.add(JobInput(id="once", name="Once", command=["echo"], schedule_type="date"))
    service.shutdown()


def test_dependency_graph_runs_parallel_branches_and_skips_join_after_failure(tmp_path):
    config = settings(tmp_path)
    service = SchedulerService(config)
    service.start()
    service.add(
        JobInput(
            id="entry", name="Entry", command=None, schedule_type="interval", interval_seconds=3600
        )
    )
    service.add(
        JobInput(
            id="branch-a",
            name="Branch A",
            command=[sys.executable, "-c", "pass"],
            depends_on=["entry"],
            timeout_seconds=5,
        )
    )
    service.add(
        JobInput(
            id="branch-b",
            name="Branch B",
            command=[sys.executable, "-c", "raise SystemExit(4)"],
            depends_on=["entry"],
            timeout_seconds=5,
        )
    )
    service.add(JobInput(id="join", name="Join", command=None, depends_on=["branch-a", "branch-b"]))

    execute_workflow("entry", config.database_url, max_workers=2)
    runs = {run["job_id"]: run for run in service.runs.list()}
    assert runs["entry"]["status"] == "succeeded"
    assert runs["branch-a"]["status"] == "succeeded"
    assert runs["branch-b"]["status"] == "failed"
    assert runs["join"]["status"] == "skipped"
    assert runs["entry"]["workflow_run_id"] == runs["join"]["workflow_run_id"]
    service.shutdown()


def test_dependent_job_can_have_own_schedule_and_wait_for_scheduled_parent(tmp_path):
    config = settings(tmp_path)
    service = SchedulerService(config)
    service.start()
    service.add(
        JobInput(id="daily-root", name="Daily root", schedule_type="cron", cron="30 0 * * 1-5")
    )
    service.add(
        JobInput(
            id="after-close",
            name="After close",
            command=None,
            depends_on=["daily-root"],
            schedule_type="cron",
            cron="30 16 * * 1-5",
            timezone="Asia/Shanghai",
        )
    )
    service.add(
        JobInput(id="after-close-child", name="After close child", depends_on=["after-close"])
    )

    scheduled_child = service.scheduler.get_job(service._scheduler_id("after-close"))
    assert scheduled_child is not None
    assert service.get("after-close")["next_run_time"] is not None
    assert service.get("after-close")["is_scheduled"] is True

    execute_workflow("daily-root", config.database_url, scheduled=True, default_timezone="UTC")
    assert [run["job_id"] for run in service.runs.list()] == ["daily-root"]

    execute_workflow("after-close", config.database_url, scheduled=True, default_timezone="UTC")
    latest = {run["job_id"]: run for run in service.runs.list()}
    assert latest["after-close"]["status"] == "succeeded"
    assert latest["after-close-child"]["status"] == "succeeded"
    assert latest["daily-root"]["workflow_run_id"] != latest["after-close"]["workflow_run_id"]
    service.shutdown()


def test_manual_run_scope_runs_self_or_skips_join_outside_scope(tmp_path):
    config = settings(tmp_path)
    service = SchedulerService(config)
    service.add(
        JobInput(
            id="scope-root", name="Scope root", schedule_type="interval", interval_seconds=3600
        )
    )
    service.add(JobInput(id="scope-a", name="Scope A", depends_on=["scope-root"]))
    service.add(JobInput(id="scope-b", name="Scope B", depends_on=["scope-root"]))
    service.add(JobInput(id="scope-join", name="Scope join", depends_on=["scope-a", "scope-b"]))

    execute_workflow("scope-a", config.database_url, scope="self")
    latest = {run["job_id"]: run for run in service.runs.list()}
    assert latest["scope-a"]["status"] == "succeeded"
    assert "scope-join" not in latest

    execute_workflow("scope-a", config.database_url, scope="downstream")
    latest = {run["job_id"]: run for run in service.runs.list()}
    assert latest["scope-join"]["status"] == "skipped"
    assert "outside the selected run scope" in latest["scope-join"]["output"]
    service.shutdown()


def test_run_now_accepts_any_job_and_requested_scope(tmp_path, monkeypatch):
    service = SchedulerService(settings(tmp_path))
    service.add(
        JobInput(
            id="manual-root", name="Manual root", schedule_type="interval", interval_seconds=3600
        )
    )
    service.add(JobInput(id="manual-child", name="Manual child", depends_on=["manual-root"]))
    scheduled = {}

    def capture_job(func, **kwargs):
        scheduled.update(func=func, **kwargs)

    monkeypatch.setattr(service.scheduler, "add_job", capture_job)
    service.run_now("manual-child", scope="self")

    assert scheduled["func"] is execute_workflow
    assert scheduled["kwargs"]["root_id"] == "manual-child"
    assert scheduled["kwargs"]["scope"] == "self"
    service.shutdown()


def test_dependency_graph_rejects_cycles_missing_parents_and_cross_root_join(tmp_path):
    service = SchedulerService(settings(tmp_path))
    service.add(JobInput(id="root-a", name="A", schedule_type="interval", interval_seconds=600))
    service.add(JobInput(id="root-b", name="B", schedule_type="interval", interval_seconds=600))
    with pytest.raises(ValueError, match="does not exist"):
        service.add(JobInput(id="orphan", name="Orphan", depends_on=["missing"]))
    service.add(JobInput(id="child-a", name="Child A", depends_on=["root-a"]))
    with pytest.raises(ValueError, match="same workflow root"):
        service.add(JobInput(id="join", name="Join", depends_on=["child-a", "root-b"]))
    with pytest.raises(ValueError, match="cycle"):
        service.add(
            JobInput(
                id="root-a",
                name="A",
                depends_on=["child-a"],
                schedule_type="interval",
                interval_seconds=600,
            )
        )
    service.shutdown()


def test_legacy_scheduled_jobs_are_migrated_on_start(tmp_path):
    config = settings(tmp_path)
    service = SchedulerService(config)
    service.scheduler.add_job(
        execute_command,
        trigger="interval",
        seconds=600,
        id="legacy-task",
        name="Legacy task",
        kwargs={
            "job_id": "legacy-task",
            "command": [sys.executable, "-c", "pass"],
            "working_directory": None,
            "timeout_seconds": 30,
            "database_url": config.database_url,
        },
    )
    service.start()
    migrated = service.get("legacy-task")
    assert migrated["name"] == "Legacy task"
    assert migrated["command"] == [sys.executable, "-c", "pass"]
    assert service.scheduler.get_job("legacy-task") is None
    assert service.scheduler.get_job("job-legacy-task") is not None
    service.remove("legacy-task")
    service.shutdown()
