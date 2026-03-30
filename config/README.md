# Orchestrator Configuration Directory
#
# Purpose: Centralized configuration management for Orchestrator services
# Location: /root/.openclaw/workspace/projects/orchestrator/config/
#
# Files:
#   - supervisor-celery.conf  : Supervisor configuration for Celery workers
#
# Usage:
#   1. Copy to system location:
#      cp config/supervisor-celery.conf /etc/supervisor/conf.d/
#
#   2. Deploy using script:
#      ./deploy-config.sh supervisor
#
#   3. Reload Supervisor:
#      supervisorctl reread && supervisorctl update
#
# Environment-specific settings:
#   - Modify numprocs based on available memory (default: 0, no resource usage)
#   - Adjust concurrency for different workloads
#   - Set appropriate log file paths
#
# Notes:
#   - Do not commit sensitive data (API keys, passwords)
#   - Use environment variables for secrets
#   - Keep configurations in version control
#   - Document all configuration changes
#   - All comments use English for consistency
