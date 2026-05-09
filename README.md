# Orchestrator

Orchestrator is a FastAPI + React control plane for OpenClaw-driven development work. It manages projects, tasks, execution sessions, checkpoints, logs, permission flows, and a mobile-facing API that ClawMobile can query through OpenClaw.

[![Release](https://img.shields.io/github/v/release/henrycode03/orchestrator?style=flat-square&label=release&color=555555)](https://github.com/henrycode03/orchestrator/releases)
[![Downloads](https://img.shields.io/github/downloads/henrycode03/orchestrator/total?style=flat-square&label=downloads&color=4c9be8)](https://github.com/henrycode03/orchestrator/releases)
[![License](https://img.shields.io/github/license/henrycode03/orchestrator?style=flat-square&color=blue)](https://github.com/henrycode03/orchestrator/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20android%20%7C%20web-1f6feb?style=flat-square)](#)

## What It Includes

- FastAPI backend with JWT authentication
- React + Vite dashboard for projects, tasks, sessions, and settings
- Celery worker pipeline backed by Redis
- Session lifecycle controls: start, pause, resume, stop, retry
- Checkpoint save/load/list/cleanup flows for resumable work
- Real-time session log and status streaming over WebSockets
- Task workspace review flows including promote and request-changes
- Mobile/OpenClaw bridge endpoints under `/api/v1/mobile/*`
- Settings and helper endpoints for mobile connection setup

## Core Concepts

- `Project`: a repository or workspace root that Orchestrator tracks
- `Task`: a unit of work in a project, optionally ordered by `plan_position`
- `Session`: a runtime execution context for OpenClaw work
- `Checkpoint`: saved session state used for inspection or resume flows
- `Permission Request`: a tracked approval decision for sensitive operations

## Stack

- Backend: FastAPI, SQLAlchemy, Pydantic v2, Celery, Redis
- Frontend: React 19, Vite, TypeScript, Tailwind
- Database: SQLite by default (`orchestrator.db`)
- Auth: JWT bearer tokens

## Prerequisites

- Python 3.10+
- Node.js 18+
- `pnpm`
- Redis
- An OpenClaw gateway reachable from Orchestrator

OpenClaw port note:

- `8000` is the expected OpenClaw gateway default
- `8001` is not the Orchestrator integration target

## Quick Start

1. Create the env file:

```bash
cp .env.example .env
```

2. Set a real `SECRET_KEY` in `.env`.

3. Start the stack:

```bash
./start.sh
```

`start_all.sh` is now just a compatibility wrapper around `./start.sh`.

## What `start.sh` Does

`start.sh` is the main local startup path. It:

- creates `venv/` if missing
- installs Python deps if the venv is first created
- installs frontend deps if `frontend/node_modules` is missing
- initializes `orchestrator.db` if missing
- starts Redis if needed
- starts the FastAPI backend
- starts the Celery worker
- starts the Vite frontend
- writes runtime logs to `logs/`
- writes PID files to `run/`

Default local URLs:

- Dashboard: `http://localhost:3000`
- API root: `http://localhost:8080`
- OpenAPI docs: `http://localhost:8080/docs`
- Health check: `http://localhost:8080/health`

## Required Environment Variables

Start from [.env.example](.env.example).

Most important values:

- `SECRET_KEY`: required; the API refuses to start with the default insecure value
- `OPENCLAW_GATEWAY_URL`: where Orchestrator reaches the OpenClaw gateway
- `MOBILE_GATEWAY_API_KEY`: shared secret for `/api/v1/mobile/*`
- `ORCHESTRATOR_MOBILE_BASE_URL`: base URL used by the mobile helper script
- `CELERY_BROKER_URL`: Redis broker URL
- `CELERY_RESULT_BACKEND`: Redis result backend URL
- `VITE_API_URL`: frontend API base, default `/api/v1`
- `VITE_API_WS_HOST`: optional override when WebSockets should use a different host than the API base

Optional values:

- `OPENCLAW_API_KEY`
- `OPENCLAW_CLI_PATH`
- `OPENCLAW_CLI_ARGS`
- `GITHUB_TOKEN`
- `GITHUB_USERNAME`
- `GITHUB_WEBHOOK_SECRET`
- `LOCALHOST`

## Authentication Model

- `/api/v1/auth/*` is the public auth surface
- Most dashboard APIs now require an authenticated active user
- Session log/status WebSockets also require bearer-token authentication
- `/api/v1/mobile/*` does not use user auth; it uses the shared mobile gateway key

Bearer auth for dashboard APIs:

```http
Authorization: Bearer <access_token>
```

Mobile auth for OpenClaw/ClawMobile bridge calls:

```http
X-OpenClaw-API-Key: <shared_key>
```

or

```http
Authorization: Bearer <shared_key>
```

## Main API Areas

### Auth

- user registration and token login
- token refresh
- current-user lookup
- API key management
- device pairing / verification helpers

### Projects

- create, list, update, soft-delete
- purge old soft-deleted projects
- rebuild baseline from promoted task workspaces
- project-level logs and log summary endpoints

### Tasks

- create, list, update, delete
- execute or retry tasks
- inspect task details and logs
- workspace overview and backup helpers
- mark workspaces as `promoted` or `changes_requested`

### Sessions

- create and list sessions
- start, stop, pause, resume, and refresh tasks
- run a specific task in a session
- get logs, sorted logs, statistics, prompts, tools, workspace info
- manage session checkpoints
- WebSocket log and status streams

### Planner

- generate markdown plans
- parse planner markdown
- create tasks in batch from a plan
- update/delete project plans

### Context Preservation

- save and load session state
- store conversation history
- create task checkpoints
- export/import context payloads

### Permissions

- create approval requests
- approve or deny requests
- inspect pending/history queues
- cleanup and preflight checks

### GitHub

- webhook ingestion
- repository lookup
- issue creation

## Session and Task Lifecycle Notes

- New sessions are created as `pending` and `is_active = false`
- Starting a session moves execution into the lifecycle services
- Sessions can save manual checkpoints and resume from stored state
- Task update requests reject unsupported writable fields instead of silently ignoring them
- Task workspaces can be reviewed and promoted into a project baseline

## Mobile and ClawMobile Integration

The mobile bridge lives under `/api/v1/mobile/*` and is designed for OpenClaw tools or ClawMobile workflows.

### ClawMobile Setup in Plain English

ClawMobile usually touches this stack in two different ways:

1. Chat with OpenClaw
   This is the normal assistant/chat path. ClawMobile talks to OpenClaw directly.

2. Check Orchestrator status
   This is the dashboard-status path. OpenClaw calls Orchestrator to read projects, tasks, sessions, and dashboard data.

Think of it like this:

```text
ClawMobile -> OpenClaw chat
ClawMobile -> OpenClaw -> Orchestrator mobile API
```

If you want the Tasks/Projects/Sessions status view to work, you must set a shared key in Orchestrator:

```env
MOBILE_GATEWAY_API_KEY=replace-with-a-shared-secret
```

That mobile API key is:

- required for `/api/v1/mobile/*`
- separate from your dashboard login
- not the same as a user bearer token

If the mobile status pages are not working, the most common cause is that this shared key was never configured or does not match what OpenClaw is sending.

Available mobile endpoints include:

- `GET /api/v1/mobile/dashboard`
- `GET /api/v1/mobile/projects`
- `GET /api/v1/mobile/projects/{project_id}/status`
- `GET /api/v1/mobile/projects/{project_id}/tree`
- `GET /api/v1/mobile/projects/{project_id}/tasks`
- `GET /api/v1/mobile/sessions`
- `GET /api/v1/mobile/sessions/{session_id}/summary`
- `GET /api/v1/mobile/sessions/{session_id}/checkpoints`
- `POST /api/v1/mobile/sessions/{session_id}/stop`
- `POST /api/v1/mobile/sessions/{session_id}/resume`
- `GET /api/v1/mobile/tasks/{task_id}`
- `POST /api/v1/mobile/tasks/{task_id}/retry`

Set the shared secret on the Orchestrator side:

```env
MOBILE_GATEWAY_API_KEY=replace-with-a-shared-secret
```

## Mobile Setup Helper Endpoints

For logged-in dashboard users, Orchestrator exposes helper endpoints under `/api/v1/mobile-admin/*`:

- `GET /api/v1/mobile-admin/connection-info`
- `GET /api/v1/mobile-admin/connection-secret`

These use normal dashboard bearer auth, not the mobile shared key.

Important behavior:

- `connection-info` returns connection metadata, base URL guidance, and a masked key preview
- `connection-secret` is a compatibility-safe setup endpoint
- `connection-secret` does not return the raw key anymore
- `connection-secret` keeps setup-related fields in the response shape, but `api_key` is always `null`

This is intentional so onboarding or settings clients can still render the mobile API-key setup UI without exposing the actual shared secret over the API.

## Settings UI Notes

The dashboard settings page uses:

- `GET /api/v1/settings`
- `PATCH /api/v1/settings/profile`
- `POST /api/v1/settings/password`
- `PATCH /api/v1/settings/system`
- `GET /api/v1/settings/mobile-secret`

`/api/v1/settings/mobile-secret` also does not reveal the raw mobile key. It returns masked metadata for display.

## Helper Script for OpenClaw

The repo includes [scripts/orchestrator-mobile-api.sh](scripts/orchestrator-mobile-api.sh) for calling the mobile API safely.

Examples:

```bash
export MOBILE_GATEWAY_API_KEY=replace-with-a-shared-secret

./scripts/orchestrator-mobile-api.sh dashboard
./scripts/orchestrator-mobile-api.sh projects
./scripts/orchestrator-mobile-api.sh project-status 1
./scripts/orchestrator-mobile-api.sh sessions
./scripts/orchestrator-mobile-api.sh sessions 1 running
./scripts/orchestrator-mobile-api.sh session-summary 12
./scripts/orchestrator-mobile-api.sh project-tasks 1
./scripts/orchestrator-mobile-api.sh project-tasks 1 running
```

Optional:

```bash
export ORCHESTRATOR_MOBILE_BASE_URL=http://127.0.0.1:8080/api/v1
```

## Suggested OpenClaw Instruction

```text
You are the OpenClaw assistant for Orchestrator.

For dashboard, project, session, task, or recent activity questions, call:
  ./scripts/orchestrator-mobile-api.sh

Do not invent live status from memory.
Summarize returned JSON clearly.
If the script fails, explain the likely cause briefly.
```

## Health Check

`GET /health` returns:

- `200 OK` with `status: healthy` when API, database, and Redis checks pass
- `503` with `status: degraded` when a dependency check fails

The payload includes:

- `status`
- `checks`
- `details`

## Project Layout

```text
orchestrator/
├── app/
│   ├── api/v1/endpoints/     FastAPI route modules
│   ├── services/             Session, task, checkpoint, planner, and runtime services
│   ├── tasks/                Celery tasks and worker-side orchestration code
│   ├── main.py               FastAPI app entrypoint
│   ├── config.py             Environment-backed settings
│   ├── models.py             SQLAlchemy models
│   └── schemas.py            Pydantic schemas
├── frontend/src/             React pages, layouts, components, and API client
├── scripts/                  Operational helper scripts
├── logs/                     Runtime logs
├── run/                      PID files
├── checkpoints/              Saved session checkpoint payloads
├── requirements.txt          Python dependencies
├── start.sh                  Main startup script
├── start_all.sh              Compatibility wrapper
└── orchestrator.db           Default SQLite database
```

## Useful Scripts

- `./start.sh`
- `./start_all.sh`
- `./stop_all.sh`
- `./scripts/orchestrator-mobile-api.sh`
- `./scripts/security_check.sh`
- `./scripts/format-python.sh`
- `./scripts/capture_replay_report.py`
- `./scripts/inspect_session_state.py`

## Runtime Files You Can Ignore

These are normal local/runtime artifacts:

- `venv/`
- `frontend/node_modules/`
- `orchestrator.db`
- `dump.rdb`
- `__pycache__/`
- `logs/`
- `run/`
- `checkpoints/`

## Troubleshooting

- `SECRET_KEY is unset or still using the default value`
  Set a real `SECRET_KEY` in `.env` before starting the backend.

- Frontend loads but API calls fail
  Check `VITE_API_URL`, backend startup, and `logs/backend.log`.

- Worker jobs do not execute
  Verify Redis is running and inspect `logs/worker.log`.

- Mobile helper script fails with missing key
  Set `MOBILE_GATEWAY_API_KEY` or `OPENCLAW_API_KEY`.

- Mobile setup UI appears but no raw key is shown
  This is expected. The current code intentionally returns only masked metadata.

- OpenClaw operations fail
  Confirm `OPENCLAW_GATEWAY_URL` points to the OpenClaw gateway and not an LLM-only port.

## Development Notes

- Python tests live in `app/tests/`
- Frontend tests use Vitest
- FastAPI docs are available at `/docs`
- Current API behavior is best treated as code-first; update docs when route contracts change

## Summary

Use Orchestrator when you want OpenClaw work to be observable, resumable, and manageable from both a desktop dashboard and a lightweight mobile status flow.

## Star History

Consider giving it a star if it helps your workflow.

<p align="center">
  <a href="https://github.com/henrycode03/orchestrator/stargazers">
    <img src="https://img.shields.io/github/stars/henrycode03/orchestrator?style=for-the-badge&logo=github&color=yellow" alt="GitHub Stars" />
  </a>
</p>

<p align="center">
  <a href="https://star-history.com/#henrycode03/orchestrator&Date">
    <img src="https://api.star-history.com/svg?repos=henrycode03/orchestrator&type=Date" width="600" alt="Star History Chart" />
  </a>
</p>

**Last updated: 2026-04-21**
