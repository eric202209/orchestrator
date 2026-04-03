# Orchestrator Control UI - AI Dev Agent Platform

**Your AI-powered development orchestrator for automating software projects with OpenClaw agents.**

---

## 🚀 What Is This?

This is a complete AI development agent orchestrator that automates software development tasks using OpenClaw's AI agents. It handles everything from project creation to code generation, testing, and deployment.

### Core Features
- **Multi-phase development workflow** - From authentication to mobile integration
- **Real-time monitoring** - Watch AI agents work live via WebSocket streams
- **Task queue system** - Background processing with Celery and Redis
- **Session lifecycle management** - Start, pause, resume, and stop AI sessions
- **Tool execution tracking** - Full audit trail of all operations
- **Mobile-ready API** - Control sessions from any device

---


## 🛠️ Quick Start

### Prerequisites
- ✅ **OpenClaw** running locally (gateway on port 8000) ⚠️ NOT 8001!
  - Port 8000 = OpenClaw Gateway (python3 process)
  - Port 8001 = llama.cpp AI server (LLM API only, DO NOT use for Orchestrator!)
- ✅ **Redis** running (default port 6379)
- ✅ **Python 3.10+** installed
- ✅ **Node.js 18+** installed
- ✅ **pnpm** installed (`npm install -g pnpm`)

### One-Command Startup
```bash
cd ~/.openclaw/workspace/projects/orchestrator
./start_all.sh
```

This script automatically:
- ✅ Checks and starts Redis
- ✅ Ensures virtual environment exists
- ✅ Installs frontend dependencies if needed
- ✅ Initializes database if needed
- ✅ Starts backend (port 8080)
- ✅ Starts Celery workers
- ✅ Starts frontend (port 3000)
- ✅ Verifies all services are healthy

---

## 📁 Project Structure

```
orchestrator/
├── app/                          # Backend application (unified structure)
│   ├── api/v1/
│   │   ├── endpoints/
│   │   │   ├── auth.py          # Authentication endpoints
│   │   │   ├── sessions.py      # Session management
│   │   │   ├── tasks.py         # Task management
│   │   │   ├── projects.py      # Project management
│   │   │   └── orchestrator.py  # Orchestrator endpoints
│   │   └── router.py            # API router
│   ├── services/
│   │   ├── openclaw_service.py  # OpenClaw integration
│   │   ├── log_stream_service.py # Real-time logging
│   │   ├── tool_tracking_service.py # Tool audit trail
│   │   ├── prompt_templates.py  # LLM prompt templates
│   │   ├── task_service.py      # Task operations
│   │   ├── context_service.py   # Context preservation
│   │   ├── permission_service.py # Permission approval
│   │   ├── project_isolation_service.py # Workspace isolation
│   │   ├── github_service.py    # GitHub integration
│   │   ├── openclaw_executor.py # OpenClaw task executor
│   │   └── __init__.py          # Service exports
│   ├── celery_app.py            # Celery configuration
│   ├── main.py                  # FastAPI app
│   ├── models.py                # Database models
│   └── database.py              # DB initialization
├── frontend/                     # React frontend
│   ├── src/
│   │   ├── api/
│   │   │   └── client.ts        # API client
│   │   ├── pages/
│   │   │   ├── Dashboard.tsx    # Main dashboard
│   │   │   ├── ProjectDetail.tsx # Project view
│   │   │   └── SessionDashboard.tsx # Session control
│   │   └── types/
│   │       └── api.ts           # TypeScript types
│   ├── .env                     # Frontend config
│   └── package.json
├── orchestrator.db              # SQLite database (root level)
├── logs/                        # Log files
├── scripts/                     # Utility scripts
│   ├── security_check.sh           # Security scanning
│   ├── sync-logs.sh                # Log synchronization
│   ├── cleanup-*.sh                # Maintenance scripts
│   └── README.md                   # Scripts documentation
├── start_all.sh                 # Comprehensive startup
├── start.sh                     # Basic startup
├── requirements.txt             # Python dependencies
└── .env                         # Environment configuration
```

---

## 🔧 API Endpoints & Scripts

### Mobile Gateway Endpoints

These endpoints are intended for the OpenClaw/Gateway side of the stack, not for the Android app to call directly.

Architecture:
```text
clawmobile -> Tailscale -> GX10 host -> OpenClaw/Gateway -> Orchestrator /api/v1/mobile/*
```

Required environment on the Orchestrator side:

```env
MOBILE_GATEWAY_API_KEY=replace-with-a-shared-secret
```

Accepted auth headers:
- `X-OpenClaw-API-Key: <key>`
- `Authorization: Bearer <key>`

Available endpoints:
- `GET /api/v1/mobile/dashboard`
- `GET /api/v1/mobile/projects`
- `GET /api/v1/mobile/projects/{project_id}/status`
- `GET /api/v1/mobile/projects/{project_id}/tasks`
- `GET /api/v1/mobile/sessions`
- `GET /api/v1/mobile/sessions/{session_id}/summary`

### OpenClaw Wrapper Script

Use the helper script instead of ad hoc `curl` calls:

```bash
export MOBILE_GATEWAY_API_KEY=replace-with-a-shared-secret
./scripts/orchestrator-mobile-api.sh dashboard
./scripts/orchestrator-mobile-api.sh projects
./scripts/orchestrator-mobile-api.sh project-status 1
./scripts/orchestrator-mobile-api.sh sessions
./scripts/orchestrator-mobile-api.sh sessions 1 running
./scripts/orchestrator-mobile-api.sh session-summary 12
./scripts/orchestrator-mobile-api.sh project-tasks 1 running
```

Optional:

```bash
export ORCHESTRATOR_MOBILE_BASE_URL=http://your_ip_here:8080/api/v1
```

Suggested OpenClaw tool behavior:
- When the user asks for overall orchestrator health, call `./scripts/orchestrator-mobile-api.sh dashboard`
- When the user asks about projects, call `./scripts/orchestrator-mobile-api.sh projects`
- When the user asks about one project, call `./scripts/orchestrator-mobile-api.sh project-status <id>`
- When the user asks about recent sessions, call `./scripts/orchestrator-mobile-api.sh sessions`
- When the user asks about one session, call `./scripts/orchestrator-mobile-api.sh session-summary <id>`

### Available Scripts in `scripts/` Directory

**Core Utilities:**
- **`orchestrator-mobile-api.sh`** - Mobile API helper (NEW!) - Query Orchestrator status via OpenClaw
- **`security_check.sh`** - Security scanning and vulnerability detection
- **`sync-logs.sh`** / **`sync-tmp-logs.sh`** - Sync logs from `/tmp/` to project `logs/` directory
- **`cleanup-logs.sh`** - Clean up old log files (7-day retention)
- **`cleanup-historical-docs.sh`** - Archive historical documentation
- **`check-logs-status.sh`** - Monitor log file health and sizes
- **`deploy-config.sh`** - Deploy Supervisor configuration files

All scripts are executable (`chmod +x`) and documented in `scripts/README.md`.

### Copy-Paste OpenClaw Instruction

Use this as a system prompt, tool instruction, or agent note on the OpenClaw side:

```text
You are the OpenClaw assistant for Orchestrator.

Your job is to help the mobile user query Orchestrator status through the local helper script, not by guessing.

Architecture:
- clawmobile talks to OpenClaw
- OpenClaw runs on the GX10 host/container stack
- Orchestrator is a separate backend/frontend service
- To read Orchestrator state, use this helper script:
  ./scripts/orchestrator-mobile-api.sh

Rules:
1. When the user asks for orchestrator status, dashboard health, projects, sessions, tasks, or recent activity, call the helper script first.
2. Do not invent live status from memory.
3. If the script returns JSON, summarize it clearly for mobile.
4. If the script fails, explain the failure briefly and mention the likely cause.
5. Keep answers concise and operational.

Command mapping:
- Overall orchestrator health or status:
  ./scripts/orchestrator-mobile-api.sh dashboard
- List projects:
  ./scripts/orchestrator-mobile-api.sh projects
- Project status:
  ./scripts/orchestrator-mobile-api.sh project-status <project_id>
- Recent sessions:
  ./scripts/orchestrator-mobile-api.sh sessions
- Sessions for a project:
  ./scripts/orchestrator-mobile-api.sh sessions <project_id>
- Session summary:
  ./scripts/orchestrator-mobile-api.sh session-summary <session_id>
- Project tasks:
  ./scripts/orchestrator-mobile-api.sh project-tasks <project_id>
- Project tasks filtered by status:
  ./scripts/orchestrator-mobile-api.sh project-tasks <project_id> <status>

How to respond:
- For dashboard requests, report projects, active/running sessions, task totals, failures, and recent activity.
- For project requests, report project name, active sessions, and task breakdown.
- For session requests, report session name, status, recent logs, and task progress.
- If IDs are missing and needed, first call `projects` or `sessions` to discover them.
```

### Authentication
- `POST /api/v1/auth/register` - Register new user
- `POST /api/v1/auth/tokens` - Login and get tokens
- `POST /api/v1/auth/refresh` - Refresh access token
- `GET /api/v1/auth/me` - Get current user

### Projects
- `GET /api/v1/projects` - List all projects
- `POST /api/v1/projects` - Create project
- `GET /api/v1/projects/{id}` - Get project details
- `PUT /api/v1/projects/{id}` - Update project
- `DELETE /api/v1/projects/{id}` - Delete project

### Sessions
- `GET /api/v1/projects/{project_id}/sessions` - List project sessions
- `POST /api/v1/sessions` - Create session
- `POST /api/v1/sessions/{id}/start` - Start session
- `POST /api/v1/sessions/{id}/stop` - Stop session
- `POST /api/v1/sessions/{id}/pause` - Pause session
- `POST /api/v1/sessions/{id}/resume` - Resume session
- `GET /api/v1/sessions/{id}/logs` - Get session logs
- `WebSocket /api/v1/sessions/{id}/logs` - Real-time log stream
- `WebSocket /api/v1/sessions/{id}/status` - Real-time status

### Tasks
- `POST /api/v1/tasks` - Create task
- `POST /api/v1/tasks/execute` - Execute task via Celery
- `GET /api/v1/tasks/{id}` - Get task details

### Interactive API Docs
Visit: **http://localhost:8080/docs** (Swagger UI)

---

## 🐛 Troubleshooting

### Services Won't Start

**1. Check if ports are in use:**
```bash
lsof -i :8080  # Backend
lsof -i :3000  # Frontend
lsof -i :6379  # Redis
```

**2. Check service logs:**
```bash
# Backend logs
tail -50 /tmp/backend.log

# Worker logs
tail -50 /tmp/celery_worker.log

# Frontend logs
tail -50 /tmp/frontend.log
```

**3. Verify Redis is running:**
```bash
redis-cli ping  # Should return PONG
```

**4. Restart everything:**
```bash
./start_all.sh
```

---

### Frontend Can't Connect to Backend

**Check VITE_API_URL in frontend/.env:**
```bash
cat frontend/.env
```

Should contain:
```
VITE_API_URL=http://localhost:8080/api/v1
```

**Or configure `LOCALHOST` in root `.env`:**
```bash
cat .env | grep LOCALHOST
# Set: LOCALHOST=<your-ip> for containerized deployment
```

**Browser can't access dashboard (host browser issues):**

**1. Get access token from API:**
```bash
# Register test user
curl -X POST http://localhost:8080/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "test123"}'

# Login and get token
TOKEN=$(curl -X POST http://localhost:8080/api/v1/auth/tokens \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "test123"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin).get('access_token', ''))")

echo "Token: $TOKEN"
```

**2. Use token in browser:**
- Open DevTools (F12) → **Application** tab → **Local Storage** → `http://localhost:3000`
- Add item: Key=`access_token`, Value=`<your-token>`
- Refresh page

**3. Common issues:**
- **401 Unauthorized** → Token expired, get new token
- **404 Not Found** → API URL mismatch, check `VITE_API_URL`
- **Blank page** → Check browser console for JavaScript errors
- **CORS errors** → Ensure backend allows your origin in `app/main.py`

---

### Celery Worker Not Processing Tasks

```bash
# Check Redis connection
redis-cli ping

# Check worker logs
tail -f /tmp/celery_worker.log

# Verify queues
celery -A app.celery_app inspect active -q default,openclaw,github

# Check task registry
celery -A app.celery_app inspect registered
```

---

### Task Fails to Execute

**1. Check Celery worker output:**
```bash
tail -100 /tmp/celery_worker.log
```

**2. Common errors:**
- `No OpenClaw session available` → OpenClaw gateway not running
- `Connection refused` → Redis not running
- `Session not found` → Session was deleted or ID is wrong
- `Context window overflow` → See bug fixes below

**3. Context window overflow (65,536 token limit):**
```bash
# Check if prompts are too verbose
tail -100 /tmp/celery_worker.log | grep -i "token\|context"

```

---

### No Live Logs Appearing

**1. Check WebSocket connection:**
- Open browser DevTools (F12)
- Go to **Network** tab
- Filter by **WS** (WebSockets)
- Look for `/api/v1/sessions/{id}/logs` connection

**2. Check backend logs:**
```bash
tail -f /tmp/backend.log
```

**3. Verify session exists:**
```bash
curl http://localhost:8080/api/v1/sessions/1 \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

### Database Errors

**Reset database:**
```bash
# Backup first
cp orchestrator.db orchestrator.db.backup

# Reset
rm orchestrator.db
python3 -c "from app.database import init_db; init_db()"
```

**Note:** Database is located in the **root directory** (where you run `./start.sh`), not in `app/`. If you see two databases, delete the one in `app/` and keep the root-level one.

---

### OpenClaw Integration Issues

```bash
# Check OpenClaw gateway health
curl http://localhost:8001/health
# Should return: {"status":"ok"}

# Check if OpenClaw CLI is available
openclaw --version

# Test sessions spawn manually
openclaw sessions spawn --task "Test task" --mode session
```

---

### Debug Mode

**Enable verbose logging:**

**Backend:**
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --log-level debug
```

**Celery Worker:**
```bash
celery -A app.celery_app worker --loglevel=debug -Q default,openclaw,github
```

**Frontend:**
Open browser DevTools → **Console** tab

**Database inspection:**
```bash
python3 << 'EOF'
import sqlite3
conn = sqlite3.connect('orchestrator.db')
cursor = conn.cursor()

# List all sessions
cursor.execute("SELECT id, name, status, project_id, created_at FROM sessions")
for row in cursor.fetchall():
    print(row)

conn.close()
EOF
```

---

## 🔄 Production Deployment

### Backend (Systemd Service)
```bash
# Create service file
sudo tee /etc/systemd/system/orchestrator-backend.service > /dev/null << 'EOF'
[Unit]
Description=Orchestrator Backend API
After=network.target redis.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/root/.openclaw/workspace/projects/orchestrator
Environment="PATH=/root/.openclaw/workspace/projects/orchestrator/venv/bin:/usr/bin:/bin"
ExecStart=/root/.openclaw/workspace/projects/orchestrator/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable orchestrator-backend
sudo systemctl start orchestrator-backend
```

### Celery Worker (Systemd Service)
```bash
sudo tee /etc/systemd/system/orchestrator-worker.service > /dev/null << 'EOF'
[Unit]
Description=Orchestrator Celery Worker
After=network.target redis.service orchestrator-backend.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/root/.openclaw/workspace/projects/orchestrator
Environment="PATH=/root/.openclaw/workspace/projects/orchestrator/venv/bin:/usr/bin:/bin"
ExecStart=/root/.openclaw/workspace/projects/orchestrator/venv/bin/celery -A app.celery_app worker --loglevel=info -Q default,openclaw,github
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable orchestrator-worker
sudo systemctl start orchestrator-worker
```

### Frontend (Nginx)
```bash
# Build frontend
cd frontend
pnpm build

# Configure Nginx
sudo tee /etc/nginx/sites-available/orchestrator > /dev/null << 'EOF'
server {
    listen 80;
    server_name your-domain.com;

    location / {
        root /root/.openclaw/workspace/projects/orchestrator/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    location /api {
        proxy_pass http://localhost:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/orchestrator /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## 📚 Additional Resources

- **API Documentation:** http://localhost:8080/docs (Swagger UI)
- **Celery Flower (Monitoring):** http://localhost:5555 (if configured)
- **Internal Documentation:** `.notes/` folder (detailed implementation notes)

---

## 🛑 Stopping Services

### Stop All Services
```bash
# Option 1: Use stop script
./stop_all.sh

# Option 2: Manual stop
pkill -f "uvicorn app.main:app"
pkill -f "celery -A app.celery_app worker"
pkill -f "vite"
```

### Stop Individual Services
```bash
# Stop backend
pkill -f "uvicorn app.main:app"

# Stop workers
pkill -f "celery -A app.celery_app worker"

# Stop frontend
pkill -f "vite"
```

---

**Last updated: 2026-04-03**
