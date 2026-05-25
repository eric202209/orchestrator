# Orchestrator Setup Guide

Three paths depending on your machine:

- [Linux / Ubuntu (with OpenClaw)](#linux--ubuntu-with-openclaw) — native processes, `start.sh`
- [Windows WSL2 (Ollama, no OpenClaw)](#windows-Nvidia-gpu--ollama-no-openclaw) — `./wsl-start.sh`
- [Windows WSL2 (llama.cpp, no OpenClaw)](#windows-llamacpp-no-openclaw) — `./wsl-start.sh --llama`

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
cp .env.example .env
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

### Workspace paths

The Settings `workspace_root` value must be the path visible to the running backend process.

- Linux native: use the real Linux projects path, e.g. `/home/yourname/projects`.
- OpenClaw may run inside one of the container, and Orchestrator itself runs 
  as a normal project under the OpenClaw workspace. When you login to Dashboard, 
  at Settings page, set `workspace_root` to the projects root e.g.
  (`~/.openclaw/workspace/vault/projects`), not to Docker `/app/projects` and
  not to the Orchestrator repo directory.
- Each device should set `WORKSPACE_ROOT` in `.env` to its own host-visible
  projects directory. This is model/backend neutral and applies to OpenClaw,
  Ollama, and llama.cpp deployments:

```text
WORKSPACE_ROOT=/home/yourname/projects
```

Project API responses store `workspace_path` as a project-root-relative slug
and also return `resolved_workspace_path` for scripts that need filesystem
access. On Linux native/OpenClaw, `resolved_workspace_path` should already be a
host-visible path. On Docker/WSL, map Docker `/app/projects/...` back to the
configured host projects root when seeding files outside Docker. Do not resolve
the relative slug against the orchestrator repo checkout.

### Model selection

In Linux / Ubuntu mode, choose the real model in the **OpenClaw dashboard**. Orchestrator sends work to OpenClaw, and OpenClaw runs the model selected there.

Recommended Orchestrator settings:

```text
Agent Backend: Local OpenClaw
Model Family: local
Adaptation Profile: OpenClaw Default
```

Orchestrator also has a separate planning-speed model. Defaults are in `app/config.py`; override in `.env` only if needed:

```ini
PLANNING_REPAIR_ENABLED=true
PLANNING_REPAIR_BASE_URL=http://ai-gateway:8000/v1
PLANNING_REPAIR_MODEL=qwen-local
```

Only switch to `AGENT_BACKEND=openai_responses_api` if you want Orchestrator to bypass OpenClaw and call OpenAI directly.

### Stopping

```bash
./stop_all.sh
```

### Knowledge Layer (optional)

```ini
EMBEDDING_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
QDRANT_URL=http://localhost:6333
```

```bash
ollama pull nomic-embed-text
```

If Ollama is not installed, set `EMBEDDING_PROVIDER=openai` and provide `OPENAI_API_KEY`.

For Linux native/OpenClaw installs, ingest `knowledge/` into the same local
SQLite/Qdrant runtime started by `start.sh`:

```bash
venv/bin/python scripts/ingest_knowledge.py --source-dir . --qdrant-url http://localhost:6333
```

For Docker/WSL installs, use the Docker-specific startup flags below instead
so ingest targets the active container runtime rather than a host-side database:

```bash
./wsl-start.sh --ingest-knowledge
```

## Alpha Operator Verification Path

Use this path after setup to verify the current alpha baseline. The goal is not
new functionality; it is proving the install can run, recover, and report state
without manual database cleanup.

1. Clone the repo and create `.env` from `.env.example`.
2. Start the real platform entrypoint:
   - Linux: `./start.sh`
   - Windows WSL2 / Ollama: `./wsl-start.sh`
   - Windows WSL2 / llama.cpp: `./wsl-start.sh --llama --backend-only`
3. Confirm service health:

```bash
curl -fsS http://127.0.0.1:8080/health
```

4. Run backend tests:

```bash
PYTHONPATH=. venv/bin/python -m pytest app/tests -q
```

5. Run a small smoke:
   - 1 project
   - 3 tasks: `docs-update`, `python-bug-fix`, `verification-only`
6. Confirm:
   - session reaches `completed`
   - all tasks reach `done/promoted`
   - no running `TaskExecution` remains
   - change sets are captured
   - `/api/v1/ops/backends*` diagnostics are healthy
   - no manual database cleanup was needed

### Alpha Operational Caveats

- `local_openclaw` defaults to `LOCAL_OPENCLAW_MAX_PARALLEL_SESSIONS=1`.
- `backend_capacity_limit` may appear during concurrent smoke runs; this is an
  operational capacity signal and should retry before becoming actionable.
- Qdrant positive-path retrieval is not the primary alpha proof; SQLite fallback
  is the proven retrieval path.
- Windows paths should be short, local, and plain ASCII. Avoid synced folders,
  deep paths, and non-ASCII workspace paths for active execution.
- Local runtime speed depends on GPU availability, backend process health, and
  gateway responsiveness.
- Test stubs (`stub_success`, `stub_capacity`) are for automated tests only and
  require `ENABLE_TEST_RUNTIME_BACKENDS=True`.

---

## Windows (Nvidia GPU + Ollama, no OpenClaw)

Uses `docker-compose.windows.yml` for the backend stack from WSL2. Ollama runs natively on Windows for GPU access.

> **Note:** This path is tested on NVIDIA GPU with CUDA. For GGUF/llama.cpp endpoints, see the [Windows llama.cpp](#windows-llamacpp-no-openclaw) path below.
> For users with less than 8 GB VRAM, start here. Ollama/direct_ollama is the
> easier supported path for constrained NVIDIA machines; use `RUNTIME_PROFILE=compact_local`
> and `OLLAMA_NUM_CTX=4096`.

Windows process layout:

```text
Windows host
├── Ollama native app              http://localhost:11434
├── Frontend dev server (optional) http://localhost:3000
└── Docker Desktop (WSL2 backend)
    ├── FastAPI backend            http://localhost:8080
    ├── Celery worker
    ├── Redis
    └── Qdrant
```

### Prerequisites

- [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/) (WSL2 backend)
- [Ollama for Windows](https://ollama.com/download/windows) with NVIDIA GPU support
  - CUDA drivers ≥ 528 required
  - Verify GPU: `ollama run qwen3:8b-hybrid` should run on GPU, not CPU
- Node.js 18+ and `pnpm` (optional, for dashboard)

### Hardware recommendations

| VRAM | Recommended model | `OLLAMA_NUM_CTX` |
|---|---|---|
| 6 GB | `qwen3:8b-hybrid` | 4096 |
| 8 GB | `qwen3:8b-hybrid` | 4096 or 8192 |
| 12 GB+ | `qwen3:14b-q4_K_M` | 8192 |

### Steps

**1. Clone the repo (in WSL2 Ubuntu)**

```bash
git clone https://github.com/henrycode03/orchestrator.git
cd orchestrator
```

**2. Expose Ollama to Docker containers**

```powershell
# Run in PowerShell as Administrator
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "Machine")
```

Restart Ollama after setting this. Verify:

```powershell
curl http://localhost:11434/api/tags
```

**3. Pull the required models**

```powershell
ollama pull qwen3:8b-q4_K_M
ollama create qwen3:8b-hybrid -f Modelfile
ollama pull nomic-embed-text
```

**3.5. Pre-create bind-mount paths (in WSL2)**

```bash
mkdir -p data logs checkpoints knowledge
touch orchestrator.db
```

**4. Create `.env`**

```ini
SECRET_KEY=<generate: python3 -c "import secrets; print(secrets.token_hex(32))">
DATABASE_URL=sqlite:////app/orchestrator.db

AGENT_BACKEND=direct_ollama
AGENT_MODEL=qwen3:8b-hybrid

OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_AGENT_MODEL=qwen3:8b-hybrid
OLLAMA_NUM_CTX=4096
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_PROVIDER=ollama
EMBEDDING_DIM=0

PLANNING_REPAIR_ENABLED=true
PLANNING_REPAIR_BASE_URL=http://host.docker.internal:11434/v1
PLANNING_REPAIR_MODEL=qwen3:8b-hybrid
PLANNING_REPAIR_API_KEY=
PLANNING_REPAIR_DISABLE_THINKING=true

CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
QDRANT_URL=http://qdrant:6333

OPENCLAW_API_KEY=
OPENAI_API_KEY=
WORKSPACE_REVIEW_POLICY=hold_nontrivial

WORKSPACE_ROOT=/home/yourname/projects
```

**5. Build and start**

Set the runtime profile in your private `.env`, not in
`docker-compose.windows.yml`. The compose file passes `RUNTIME_PROFILE` through
to the containers and falls back to `medium` only if it is unset:

```ini
RUNTIME_PROFILE=compact_local
```

```bash
./wsl-start.sh --check
./wsl-start.sh --build
```

For backend-only validation, add `--no-frontend`.

To ingest local `knowledge/` into the active Docker runtime during startup,
use:

```bash
./wsl-start.sh --ingest-knowledge
```

**6. Open the API**

| Service | URL |
|---|---|
| API docs | http://localhost:8080/docs |
| Health | http://localhost:8080/health |

**7. Optional: run the dashboard (in WSL2)**

```bash
cd frontend
pnpm install
VITE_API_URL=http://localhost:8080/api/v1 pnpm dev
```

Open `http://localhost:3000` in your browser.

**8. Register a user**

Open the dashboard register page, or via API:

```bash
curl -s -X POST http://localhost:8080/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"yourpassword","full_name":"Your Name"}'
```

**9. Configure workspace**

In Settings, set `workspace_root` to `/app/projects` (the container path, not the host path).
API responses include both the stored relative `workspace_path` and the runtime
`resolved_workspace_path` under `/app/projects/...`.

### Stopping

```bash
docker compose -f docker-compose.windows.yml down
```

Add `-v` to also remove the Qdrant data volume.

### Troubleshooting (Windows Ollama)

| Symptom | Fix |
|---|---|
| Ollama not reachable from containers | Confirm `OLLAMA_HOST=0.0.0.0` is set; restart Ollama |
| Model runs on CPU | Verify CUDA drivers ≥ 528; check `ollama ps` |
| OOM during generation | Lower `OLLAMA_NUM_CTX=4096` in `.env` |
| `host.docker.internal` not resolving | Already in `docker-compose.windows.yml` via `extra_hosts`; verify Docker Desktop WSL2 integration is enabled |

---

## Windows (llama.cpp, no OpenClaw)

Uses `docker-compose.windows.yml` for the backend stack. llama.cpp runs natively on Windows with the Vulkan backend for AMD GPU access. Ollama is optional for embeddings; the Phase 10G third-machine path intentionally leaves it uninstalled and accepts degraded knowledge retrieval.

> **This path does not use Ollama for LLM inference.** llama.cpp exposes an OpenAI-compatible endpoint that the orchestrator treats as its agent backend.
> This is a compatibility/stability path for GGUF/Vulkan users, not the default
> recommendation for low-VRAM NVIDIA users. If Ollama supports your GPU and model,
> the Ollama path above is simpler.

Windows process layout:

```text
Windows host
├── llama-server.exe (Vulkan)      http://localhost:8001/v1
├── Ollama native app (embed only) http://localhost:11434
├── Frontend dev server (optional) http://localhost:3000
└── Docker Desktop (WSL2 backend)
    ├── FastAPI backend            http://localhost:8080
    ├── Celery worker
    ├── Redis
    └── Qdrant
```

### Hardware requirements

| Component | Minimum | Tested |
|---|---|---|
| GPU | Any AMD RDNA2+ with Vulkan | 16 GB VRAM AMD GPU class |
| RAM | 16 GB | 48 GB |
| OS | Windows 10/11 | Windows 11 |

### Known unsupported configurations

- Do not mount workspace from Windows NTFS into Docker (use WSL2 ext4)
- Do not set context > 8192 during stability testing
- Do not run the frontend dev server during stability testing
- Do not update AMD drivers between test stages
- Do not mix ROCm and Vulkan builds of llama.cpp
- Do not run Discord overlay, Steam overlay, MSI Afterburner OSD, or OBS during Vulkan inference

---

### Pre-deployment: Windows host configuration

**1. Pin AMD driver version**

Do not use "latest". Verify the current known-good Adrenalin version against recent RX 7000 series + Vulkan reports on r/LocalLLaMA before installing. Do not update drivers between test stages.

**2. Disable sleep(Options) and GPU suspend**

```powershell
# Run as Administrator
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
```

In AMD Adrenalin software, also disable GPU power-saving features and Anti-Lag.

**3. Disable overlay software**

Disable the following before any stability testing:
- Discord in-game overlay
- Steam overlay
- MSI Afterburner / RivaTuner OSD
- AMD Radeon overlay
- OBS GPU capture hooks

---

### Step 1 — Get llama.cpp (pre-built Vulkan binary)

> Do not compile llama.cpp yourself. Use the pre-built binary to eliminate build toolchain variables.

Download from: **https://github.com/ggml-org/llama.cpp/releases**

Asset name:
```
llama-<version>-bin-win-vulkan-x64.zip
```

Extract to a stable location, e.g. `D:\llama.cpp\`. Do not rename the inner versioned folder.

Verify GPU is detected:

```powershell
D:\llama.cpp\<version-folder>\llama-server.exe --list-devices
```

Expected output lists your AMD GPU as a Vulkan device. Do not proceed until this passes.

---

### Step 2 — Download a model

> Start with a smaller model for stability validation before moving to larger ones.

Recommended progression:

| Phase | Model | VRAM | Context |
|---|---|---|---|
| 1 (stability) | Qwen3 4B Q4_K_M | ~3 GB | 4096 |
| 2 (8 GB VRAM) | Qwen2.5-Coder 7B Q5_K_M | ~5.5 GB | 4096 |
| 3 (12-16 GB VRAM) | Qwen2.5-Coder 14B Q5_K_M | ~10-12 GB | 6144 |

Download GGUF files from HuggingFace and place in a stable folder, e.g. `D:\models\`.

---

### Step 3 — Start llama-server

```powershell
D:\llama.cpp\<version-folder>\llama-server.exe `
  -m "D:\models\qwen3-4b-q4_k_m.gguf" `
  --host 0.0.0.0 `
  --port 8001 `
  -ngl 99 `
  -c 4096 `
  --jinja
```

| Flag | Purpose |
|---|---|
| `-ngl 99` | Offload all layers to GPU |
| `-c 4096` | Context window — expand only after stability confirmed |
| `--host 0.0.0.0` | Required for WSL2 container access |
| `--jinja` | Chat template rendering for instruction-tuned models |

Profile guidance:

| Hardware tier | Recommended backend/profile | Context |
|---|---|---|
| < 8 GB VRAM | Ollama / `direct_ollama`, `RUNTIME_PROFILE=low_resource` | 4096 |
| 8 GB VRAM llama.cpp compatibility | `openai_responses_api`, `RUNTIME_PROFILE=low_resource` | 4096 |
| 12-16 GB VRAM llama.cpp validated locally | `openai_responses_api`, `RUNTIME_PROFILE=medium` | 6144 |

Verify:

```powershell
curl http://localhost:8001/v1/models
```

---

### Step 4 — Run independent llama-server soak test

> Run this before setting up Docker. If llama-server itself is unstable, all subsequent orchestrator debug sessions produce false signals.

```powershell
while ($true) {
  try {
    $body = '{"model":"local","messages":[{"role":"user","content":"Explain what a distributed system is in 3 sentences."}],"max_tokens":150}'
    $result = Invoke-RestMethod -Uri "http://localhost:8001/v1/chat/completions" -Method POST -ContentType "application/json" -Body $body
    Write-Host "$(Get-Date -Format 'HH:mm:ss') - OK - $($result.choices[0].message.content.Substring(0, [Math]::Min(60, $result.choices[0].message.content.Length)))..."
  } catch {
    Write-Host "$(Get-Date -Format 'HH:mm:ss') - ERROR: $_"
  }
  Start-Sleep -Seconds 10
}
```

Run for 30–60 minutes. Pass condition: no crashes, no VRAM growth, every request returns a valid completion.

---

### Step 5 — Set up Ollama for embeddings only

Ollama is used only for `nomic-embed-text` embeddings, not for LLM inference.
Skip this step for the Phase 10G third-machine no-Ollama test. In that path,
keep `EMBEDDING_PROVIDER=ollama` so `OPENAI_API_KEY=dummy` is not treated as an
OpenAI embeddings signal; the backend should warn/degrade rather than exit.

```powershell
ollama pull nomic-embed-text
```

Expose Ollama to Docker containers:

```powershell
# Run as Administrator
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "Machine")
```

Restart Ollama (Quit from system tray, reopen). Verify:

```powershell
curl http://localhost:11434/api/tags
```

Find the Windows host IP reachable from WSL2:

```bash
# In WSL2
ip route | grep default
# Use the gateway IP shown, e.g. 172.x.x.1
```

Verify from WSL2:

```bash
curl http://<windows-host-ip>:11434/api/tags
```

---

### Step 6 — Set up WSL2 + Docker Desktop

**Enable Docker Desktop WSL2 integration:**

```
Docker Desktop → Settings → Resources → WSL Integration
→ Enable integration with Ubuntu
→ Apply & Restart
```

**Create workspace inside WSL2 ext4 (not Windows NTFS):**

```bash
# In WSL2
git clone https://github.com/henrycode03/orchestrator.git ~/orchestrator
cd ~/orchestrator
mkdir -p data logs checkpoints knowledge ~/projects
touch orchestrator.db
```

> Keep all working files inside WSL2's ext4 filesystem. Do not bind-mount from Windows paths. The NTFS↔WSL boundary causes file watcher failures, chmod issues, and git corruption in long sessions.

Fix line-ending detection (prevents false git diffs on shell scripts):

```bash
git config core.autocrlf false
git config core.filemode false
git rm --cached -r .
git reset --hard HEAD
```

---

### Step 7 — Create `.env`

```bash
cd ~/orchestrator
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"  # use output as SECRET_KEY
nano .env
```

Replace contents with:

```ini
PROJECT_NAME=AI Dev Agent Orchestrator
VERSION=0.1.0
HOST=0.0.0.0
PORT=8080

SECRET_KEY=<your-generated-key>
ACCESS_TOKEN_EXPIRE_MINUTES=15
AUTH_RATE_LIMIT_WINDOW_SECONDS=60
AUTH_RATE_LIMIT_MAX_ATTEMPTS=5

DATABASE_URL=sqlite:////app/orchestrator.db

# Agent backend — llama.cpp as OpenAI-compatible endpoint
AGENT_BACKEND=openai_responses_api
AGENT_MODEL=local
OPENAI_API_KEY=dummy
OPENAI_BASE_URL=http://host.docker.internal:8001/v1

# Planning repair — same llama.cpp endpoint
PLANNING_REPAIR_ENABLED=True
PLANNING_REPAIR_BASE_URL=http://host.docker.internal:8001/v1
PLANNING_REPAIR_MODEL=local
PLANNING_REPAIR_API_KEY=dummy
PLANNING_REPAIR_DISABLE_THINKING=True
PLANNING_REPAIR_TIMEOUT_SECONDS=90
PLANNING_SYNTHESIS_TIMEOUT_SECONDS=180
REPLAN_SYNTHESIS_TIMEOUT_SECONDS=45

# Internal Docker network
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
QDRANT_URL=http://qdrant:6333

# Embeddings intentionally unavailable for the third-machine no-Ollama test.
# Keep provider=ollama so OPENAI_API_KEY=dummy is not treated as OpenAI embeddings.
EMBEDDING_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIM=0

# Runtime
RUNTIME_PROFILE=low_resource
WORKSPACE_REVIEW_POLICY=hold_nontrivial
DEMO_MODE=False
JUDGE_AGENT_ENABLED=False
INLINE_PLANNING=False

# Projects path (WSL2 ext4 path, not Windows path)
WORKSPACE_ROOT=/home/yourname/projects

# Not used in this setup
OPENCLAW_GATEWAY_URL=
OPENCLAW_API_KEY=
LANGFUSE_ENABLED=False
MOBILE_GATEWAY_API_KEY=change-me-shared-secret
MOBILE_BASE_URL=http://127.0.0.1:8080/api/v1
ADMIN_EMAILS=
GITHUB_TOKEN=
GITHUB_USERNAME=
GITHUB_WEBHOOK_SECRET=
VITE_API_URL=/api/v1
VITE_API_WS_HOST=
LOCALHOST=127.0.0.1
```

---

### Step 8 — Build and start (backend only)

> Do not run the frontend during stability testing.

Run the preflight check first. It validates host tools, `.env`, WSL2 project
path shape, llama-server inputs, and Docker Compose config without starting
services.

```bash
cd ~/orchestrator
LLAMA_CTX=4096 \
LLAMA_MODEL_PATH="D:\\AI\\models\\Qwen2.5-Coder-7B-Instruct-Q5_K_M.gguf" \
LLAMA_EXE_WIN="/mnt/d/AI/llama.cpp/llama-server.exe" \
./wsl-start.sh --llama --check --backend-only
```

`--check` reads `RUNTIME_PROFILE` and `LLAMA_CTX` from private `.env` when
present. For a previously validated 12-16 GB VRAM llama.cpp machine
intentionally running `medium`, set `RUNTIME_PROFILE=medium` and
`LLAMA_CTX=6144` in `.env`, or pass them as command-line overrides.
It also expects Ollama to be absent by default for the third-machine path. For
current-machine validation where Ollama is intentionally installed, add
`EXPECTED_OLLAMA_ABSENT=false`.

Start the backend stack through `wsl-start.sh`. `--llama` forces the original
llama.cpp path even if a local `.env` is temporarily configured for Ollama:

```bash
LLAMA_CTX=4096 \
LLAMA_MODEL_PATH="D:\\AI\\models\\Qwen2.5-Coder-7B-Instruct-Q5_K_M.gguf" \
LLAMA_EXE_WIN="/mnt/d/AI/llama.cpp/llama-server.exe" \
./wsl-start.sh --llama --backend-only
```

To ingest `knowledge/` into the active Docker runtime while starting, run:

```bash
./wsl-start.sh --llama --ingest-knowledge --backend-only
```

First build takes several minutes. Verify all containers are up:

```bash
docker compose -f docker-compose.windows.yml ps
curl http://localhost:8080/health
```

Expected health response:
```json
{"status":"healthy","checks":{"api":"ok","database":"ok","redis":"ok"}}
```

---

### Step 9 — Register a user

```bash
curl -s -X POST http://localhost:8080/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"yourpassword","full_name":"Your Name"}'
```

Or use the Swagger UI at `http://localhost:8080/docs`.

---

### Step 10 — Optional: run the dashboard

```bash
cd ~/orchestrator/frontend
pnpm install
VITE_API_URL=http://localhost:8080/api/v1 pnpm dev
```

Open `http://localhost:3000` in your browser.

---

### Step 11 — Configure workspace in Settings

In the dashboard Settings, set `workspace_root` to:

```
/app/projects
```

This is the container-internal path. The host path (`~/projects`) is mapped via `WORKSPACE_ROOT` in `.env`.
API responses include `resolved_workspace_path=/app/projects/...`; map that
path to `~/projects/...` for host-side setup scripts.

---

### API compatibility testing phases

`llama-server` is not a complete OpenAI API implementation. Test in phases:

| Phase | What to test | Pass condition |
|---|---|---|
| A | `POST /v1/chat/completions` — plain text | Valid JSON, correct structure |
| B | Structured JSON output | Model returns parseable JSON |
| C | Tool calling / function schemas | Schema accepted, response matches format |
| D | Full agent orchestration | Multi-step session completes without dropped context |

Do not move to the next phase until the current one passes.

---

### Stability testing progression

| Stage | Duration | Pass conditions |
|---|---|---|
| 1 | 30 min | No VRAM growth, no container restart, no dropped completions |
| 2 | 2 hours | Memory plateau held, no session corruption, `docker stats` flat |
| 3 | Recovery test | Kill and restart llama-server mid-session; orchestrator recovers without workspace corruption |
| 4 | Overnight | Checkpoint files valid, workspace consistent, no memory growth trend |

---

### What to monitor

Focus on memory growth, not GPU utilization percentage.

```powershell
# Windows: Task Manager > GPU > GPU Memory (Dedicated) and (Shared)
# Growing Shared GPU memory = VRAM fragmentation
```

```bash
# WSL2
watch -n 5 free -h
docker stats
```

---

### Stopping

```bash
docker compose -f docker-compose.windows.yml down
```

Add `-v` to also remove the Qdrant data volume.

Kill llama-server: `Ctrl+C` in the PowerShell window running it.

---

### Troubleshooting (AMD + llama.cpp)

| Symptom | Fix |
|---|---|
| `--list-devices` shows CPU only | Vulkan runtime missing or driver mismatch; update AMD driver |
| Crash after driver update | Driver regression; roll back to pinned version |
| Container can't reach port 8001 | Confirm `--host 0.0.0.0` in llama-server command |
| `host.docker.internal` not resolving | Already in `docker-compose.windows.yml`; verify Docker Desktop WSL2 integration is enabled |
| OOM / instability after 30–60 min | VRAM fragmentation; drop `-c` to `2048`, restart server |
| Git permission errors in container | Workspace on Windows NTFS; move all files to `~/` inside WSL2 |
| Structured JSON malformed | Verify `--jinja` flag is set; check model tokenizer config |
| Overnight test fails silently | Sleep/suspend triggered; confirm `powercfg` settings |
| Inference unstable | Overlay software conflict; disable all overlays before testing |
| `WORKSPACE_ROOT` error on compose up | Run `export WORKSPACE_ROOT=...` before `docker compose` command |
| Project files appear inside repo checkout | Use API `resolved_workspace_path`; do not resolve relative `workspace_path` against the current shell directory |
| Login calls hit `/auth/session/login` | Current backend keeps `/auth/*` as compatibility, but new frontend/API clients should use `/api/v1/auth/*` |

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | — | Required. JWT signing key. |
| `AGENT_BACKEND` | `local_openclaw` | Runtime backend: `local_openclaw`, `direct_ollama`, or `openai_responses_api`. |
| `OPENAI_BASE_URL` | — | Required for `openai_responses_api`. Point to llama.cpp or any OpenAI-compatible endpoint. |
| `OPENAI_API_KEY` | — | Required for `openai_responses_api`. Set to `dummy` for local endpoints. |
| `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:8000` | OpenClaw gateway (Linux only). |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama API base URL. |
| `OLLAMA_AGENT_MODEL` | `qwen3:8b-hybrid` | Model used for planning/execution (direct_ollama only). |
| `OLLAMA_NUM_CTX` | `4096` | Context window tokens sent to Ollama. |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text` | Embedding model for knowledge retrieval. |
| `EMBEDDING_PROVIDER` | `auto` | `auto` / `ollama` / `openai`. |
| `EMBEDDING_DIM` | `0` | `0` = auto (768 for Ollama, 1536 for OpenAI). |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant vector store URL. |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Redis broker for Celery. |
| `WORKSPACE_ROOT` | `~/projects` | Host-visible project workspace root for native/host-run backend and worker. Set this per device in `.env`. |
| `HOST_WORKSPACE_ROOT` | — | Optional host-only override used when persisted Settings still contain a container path such as `/app/projects`. |
| `OPENCLAW_WORKSPACE` | — | Legacy fallback for older OpenClaw-oriented installs. Prefer `WORKSPACE_ROOT` for new installs, including Ollama and llama.cpp lanes. |
| `WORKSPACE_REVIEW_POLICY` | `hold_nontrivial` | `auto_publish_all` / `hold_nontrivial` / `hold_all`. |
| `PLANNING_REPAIR_ENABLED` | `true` | Enable second-pass plan repair. |
| `PLANNING_REPAIR_BASE_URL` | — | Endpoint for planning repair model. |
| `RUNTIME_PROFILE` | `standard` | `standard`, `medium`, or `low_resource`. Use `low_resource` for 8 GB llama.cpp or constrained local backends; use `medium` only after a 12-16 GB VRAM llama.cpp setup is stable. |
| `MOBILE_GATEWAY_API_KEY` | — | Shared key for `/api/v1/mobile/*`. |
| `LANGFUSE_ENABLED` | `false` | Enable Langfuse tracing. |
