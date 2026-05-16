# Orchestrator

Orchestrator is a FastAPI + React control plane for AI-driven development work. It manages projects, tasks, execution sessions, change-set governance, a knowledge layer, and a mobile-facing API — with backends ranging from local OpenClaw to native Ollama to OpenAI.

[![Release](https://img.shields.io/github/v/release/henrycode03/orchestrator?style=flat-square&label=release&color=555555)](https://github.com/henrycode03/orchestrator/releases)
[![Downloads](https://img.shields.io/github/downloads/henrycode03/orchestrator/total?style=flat-square&label=downloads&color=4c9be8)](https://github.com/henrycode03/orchestrator/releases)
[![License](https://img.shields.io/github/license/henrycode03/orchestrator?style=flat-square&color=blue)](https://github.com/henrycode03/orchestrator/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20windows%20%7C%20android%20%7C%20web-1f6feb?style=flat-square)](#)

---

## What It Does

- Plan and execute development tasks via configurable AI backends
- Review, approve, or hold workspace changes before they are promoted
- Retrieve relevant knowledge from Qdrant at planning time (semantic search)
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
- **Knowledge layer** — Qdrant vector store with Ollama or OpenAI embeddings; injected at planning time
- **Security audit** — command, path, quota, and retention policy checks; shadow-mode audit on every execution
- **Outcome classification** — terminal reasons tracked, aggregated at `/admin/outcome-rates`
- **Production observability** — `/health`, `/ops/metrics/summary`, `/ops/backends`, Langfuse tracing optional
- **Session lifecycle** — start, pause, resume, stop, checkpoint, retry, cross-session diff
- **Mobile bridge** — `/api/v1/mobile/*` endpoints for ClawMobile and OpenClaw status queries
- **React dashboard** — projects, tasks, sessions, settings, operator review queue

## Quick Start

See the [Setup Guide](SETUP.md) for step-by-step instructions:

- **Linux / Ubuntu** (with OpenClaw) → [Linux setup](SETUP.md#linux--ubuntu-with-openclaw)
- **Windows** (native Ollama, no OpenClaw) → [Windows setup](SETUP.md#windows-native-ollama-no-openclaw)

Linux `start.sh` runs the API, worker, and React dashboard. Windows Docker setup runs the API and `/docs`; detailed setup stays in [SETUP.md](SETUP.md).

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy, Celery |
| Frontend | React 19, Vite, TypeScript, Tailwind |
| Database | SQLite (`orchestrator.db`) |
| Queue | Redis |
| Vector store | Qdrant |
| Auth | JWT bearer tokens |

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
├── scripts/                      Operational helpers
├── SETUP.md                      Step-by-step setup guide (Linux + Windows)
├── docker-compose.yml            Linux: qdrant only
├── docker-compose.windows.yml    Windows: full stack (qdrant + redis + app + worker)
├── start.sh                      Linux native startup
└── requirements.txt
```

## Authentication

Dashboard APIs use JWT bearer tokens:
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

## Useful Scripts

| Script | Purpose |
|---|---|
| `./start.sh` | Start full stack (Linux native) |
| `./stop_all.sh` | Stop all processes |
| `./scripts/orchestrator-mobile-api.sh` | Query mobile API from shell |
| `./scripts/security_check.sh` | Run security audit |
| `./scripts/capture_replay_report.py` | Replay and report execution evidence |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `SECRET_KEY is unset` | Set a real `SECRET_KEY` in `.env` |
| API calls fail from frontend | Check `VITE_API_URL` and `logs/backend.log` |
| Celery jobs don't run | Confirm Redis is up; check `logs/worker.log` |
| Ollama not reachable from container | Set `OLLAMA_HOST=0.0.0.0` on Windows host |
| Mobile status pages empty | Set `MOBILE_GATEWAY_API_KEY` in `.env` |
| OpenClaw operations fail | Confirm `OPENCLAW_GATEWAY_URL` points to port `8000`, not `8001` |

---

**Last updated: 2026-05-16**

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
