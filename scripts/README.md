# Log Sync Script

## Purpose
Automatically sync logs from `/tmp/` to the project's `logs/` directory to prevent data loss when `/tmp/` is cleared.

## Script Location
`scripts/sync-logs.sh`

## Usage

### Manual Execution
```bash
cd /root/.openclaw/workspace/projects/orchestrator
./scripts/sync-logs.sh
```

### Automatic Execution (Optional)

Since crontab is not available, you can use OpenClaw cron jobs:

```bash
# Add a cron job to run every 6 hours
npx openclaw cron add \
  --schedule "0 */6 * * *" \
  --command "/root/.openclaw/workspace/projects/orchestrator/scripts/sync-logs.sh" \
  --name "sync-logs"
```

Or manually via the OpenClaw dashboard.

## What Gets Synced

The script automatically syncs these logs from `/tmp/`:
- `backend.log` - Backend API server logs
- `celery.log` - Celery worker startup logs
- `frontend.log` - Frontend build/dev server logs
- `orchestrator-backend.log` - Orchestrator backend logs

## Backup Strategy

Each sync creates a timestamped backup:
```
logs/
├── backend.log
├── backend-20260328-143000.log (backup from 14:30)
├── backend-20260328-203000.log (backup from 20:30)
└── celery.log
```

## Best Practices

1. **Run regularly:** Every 6 hours recommended
2. **Monitor disk space:** Old backups accumulate
3. **Clean old backups:** Delete backups older than 7 days
4. **Check for errors:** Review synced logs for issues

## Troubleshooting

### Logs not syncing?
- Check permissions: `chmod +x scripts/sync-logs.sh`
- Verify `/tmp/` has logs: `ls -la /tmp/*.log`
- Check script output: `./scripts/sync-logs.sh`

### Need more frequent syncs?
- Edit the cron schedule in `.notes/CRON_SETUP.md`
- Or run manually more often

---

*Last updated: 2026-03-29 23:07 EDT by Claw 🦅*
