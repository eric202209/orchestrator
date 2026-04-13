# Orchestrator

Orchestrator is a control plane for running and tracking OpenClaw-driven development work. It gives you a web dashboard, task and session lifecycle management, real-time logs, and a mobile-friendly status layer that ClawMobile can query through OpenClaw.

## What It Does

- Manage projects, tasks, and OpenClaw-backed work sessions in one place
- Start, pause, resume, stop, and inspect sessions from the dashboard
- Stream logs and status updates in real time
- Track tool usage, checkpoints, and execution history
- Expose a small mobile API for status queries through OpenClaw

## Why Use It With ClawMobile

ClawMobile is best at quick operational checks. Orchestrator gives it something structured to check.

Benefits of the pairing:

- You can ask for project or session health from mobile without opening the full dashboard
- OpenClaw can query real Orchestrator state instead of guessing from memory
- The mobile flow stays concise: dashboard summary, project status, tasks, recent sessions
- Your main desktop UI remains the full control surface, while mobile becomes the fast status companion

Architecture:

```text
ClawMobile -> OpenClaw -> Orchestrator mobile endpoints
                          -> Web dashboard for full control
```

## Core Concepts

- `Project`: a tracked repo or work area
- `Task`: a unit of planned work inside a project
- `Session`: an active or historical OpenClaw execution context for that project
- `Checkpoint`: saved session state used for pause/resume flows

## Prerequisites

- Python 3.10+
- Node.js 18+
- `pnpm`
- Redis
- OpenClaw gateway running locally

Important OpenClaw port note:

- `8000` = OpenClaw gateway
- `8001` = LLM server only, not the Orchestrator integration target

## First-Time Setup

1. Copy the environment template:

```bash
cp .env.example .env
```

2. Edit `.env` and set at least:

- `SECRET_KEY`
- `MOBILE_GATEWAY_API_KEY` if you want ClawMobile/OpenClaw mobile integration
- `OPENCLAW_GATEWAY_URL` if your OpenClaw gateway is not on `http://127.0.0.1:8000`

3. For a fresh machine or first launch, use:

```bash
./start_all.sh
```

`start_all.sh` is the safest first-run path because it can automatically:

- start Redis
- create `venv/` if missing
- install Python dependencies
- install frontend dependencies
- create `orchestrator.db` if missing
- start backend, workers, and frontend

## Quick Start

If your environment is already prepared and you just want to restart the stack:

```bash
./start.sh
```

This starts:

- Redis
- FastAPI backend on `8080`
- Celery worker
- Vite frontend on `3000`

Default URLs:

- Dashboard: `http://localhost:3000`
- API: `http://localhost:8080`
- API docs: `http://localhost:8080/docs`

## Typical Usage

1. Open the dashboard.
2. Create a project.
3. Add a task or create a session.
4. Start the session and watch logs/status in real time.
5. Pause/resume when needed, or review results afterward.

Common workflows:

- Create a project and organize work into tasks
- Run an OpenClaw session for one task or a broader development goal
- Monitor progress from desktop in the dashboard
- Check health or summaries from mobile through OpenClaw

## Environment Variables

New users should start from `.env.example`.

Most important variables:

- `SECRET_KEY`: JWT signing key for auth
- `OPENCLAW_GATEWAY_URL`: where Orchestrator reaches the OpenClaw gateway
- `MOBILE_GATEWAY_API_KEY`: shared secret for `/api/v1/mobile/*`
- `ORCHESTRATOR_MOBILE_BASE_URL`: base URL used by the helper script
- `VITE_API_URL`: frontend API base URL
- `VITE_API_WS_HOST`: frontend WebSocket host for session/log streaming

Optional:

- `GITHUB_TOKEN`
- `GITHUB_USERNAME`
- `GITHUB_WEBHOOK_SECRET`
- `OPENCLAW_CLI_PATH`
- `OPENCLAW_CLI_ARGS`

## Mobile and OpenClaw Integration

Orchestrator includes mobile-oriented endpoints under `/api/v1/mobile/*`. These are meant for OpenClaw to call on behalf of ClawMobile.

Set a shared secret on the Orchestrator side:

```env
MOBILE_GATEWAY_API_KEY=replace-with-a-shared-secret
```

Accepted headers:

- `X-OpenClaw-API-Key: <key>`
- `Authorization: Bearer <key>`

Available mobile endpoints:

- `GET /api/v1/mobile/dashboard`
- `GET /api/v1/mobile/projects`
- `GET /api/v1/mobile/projects/{project_id}/status`
- `GET /api/v1/mobile/projects/{project_id}/tasks`
- `GET /api/v1/mobile/sessions`
- `GET /api/v1/mobile/sessions/{session_id}/summary`

## Helper Script for OpenClaw

Use the included helper instead of ad hoc `curl` commands:

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

### Mobile Connection Management

For logged-in Orchestrator users, you can inspect the current mobile connection setup without digging through `.env` or logs:

- `GET /api/v1/mobile-admin/connection-info`
- `GET /api/v1/mobile-admin/connection-secret`

These endpoints require normal Orchestrator authentication (`Authorization: Bearer <access_token>`), not the mobile shared key.

`connection-info` returns:

- recommended mobile base URL
- required header name
- whether a mobile key is configured
- masked key preview
- configured key source

`connection-secret` returns the current raw shared key so you can configure ClawMobile or rotate your deployment manually.

Example:

```bash
curl -H "Authorization: Bearer <access_token>" \
  http://127.0.0.1:8080/api/v1/mobile-admin/connection-info

curl -H "Authorization: Bearer <access_token>" \
  http://127.0.0.1:8080/api/v1/mobile-admin/connection-secret
```

### Recommended Host Setup

For a host-based or Tailscale-accessible deployment:

- bind Orchestrator API to `0.0.0.0:8080`
- keep llama.cpp or other local model servers on loopback-only ports like `127.0.0.1:8000`
- point ClawMobile at the host LAN/Tailscale URL, for example:
  - `http://gx10.tailnet-name:8080`
  - `http://100.x.y.z:8080`

## OpenClaw Instruction

Use this on the OpenClaw side when the goal is to answer mobile status questions accurately:

```text
You are the OpenClaw assistant for Orchestrator.

Your job is to answer status questions by calling the local helper script first, not by guessing.

Use:
  ./scripts/orchestrator-mobile-api.sh

Rules:
1. For dashboard, project, session, task, or recent activity questions, call the helper script first.
2. Do not invent live status from memory.
3. Summarize returned JSON clearly for mobile.
4. If the script fails, explain the likely cause briefly.

Command mapping:
- Overall dashboard health:
  ./scripts/orchestrator-mobile-api.sh dashboard
- List projects:
  ./scripts/orchestrator-mobile-api.sh projects
- Project status:
  ./scripts/orchestrator-mobile-api.sh project-status <project_id>
- Recent sessions:
  ./scripts/orchestrator-mobile-api.sh sessions
- Session summary:
  ./scripts/orchestrator-mobile-api.sh session-summary <session_id>
- Project tasks:
  ./scripts/orchestrator-mobile-api.sh project-tasks <project_id>
```

## Main API Areas

- Auth: register, login, refresh, current user
- Projects: create, list, update, delete
- Sessions: create, start, pause, resume, stop, logs, status
- Tasks: create, execute, inspect

Interactive docs:

- `http://localhost:8080/docs`

## Project Layout

```text
orchestrator/
├── app/                FastAPI backend, Celery tasks, services
├── frontend/           React + Vite dashboard
├── scripts/            Helper and maintenance scripts
├── logs/               Stored logs
├── requirements.txt    Python dependencies
└── start.sh            Main startup script
```

## Useful Scripts

- `cp .env.example .env` - create a local environment file
- `./start_all.sh` - best first-run setup path
- `./start.sh` - start the local stack
- `./scripts/orchestrator-mobile-api.sh ...` - query mobile endpoints
- `./scripts/security_check.sh` - run security checks
- `./scripts/sync-logs.sh` - sync logs into the project log directory
- `./scripts/cleanup-logs.sh` - clean up old logs

## Runtime Files You Can Ignore

These are normal local/runtime artifacts and should be created or updated automatically as you use the project:

- `venv/`
- `frontend/node_modules/`
- `orchestrator.db`
- `dump.rdb`
- `__pycache__/`
- `logs/`
- `checkpoints/`

They are not part of normal source control workflow.

## Troubleshooting

- `No OpenClaw session available`
  OpenClaw gateway is likely not running or not reachable on the expected port.

- Mobile helper script says API key is missing
  Set `MOBILE_GATEWAY_API_KEY` or `OPENCLAW_API_KEY` before calling the script.

- Frontend does not load
  Check `logs/frontend.log`.

- Backend does not respond
  Check `logs/backend.log` and verify Redis is running.

- Worker jobs do not execute
  Check `logs/worker.log`.

- New machine setup feels broken
  Make sure you copied `.env.example` to `.env`, then use `./start_all.sh` instead of `./start.sh` for the first run.

## Summary

Use Orchestrator when you want OpenClaw work to be observable and manageable instead of opaque. Use it with ClawMobile when you want fast mobile status access without giving up the richer desktop control surface.

---

**Last updated: 2026-04-12**