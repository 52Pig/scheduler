from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import uvicorn

from scheduler.config import Settings
from scheduler.models import JobInput


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="scheduler", description="Manage scheduled command tasks")
    commands = root.add_subparsers(dest="action", required=True)
    commands.add_parser("serve", help="start the web UI and scheduler")
    add = commands.add_parser("add", help="register a scheduled command")
    add.add_argument("--id", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments after --")
    schedule = add.add_mutually_exclusive_group()
    schedule.add_argument("--every", type=int, help="interval in seconds")
    schedule.add_argument(
        "--cron", help='five standard crontab fields, e.g. "0 9 * * 1-5" (Monday-Friday)'
    )
    schedule.add_argument("--at", dest="run_at", help="ISO 8601 timestamp")
    add.add_argument("--timezone")
    add.add_argument("--cwd", dest="working_directory")
    add.add_argument("--timeout", type=int, default=300)
    add.add_argument("--depends-on", action="append", default=[], metavar="JOB_ID")
    commands.add_parser("list", help="list registered jobs")
    remove = commands.add_parser("remove", help="remove a job")
    remove.add_argument("id")
    run = commands.add_parser("run", help="run a job immediately")
    run.add_argument("id")
    run.add_argument(
        "--scope",
        choices=("self", "downstream"),
        default="downstream",
        help="run only this job or this job and all downstream jobs",
    )
    return root


def main() -> None:
    args = parser().parse_args()
    settings = Settings.from_env()
    if args.action == "serve":
        uvicorn.run("scheduler.api:app", host=settings.host, port=settings.port, reload=False)
        return
    try:
        if args.action == "add":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if command == ["--"]:
                command = []
            kind = (
                "interval"
                if args.every
                else "cron"
                if args.cron
                else "date"
                if args.run_at
                else None
            )
            data = JobInput(
                id=args.id,
                name=args.name,
                command=command or None,
                depends_on=args.depends_on,
                schedule_type=kind,
                interval_seconds=args.every,
                cron=args.cron,
                run_at=args.run_at,
                timezone=args.timezone,
                working_directory=args.working_directory,
                timeout_seconds=args.timeout,
            )
            result = api_request(settings.api_url, "/api/jobs", "POST", data.model_dump())
        elif args.action == "list":
            result = api_request(settings.api_url, "/api/jobs")
        elif args.action == "remove":
            result = api_request(settings.api_url, f"/api/jobs/{quote(args.id, safe='')}", "DELETE")
            result = {"removed": args.id}
        else:
            result = api_request(
                settings.api_url,
                f"/api/jobs/{quote(args.id, safe='')}/run?scope={args.scope}",
                "POST",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


def api_request(base_url: str, path: str, method: str = "GET", payload: dict | None = None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        base_url.rstrip("/") + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urlopen(request, timeout=10) as response:
            if response.status == 204:
                return None
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("detail", error.reason)
        except (ValueError, AttributeError):
            detail = error.reason
        raise ValueError(f"HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise ValueError(f"scheduler service unavailable at {base_url}: {error.reason}") from error


if __name__ == "__main__":
    main()
