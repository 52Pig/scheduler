# Agent Guide

This file is the repository entry point for coding agents. Read it before changing code.

## Required Reading

1. Read [`docs/README.md`](docs/README.md) to select the relevant product, operator, security, and engineering contracts.
2. Read [`docs/architecture.md`](docs/architecture.md) for component boundaries, current maturity, and known gaps.
3. Read the task/CLI/API, operations, security, or data document relevant to the change. Read `CONTRIBUTING.md` before preparing a reviewable change.
4. Inspect the implementation and tests named in the code map below. Documentation is not a substitute for checking current code.

## Product Boundary

- This repository is the scheduler framework. A registered task owns its executable, interpreter/runtime, script, arguments, working directory, external dependencies, data, and safe rerun behavior.
- Never install, update, repair, or recreate another task's runtime or dependencies as part of scheduler work.
- The scheduler starts the configured executable and arguments. Keep argument-array execution with `shell=False`; do not concatenate task input into a shell string.
- Treat registration and run access as operating-system code execution under the scheduler service identity. Do not add claims of authentication, authorization, isolation, secrets management, retries, cancellation, HA, or distributed execution unless the implementation and tests provide them.
- Do not run real registered business/data tasks during verification. Use disposable task IDs and bounded commands, then remove the temporary definitions.

## Code Map

| Concern | Source of truth |
| --- | --- |
| Process configuration and environment variables | `scheduler/config.py`, `.env.example` |
| Task request validation and persisted definition shape | `scheduler/models.py` |
| HTTP API and application lifespan | `scheduler/api.py`, `tests/test_api.py` |
| CLI grammar and HTTP client | `scheduler/cli.py`, `tests/test_cli.py` |
| Scheduling, DAG validation, workflow execution, subprocess lifecycle | `scheduler/service.py`, `tests/test_scheduler.py` |
| SQLite schema, task versions, run output, retention | `scheduler/database.py` |
| Web UI, graph, history, log detail | `scheduler/static/`, `tests/test_api.py` |
| Windows installation, monitoring, recovery, migration | `deploy/windows/`, `tests/test_deploy.py`, `deploy/windows/SERVICE_OPERATIONS.md` |
| User and operator contracts | `README.md`, `docs/` |

## Invariants

- Task IDs are stable. Definition edits increment revisions; run records retain the revision used at start.
- Dependencies form a DAG and current validation requires one workflow root. Do not introduce domain-specific task types or hardcoded pipeline names.
- A scheduled node and its eligible unscheduled descendants share a workflow run ID. Each node attempt has its own run ID.
- Dependencies run only after required parents succeed. Failed/timed-out ancestors skip downstream nodes; independent branches can continue.
- Manual run scope is `self` or `downstream`; selected upstream nodes are not implicitly rerun.
- Stop targets one active run ID. It terminates the local process tree and records `cancelled`; it does not cancel sibling workflow nodes or roll back external effects.
- Run status and output must be externally observable through the API. Preserve output bounds and five-day terminal-run retention unless the product contract and migration plan change together.
- The Windows service currently runs on `127.0.0.1:1000` as LocalService and stores live state outside the source tree. Never overwrite or delete the live database during tests or upgrades.
- Architecture documents distinguish current behavior from target designs. Update API/task/operator/security docs whenever their contracts change.

## Change Workflow

1. Trace a user-visible change through its owning layer: UI -> API -> service -> persistence/executor -> tests and docs.
2. Keep changes within the owning boundary. Avoid adding abstractions unless they define a real extension point or remove meaningful duplication.
3. Add tests for observable behavior, especially execution success/failure/timeout, dependency propagation, persistence/restart, API errors, and live output when relevant.
4. Run `python -m pytest -q`, `python -m ruff check .`, and `python -m ruff format --check .`. Run `node --check scheduler/static/app.js` and `node --check scheduler/static/logs.js` after editing those files. Parse-check changed PowerShell scripts when possible.
5. Review the final diff for task-specific paths, secret material, changed service/database paths, stale cache-busting versions, generated files, and unsupported feature claims.
6. For user-facing or operational changes, give a concise report of changed behavior, validation, deployment/restart state, and remaining limitations.

## Operations Caution

The installed Windows service is controlled by an administrator and may be serving live tasks. Do not stop/restart it merely to run tests. Restart only when required to activate an authorized code change; first check for active runs, then verify service health and port 1000 after restart. Do not modify unrelated scheduled tasks on the host.
