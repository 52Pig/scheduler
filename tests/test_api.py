import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from scheduler import api
from scheduler.config import Settings
from scheduler.models import JobInput
from scheduler.service import SchedulerService, execute_command


def test_job_api_create_list_remove_and_validate(tmp_path, monkeypatch):
    service = SchedulerService(
        Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}", timezone="UTC")
    )
    monkeypatch.setattr(api, "service", service)
    payload = {
        "id": "api-check",
        "name": "API check",
        "command": None,
        "schedule_type": "interval",
        "interval_seconds": 600,
    }

    with TestClient(api.app) as client:
        assert client.get("/api/health").json()["scheduler_running"] is True
        assert client.post("/api/jobs", json=payload).status_code == 201
        assert client.get("/api/jobs").json()[0]["id"] == "api-check"
        changed = {**payload, "name": "Renamed API check"}
        update_response = client.put("/api/jobs/api-check", json=changed)
        assert update_response.status_code == 200
        assert update_response.json()["name"] == "Renamed API check"
        assert update_response.json()["revision"] == 2
        child = {
            "id": "api-child",
            "name": "API child",
            "command": None,
            "depends_on": ["api-check"],
            "timeout_seconds": 300,
        }
        assert client.post("/api/jobs", json=child).status_code == 201
        assert (
            client.patch(
                "/api/jobs/api-child/dependencies", json={"depends_on": ["api-check"]}
            ).status_code
            == 200
        )
        assert (
            client.patch(
                "/api/jobs/api-child/dependencies", json={"depends_on": ["missing"]}
            ).status_code
            == 422
        )
        assert client.delete("/api/jobs/api-check").status_code == 409
        assert client.delete("/api/jobs/api-child").status_code == 204
        run_id = service.runs.start("api-check", "workflow-test", 2)
        service.runs.finish(run_id, "failed", 7, "full diagnostic output")
        recent = client.get("/api/runs/recent").json()
        assert [run["id"] for run in recent["api-check"]] == [run_id]
        assert recent["api-check"][0]["status"] == "failed"
        assert "output" not in recent["api-check"][0]
        run_detail = client.get(f"/api/runs/{run_id}")
        assert run_detail.status_code == 200
        assert run_detail.json()["output"] == "full diagnostic output"
        assert run_detail.json()["definition_revision"] == 2
        run_list = client.get("/api/runs?job_id=api-check").json()
        assert run_list[0]["job_id"] == "api-check"
        assert "output" not in run_list[0]

        streaming_run = service.runs.start("api-check", "streaming-workflow", 2)
        service.runs.append_output(streaming_run, "partial output")
        live_detail = client.get(f"/api/runs/{streaming_run}")
        assert live_detail.json()["status"] == "running"
        assert live_detail.json()["output"] == "partial output"
        service.runs.finish(streaming_run, "succeeded", 0, "")

        assert client.delete("/api/jobs/api-check").status_code == 204
        assert client.delete("/api/jobs/api-check").status_code == 404
        invalid = {**payload, "id": "bad id"}
        assert client.post("/api/jobs", json=invalid).status_code == 422


def test_manual_run_api_accepts_scope_for_any_job(tmp_path, monkeypatch):
    service = SchedulerService(
        Settings(database_url=f"sqlite:///{tmp_path / 'manual-api.db'}", timezone="UTC")
    )
    monkeypatch.setattr(api, "service", service)
    service.add(JobInput(id="root", name="Root", schedule_type="interval", interval_seconds=600))
    service.add(JobInput(id="child", name="Child", depends_on=["root"]))
    calls = []
    monkeypatch.setattr(service, "run_now", lambda job_id, scope: calls.append((job_id, scope)))

    with TestClient(api.app) as client:
        assert client.post("/api/jobs/child/run?scope=self").json() == {
            "status": "queued",
            "scope": "self",
        }
        assert client.post("/api/jobs/root/run?scope=downstream").json() == {
            "status": "queued",
            "scope": "downstream",
        }
        assert client.post("/api/jobs/child/run?scope=invalid").status_code == 422
    assert calls == [("child", "self"), ("root", "downstream")]


def test_stop_run_api_terminates_active_command(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'stop-api.db'}"
    service = SchedulerService(Settings(database_url=database_url, timezone="UTC"))
    monkeypatch.setattr(api, "service", service)
    worker = threading.Thread(
        target=execute_command,
        args=(
            "api-stoppable",
            [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
            None,
            60,
            database_url,
        ),
    )

    with TestClient(api.app) as client:
        worker.start()
        deadline = time.monotonic() + 5
        run = None
        while time.monotonic() < deadline:
            runs = service.runs.list(job_id="api-stoppable")
            if runs and "started" in runs[0]["output"]:
                run = runs[0]
                break
            time.sleep(0.02)

        assert run is not None
        response = client.post(f"/api/runs/{run['id']}/stop")
        assert response.status_code == 202
        assert response.json() == {"status": "stopping", "run_id": run["id"]}
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert client.get(f"/api/runs/{run['id']}").json()["status"] == "cancelled"
        cancelled = client.get(
            "/api/runs/search", params={"job_id": "api-stoppable", "status": "cancelled"}
        )
        assert cancelled.status_code == 200
        assert [item["id"] for item in cancelled.json()["items"]] == [run["id"]]

        assert client.post(f"/api/runs/{run['id']}/stop").status_code == 409
        assert client.post("/api/runs/999999/stop").status_code == 404


def test_log_search_filters_and_five_day_retention(tmp_path, monkeypatch):
    service = SchedulerService(
        Settings(database_url=f"sqlite:///{tmp_path / 'logs.db'}", timezone="Asia/Shanghai")
    )
    monkeypatch.setattr(api, "service", service)
    with TestClient(api.app) as client:
        assert client.get("/logs").status_code == 200
        assert client.get("/logs/123").status_code == 200
        recent = service.runs.start("node-a", "flow-1")
        service.runs.finish(recent, "failed", 9, "diagnostic")
        other = service.runs.start("node-b", "flow-2")
        service.runs.finish(other, "succeeded", 0, "done")
        old = service.runs.start("node-a", "flow-old")
        service.runs.finish(old, "failed", 9, "old diagnostic")
        old_time = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
        with service.runs._connect() as db:
            db.execute("UPDATE runs SET started_at=? WHERE id=?", (old_time, old))
            db.commit()

        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        response = client.get(
            "/api/runs/search",
            params={"job_id": "node-a", "status": "failed", "date": today},
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert [item["id"] for item in response.json()["items"]] == [recent]
        assert "output" not in response.json()["items"][0]
        assert response.json()["retention_days"] == 5
        assert client.get("/api/runs/search", params={"date": "bad"}).status_code == 422
        assert client.get("/api/runs/search", params={"offset": 1}).json()["total"] == 2
        assert client.get(f"/api/runs/{old}").status_code == 404
        assert client.get(f"/api/runs/{recent}").json()["output"] == "diagnostic"
