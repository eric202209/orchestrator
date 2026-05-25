# Orchestrator

Orchestrator is a FastAPI + React control plane for AI-driven development work. It manages projects, ordered tasks, execution sessions, change-set governance, scoped knowledge retrieval, and a mobile-facing API — with backends ranging from local OpenClaw to native Ollama to OpenAI.

[![Release](https://img.shields.io/github/v/release/henrycode03/orchestrator?style=flat-square&label=release&color=555555)](https://github.com/henrycode03/orchestrator/releases)
[![Downloads](https://img.shields.io/github/downloads/henrycode03/orchestrator/total?style=flat-square&label=downloads&color=4c9be8)](https://github.com/henrycode03/orchestrator/releases)
[![License](https://img.shields.io/github/license/henrycode03/orchestrator?style=flat-square&color=blue)](https://github.com/henrycode03/orchestrator/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20windows%20%7C%20android%20%7C%20web-1f6feb?style=flat-square)](#)

---

## What It Does

- Plan and execute development tasks via configurable AI backends
- Review, approve, or hold workspace changes before they are promoted
- Retrieve scoped knowledge during planning, validation, and failure handling
- Monitor session health, outcome rates, and security events from a dashboard
- Bridge to ClawMobile and OpenClaw via a mobile API layer

## Supported Backends

| Backend | Use case |
|---|---|
| `local_openclaw` | Linux/Ubuntu with OpenClaw installed (default) |
| `remote_openclaw_gateway` | OpenClaw running on a remote host |
| `openai_responses_api` | OpenAI API (cloud) |
| `direct_ollama` | Native Ollama, no OpenClaw needed — Windows or air-gapped |

`direct_ollama` is planning-first: it supports planning and structured file operations, but not native shell/tool execution. Use `local_openclaw` for full agent execution.

## Key Features

- **Multi-backend runtime** — swap AI backends via one env var (`AGENT_BACKEND`)
- **Workflow templates** — YAML-defined governance per task shape; auto-promote or hold based on warning flags
- **Change-set review policy** — auto_publish_all / hold_nontrivial / hold_all; full operator override trail
- **Knowledge layer** — SQLite/Qdrant runtime with task-type and failure-signature gates; session logs expose `knowledge_used`, retrieval reason, top items, and phase
- **Security audit** — command, path, quota, and retention policy checks; shadow-mode audit on every execution
- **Outcome classification** — terminal reasons tracked, aggregated at `/admin/outcome-rates`
- **Production observability** — `/health`, `/ops/metrics/summary`, `/ops/backends`, Langfuse tracing optional
- **Session lifecycle** — start, pause, resume, stop, checkpoint, retry, cross-session diff
- **Mobile bridge** — `/api/v1/mobile/*` endpoints for ClawMobile and OpenClaw status queries
- **React dashboard** — projects, tasks, sessions, settings, operator review queue

## Quick Start

See the [Setup Guide](SETUP.md) for step-by-step instructions:

- **Linux / Ubuntu** (with OpenClaw) → [Linux setup](SETUP.md#linux--ubuntu-with-openclaw)
- **Windows WSL2** (Ollama, no OpenClaw) → [Windows Ollama setup](SETUP.md#windows-nvidia-gpu--ollama-no-openclaw)
- **Windows WSL2** (llama.cpp, no OpenClaw) → [Windows llama.cpp setup](SETUP.md#windows-llamacpp-no-openclaw)

Linux `start.sh` runs the API, worker, Qdrant, and React dashboard as native processes. Windows/WSL Docker setup uses `docker-compose.windows.yml` for the API, worker, Redis, and Qdrant. On the compact Ollama laptop, use `./wsl-start.sh --ollama`; on the llama.cpp Windows device, use plain `./wsl-start.sh`. Use the ingest command for the runtime you are actually running: native Linux uses `venv/bin/python scripts/ingest_knowledge.py --source-dir . --qdrant-url http://localhost:6333`; Docker/WSL Ollama uses `./wsl-start.sh --ollama --ingest-knowledge`.

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy, Celery |
| Frontend | React 19, Vite, TypeScript, Tailwind |
| Database | SQLite (`orchestrator.db`) |
| Queue | Redis |
| Vector store | Qdrant |
| Auth | HTTP-only session cookie for dashboard routes; JWT bearer support for token/mobile flows |

## Project Layout

```
orchestrator/
├── app/
│   ├── api/v1/endpoints/         FastAPI route modules
│   ├── services/
│   │   ├── agents/               Runtime adapters (openclaw, openai, ollama)
│   │   ├── orchestration/        Planning, execution, completion, review policy
│   │   └── workspace/            Change-set tracking, security policy
│   ├── tasks/                    Celery task workers
│   ├── main.py
│   ├── config.py
│   └── models.py
├── docs/
│   └── workflow-templates/       YAML workflow template definitions
├── frontend/src/                 React dashboard
├── knowledge/                    Runtime knowledge source files
├── scripts/                      Operational helpers
├── SETUP.md                      Step-by-step setup guide (Linux + Windows)
├── docker-compose.yml            Linux: qdrant only
├── docker-compose.windows.yml    Windows: full stack (qdrant + redis + app + worker)
├── start.sh                      Linux native startup
└── requirements.txt
```

## API and Authentication

All versioned API routes are mounted under:
```http
/api/v1
```

Dashboard login uses a session cookie through:
```http
POST /api/v1/auth/session/login
```

For compatibility with older clients, auth is also mounted at `/auth/*`, but new clients should use `/api/v1/auth/*`.

Bearer tokens are still supported for token/mobile flows:
```http
Authorization: Bearer <access_token>
```

Mobile bridge uses a shared key:
```http
X-OpenClaw-API-Key: <shared_key>
```

## Health Check

```
GET /health
```
Returns `200 healthy` or `503 degraded` with a per-dependency breakdown.

## Workspace Paths

Projects store `workspace_path` as a root-relative slug. API responses also include `resolved_workspace_path`; use that field when a script needs filesystem access. In Docker, resolved paths are under `/app/projects`. Host-side scripts should map that to the configured host project root, normally e.g. `/home/user/projects`, instead of resolving the slug against the repository checkout.

## Useful Scripts

| Script | Purpose |
|---|---|
| `./start.sh` | Start full stack (Linux native) |
| `./wsl-start.sh --ollama` | Start WSL Docker backend and dashboard for Windows-host Ollama |
| `./wsl-start.sh --ollama --no-frontend` | Start WSL Docker backend only for Windows-host Ollama |
| `./wsl-start.sh` | Start WSL Docker backend and dashboard for the Windows llama.cpp device |
| `./wsl-start.sh --ollama --ingest-knowledge` | Start Ollama WSL runtime and ingest `knowledge/` into Docker runtime |
| `./stop_all.sh` | Stop all processes |
| `./scripts/orchestrator-mobile-api.sh` | Query mobile API from shell |
| `./scripts/security_check.sh` | Run security audit |
| `./scripts/capture_replay_report.py` | Replay and report execution evidence |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `SECRET_KEY is unset` | Set a real `SECRET_KEY` in `.env` |
| API calls fail from frontend | Check `VITE_API_URL` and `logs/backend.log` |
| Login calls hit `/auth/session/login` | Rebuild/restart backend/frontend; current frontend normalizes API base to `/api/v1`, and backend keeps `/auth/*` as compatibility |
| Celery jobs don't run | Confirm Redis is up; check `logs/worker.log` |
| Project files appear inside repo checkout | Use API `resolved_workspace_path`; do not resolve relative `workspace_path` against the current shell directory |
| Ollama not reachable from container | Set `OLLAMA_HOST=0.0.0.0` on Windows host |
| Mobile status pages empty | Set `MOBILE_GATEWAY_API_KEY` in `.env` |
| OpenClaw operations fail | Confirm `OPENCLAW_GATEWAY_URL` points to port `8000`, not `8001` |

---

**Last updated: 2026-05-23**

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
