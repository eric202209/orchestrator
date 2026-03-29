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

## 📊 Project Evolution (Phases 1-6)

### **Phase 1: Authentication System** ✅
Implemented secure JWT-based authentication with Ed25519 device pairing and API key management.

**What was built:**
- JWT access tokens (15 min) + refresh tokens (7 days)
- Bcrypt password hashing
- API key management (SHA-256 hashed, shown once)
- Ed25519 cryptographic device authentication
- Protected API endpoints with proper authorization

**Key files:**
- `app/auth.py` - JWT + Ed25519 utilities
- `app/models.py` - User, APIKey, Device models
- `app/api/v1/endpoints/auth.py` - Auth endpoints

---

### **Phase 2: OpenClaw Integration** ✅
Integrated OpenClaw session orchestration with real-time log streaming and tool tracking.

**What was built:**
- OpenClaw session service (create, execute, cleanup)
- Real-time log streaming via WebSocket/SSE
- Tool execution tracking with metadata
- 12 standardized LLM prompt templates (task planning, debugging, code review, etc.)
- Enhanced sessions API with log streaming endpoints

**Key services:**
- `app/services/openclaw_service.py`
- `app/services/log_stream_service.py`
- `app/services/tool_tracking_service.py`
- `app/services/prompt_templates.py`

---

### **Phase 3: Task Queue with Celery** ✅
Added robust background task processing with retry logic and job scheduling.

**What was built:**
- Celery task queue with Redis backend
- Three queue types: `default`, `openclaw`, `github`
- Retry logic with exponential backoff (3 retries, 60s delay)
- Job scheduler for delayed and recurring tasks
- Background workers for task execution

**Tasks implemented:**
- `execute_openclaw_task` - Execute AI development tasks
- `process_github_webhook` - Handle GitHub events
- `scheduled_task_execution` - Time-based task scheduling
- `cleanup_old_logs` - Automatic log retention

**Key files:**
- `app/celery_app.py` - Celery configuration
- `app/tasks/worker.py` - Core task execution
- `app/tasks/retry_logic.py` - Retry decorator
- `start_workers.sh` - Worker startup script

---

### **Phase 4: Frontend Dashboard** ✅
Built a modern React + TypeScript dashboard with real-time monitoring.

**What was built:**
- Login/registration with JWT authentication
- Dashboard with real-time statistics
- Project management (create, view, edit, delete)
- Task management with status tracking
- Dark theme with Tailwind CSS
- Responsive design (mobile-friendly)

**Tech stack:**
- React 18 + TypeScript
- Tailwind CSS v4
- React Router DOM
- Axios for API calls
- Vite build tool

**Key components:**
- `pages/Login.tsx`, `pages/Register.tsx`
- `pages/Dashboard.tsx`
- `pages/ProjectDetail.tsx`
- `api/client.ts` - API client with auth interceptors

---

### **Phase 5: Session Monitoring & Mobile Integration** 🚧
Real-time session status monitoring and mobile app support (in progress).

**Planned features:**
- Real-time session status WebSocket streaming
- Session lifecycle controls (start, stop, pause, resume)
- Mobile API endpoints for ClawMobile
- Tool usage analytics dashboard
- Performance metrics visualization

**Status:** Frontend components ready, backend endpoints needed

---

### **Phase 6: Frontend Dashboard Enhancements** ✅
Enhanced session management UI with full lifecycle controls.

**What was built:**
- `SessionDashboard.tsx` - Full session lifecycle UI
- Real-time WebSocket status updates
- Lifecycle control buttons (Start/Pause/Resume/Stop/Force Stop)
- Session metadata display (timestamps for all events)
- Live log streaming with color-coded levels
- Task execution interface within sessions
- Project integration with sessions grid view

**Features:**
- WebSocket auto-reconnect (3-second delay)
- Status color coding (green=running, yellow=paused, etc.)
- Auto-scrolling log stream
- Responsive design (mobile-friendly)

---

## 🛠️ Quick Start

### Prerequisites
- ✅ **OpenClaw** running locally (gateway on port 8001)
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
├── start_all.sh                 # Comprehensive startup
├── start.sh                     # Basic startup
├── requirements.txt             # Python dependencies
└── .env                         # Environment configuration
```

---

## 🔧 API Endpoints

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

# Fix: Prompts were optimized in v2.0 (see .notes/BUGFIXES-2026-03-26.md)
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

## 🎯 Next Steps

1. **Complete Phase 5** - Finalize mobile API endpoints
2. **Add Analytics** - Session performance metrics dashboard
3. **Enhance Monitoring** - Prometheus + Grafana integration
4. **Add Testing** - Unit tests for critical components
5. **Security Hardening** - Rate limiting, audit logging

---

**Last updated: 2026-03-28**
