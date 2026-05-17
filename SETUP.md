# Orchestrator Setup Guide

Two paths depending on your machine:

- [Linux / Ubuntu (with OpenClaw)](#linux--ubuntu-with-openclaw) — native processes, `start.sh`
- [Windows (native Ollama, no OpenClaw)](#windows-native-ollama-no-openclaw) — Docker Compose full stack

---

## Linux / Ubuntu (with OpenClaw)

### Prerequisites

- Python 3.12
- Node.js 18+ and `pnpm`
- Redis (`sudo apt install redis-server`)
- OpenClaw installed and reachable (default port `8000`)
- Qdrant — started via Docker (see step 3)

### Steps

**1. Clone and enter the repo**

```bash
git clone https://github.com/henrycode03/orchestrator.git
cd orchestrator
```

**2. Create your `.env`**

```bash
cp .env.example .env   # if .env.example exists, otherwise create manually
```

Minimum required values:

```ini
SECRET_KEY=<generate: python3 -c "import secrets; print(secrets.token_hex(32))">
AGENT_BACKEND=local_openclaw
OPENCLAW_GATEWAY_URL=http://127.0.0.1:8000
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
```

**3. Start Qdrant (vector store)**

```bash
docker compose up -d
```

This starts only the Qdrant container (`docker-compose.yml` is qdrant-only on Linux).

**4. Start the full stack**

```bash
./start.sh
```

`start.sh` handles venv creation, Python/Node deps, DB init, Redis, FastAPI, Celery worker, and Vite frontend.

**5. Open the dashboard**

| Service | URL |
|---|---|
| Dashboard | http://localhost:3000 |
| API | http://localhost:8080 |
| API docs | http://localhost:8080/docs |
| Health | http://localhost:8080/health |

**6. Register a user**

Open the dashboard, go to the register page, and create your account.

### Stopping

```bash
./stop_all.sh
```

### Knowledge Layer (optional)

Orchestrator uses Qdrant + Ollama embeddings for knowledge retrieval at planning time.

Set in `.env`:

```ini
EMBEDDING_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
QDRANT_URL=http://localhost:6333
```

Pull the embedding model once:

```bash
ollama pull nomic-embed-text
```

If Ollama is not installed, set `EMBEDDING_PROVIDER=openai` and provide `OPENAI_API_KEY`.

### Useful `.env` options

```ini
# Switch to OpenAI backend instead of OpenClaw
AGENT_BACKEND=openai_responses_api
OPENAI_API_KEY=sk-...

# Enable Langfuse tracing
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=https://cloud.langfuse.com

# Workspace review policy
WORKSPACE_REVIEW_POLICY=hold_nontrivial   # auto_publish_all | hold_nontrivial | hold_all

# Planning repair (uses a second Ollama/API call to fix bad plans)
PLANNING_REPAIR_ENABLED=true
PLANNING_REPAIR_BASE_URL=http://ai-gateway:8000/v1
PLANNING_REPAIR_MODEL=qwen-local
```

---

## Windows (native Ollama, no OpenClaw)

Uses `docker-compose.windows.yml` for the backend stack: qdrant, redis,
orchestrator API, and Celery worker. Ollama runs natively on Windows for GPU
access.

The React dashboard is optional on Windows. Run it separately with Node/pnpm if
you want the browser UI; otherwise use the FastAPI Swagger UI at
`http://localhost:8080/docs`.

Windows process layout:

```text
Windows host
├── Ollama native app              http://localhost:11434
├── Frontend dev server (optional) http://localhost:3000
└── Docker Desktop
    ├── FastAPI backend            http://localhost:8080
    ├── Celery worker
    ├── Redis
    └── Qdrant
```

The frontend talks to the backend at `http://localhost:8080/api/v1`. The
backend talks to native Ollama through `http://host.docker.internal:11434`.

### Prerequisites

- [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/) (WSL2 backend)
- [Ollama for Windows](https://ollama.com/download/windows) with NVIDIA GPU support
  - CUDA drivers ≥ 528 required
  - Verify GPU: `ollama run qwen3:8b-q4_K_M` should run on GPU, not CPU
- Optional dashboard: Node.js 18+ and `pnpm`

### Hardware recommendations

| VRAM | Recommended model | `OLLAMA_NUM_CTX` |
|---|---|---|
| 6 GB | `qwen3:8b-q4_K_M` | 4096 |
| 8 GB | `qwen3:8b-q4_K_M` | 8192 |
| 12 GB+ | `qwen3:14b-q4_K_M` | 8192 |

27B models require CPU offloading on < 16 GB VRAM — too slow for stable use.

### Model choice

Use Ollama library tags, not random Hugging Face files, for this setup. The
known-good default is:

```text
qwen3:8b-q4_K_M
```

Why this one:

- small enough for a 6 GB laptop GPU when `OLLAMA_NUM_CTX=4096`
- stronger for planning than smaller 1.7B/4B models
- much less RAM/CPU offload pressure than 14B/30B/32B models

If `qwen3:8b-q4_K_M` still OOMs, first lower `OLLAMA_NUM_CTX` to `4096`. If it
still fails, use `qwen3:4b-q4_K_M` as the fallback model and set both
`AGENT_MODEL` and `OLLAMA_AGENT_MODEL` to that exact tag.

Avoid `latest` tags and unrelated Hugging Face quantizations unless you are
creating and testing your own Ollama Modelfile. This guide assumes the model
name is an Ollama tag that `ollama pull` can install directly.

> **Backend scope:** `direct_ollama` supports planning + structured-op orchestration (write\_file, mkdir, replace\_in\_file, etc). It does not execute arbitrary shell commands via native tools. Tasks that require only structured ops work normally; tasks that need raw shell execution will ask Ollama for a text plan but cannot run it natively — use `local_openclaw` for full shell execution.

### Steps

**1. Clone the repo**

```bash
git clone https://github.com/henrycode03/orchestrator.git
cd orchestrator
```

**2. Expose Ollama to Docker containers**

By default Ollama only listens on `127.0.0.1`. Containers cannot reach it without this:

```powershell
# Run in PowerShell as Administrator
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "Machine")
```

Then restart Ollama (or reboot). Verify:

```powershell
curl http://localhost:11434/api/tags
```

**3. Pull the required models**

```powershell
ollama pull qwen3:8b-q4_K_M
ollama pull nomic-embed-text
ollama list
ollama run qwen3:8b-q4_K_M "Return only: OK"
```

**3.5. Pre build files for docker to avoid error
cd orchestrator
```powershell
New-Item -ItemType Directory -Force -Path checkpoints, logs, knowledge
New-Item -ItemType File -Force -Path orchestrator.db
```

**4. Create `.env`**

Create a `.env` file in the repo root:

```ini
# Required
SECRET_KEY=<generate: python -c "import secrets; print(secrets.token_hex(32))">

# Database
DATABASE_URL=sqlite:////app/orchestrator.db

# Backend — direct Ollama, no OpenClaw
AGENT_BACKEND=direct_ollama
AGENT_MODEL=qwen3:8b-q4_K_M

# Ollama (native on Windows host)
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_AGENT_MODEL=qwen3:8b-q4_K_M
OLLAMA_NUM_CTX=8192
# Lower to 4096 if you hit OOM on 6 GB VRAM:
# OLLAMA_NUM_CTX=4096
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_PROVIDER=ollama
EMBEDDING_DIM=0

# Disable planning repair (no ai-gateway on this machine)
PLANNING_REPAIR_ENABLED=false

# Internal Docker network URLs — do not change
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
QDRANT_URL=http://qdrant:6333

# No OpenClaw / no external keys needed
OPENCLAW_API_KEY=
OPENAI_API_KEY=

# Review policy
WORKSPACE_REVIEW_POLICY=hold_nontrivial
```

**5. Build and start**

For docker-compose.windows.yml, 
first, you need to change file path at volumes.

```powershell
docker compose -f docker-compose.windows.yml up --build
```

First build takes a few minutes (installs Python deps inside the image).

**6. Open the API**

| Service | URL |
|---|---|
| API docs | http://localhost:8080/docs |
| Health | http://localhost:8080/health |

> **Note:** The Windows Docker setup runs the FastAPI API only; it does not
> build or serve the React dashboard. Use the FastAPI Swagger UI at
> `http://localhost:8080/docs` for API setup. If you want the dashboard on
> Windows, run the frontend separately with Node/pnpm as shown below.

**7. Optional: run the dashboard**

#Install Node.js first
https://nodejs.org download LTS version

#Update npm
npm install -g npm@latest

#Install pnpm
npm install -g pnpm

#Or update pnpm
pnpm self-update

#check
node --version
npm --version
pnpm --version

In a second PowerShell window:

```powershell
cd frontend
pnpm install
$env:VITE_API_URL = "http://localhost:8080/api/v1"
pnpm dev
```

Open the dashboard at the URL printed by Vite, usually:

```text
http://localhost:3000
```

Leave `docker compose -f docker-compose.windows.yml up --build` running in the
first terminal; the frontend talks to the Docker backend on port `8080`.

**8. Register a user**

With the dashboard: open the register page in the Vite UI.

Without the dashboard: use the API directly:

```powershell
# Register
curl -X POST http://localhost:8080/api/v1/auth/register `
  -H "Content-Type: application/json" `
  -d '{"email":"you@example.com","password":"yourpassword","full_name":"Your Name"}'

# Or by PowerShell Invoke-WebRequest
Invoke-WebRequest -Uri "http://localhost:8080/api/v1/auth/register" `
  -Method POST `
  -Headers @{"Content-Type"="application/json"} `
  -Body '{"email":"youremail","password":"yourpassword","full_name":"Your Name"}'

# Get a token
curl -X POST http://localhost:8080/api/v1/auth/tokens `
  -H "Content-Type: application/json" `
  -d '{"email":"you@example.com","password":"yourpassword"}'
```

Or open `http://localhost:8080/docs` and use the Swagger UI to register and authenticate interactively.

### Stopping

```powershell
docker compose -f docker-compose.windows.yml down
```

Add `-v` to also remove the Qdrant data volume.

### Troubleshooting (Windows)

| Symptom | Fix |
|---|---|
| `Ollama not reachable` in health check | Check `OLLAMA_HOST=0.0.0.0` is set and Ollama restarted |
| Model runs on CPU, not GPU | Verify CUDA drivers ≥ 528; check `ollama ps` shows GPU |
| OOM during generation | Lower `OLLAMA_NUM_CTX=4096` in `.env`, then `docker compose ... up` |
| Container can't reach `host.docker.internal` | Docker Desktop for Windows exposes this automatically; on WSL2-only setups add `extra_hosts: host-gateway` (already in `docker-compose.windows.yml`) |
| Qdrant data lost after restart | Data persisted via `qdrant_data` named volume by default |

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | — | Required. JWT signing key. |
| `AGENT_BACKEND` | `local_openclaw` | Runtime backend. |
| `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:8000` | OpenClaw gateway (Linux only). |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama API base URL. |
| `OLLAMA_AGENT_MODEL` | `qwen3:8b-q4_K_M` | Model used for planning/execution. |
| `OLLAMA_NUM_CTX` | `8192` | Context window tokens sent to Ollama. |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text` | Embedding model for knowledge retrieval. |
| `EMBEDDING_PROVIDER` | `auto` | `auto` / `ollama` / `openai`. |
| `EMBEDDING_DIM` | `0` | `0` = auto (768 for Ollama, 1536 for OpenAI). |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant vector store URL. |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Redis broker for Celery. |
| `WORKSPACE_REVIEW_POLICY` | `hold_nontrivial` | Change-set governance: `auto_publish_all` / `hold_nontrivial` / `hold_all`. |
| `PLANNING_REPAIR_ENABLED` | `true` | Enable second-pass plan repair. |
| `MOBILE_GATEWAY_API_KEY` | — | Shared key for `/api/v1/mobile/*`. |
| `OPENAI_API_KEY` | — | Required only for `openai_responses_api` backend. |
| `LANGFUSE_ENABLED` | `false` | Enable Langfuse tracing. |
