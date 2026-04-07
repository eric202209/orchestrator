#!/bin/bash
# Supervisor Configuration Manager
# 
# Purpose: Copy configuration files from config/ directory to appropriate system locations
#
# Usage:
#   ./deploy-config.sh supervisor  # Deploy Supervisor config
#   ./deploy-config.sh all         # Deploy all configurations
#   ./deploy-config.sh --help      # Show help

set -e

CONFIG_DIR="/root/.openclaw/workspace/vault/projects/orchestrator/config"
SUPERVISOR_CONF_DIR="/etc/supervisor/conf.d"

deploy_supervisor() {
    echo "📦 Deploying Supervisor configuration..."
    
    if [ ! -f "$CONFIG_DIR/supervisor-celery.conf" ]; then
        echo "❌ supervisor-celery.conf not found in config/"
        exit 1
    fi
    
    # Copy to Supervisor config directory
    cp "$CONFIG_DIR/supervisor-celery.conf" "$SUPERVISOR_CONF_DIR/"
    
    # Reload Supervisor
    supervisorctl reread
    supervisorctl update
    
    echo "✅ Supervisor configuration deployed successfully"
}

deploy_all() {
    echo "🚀 Deploying all configurations..."
    
    if [ -f "$CONFIG_DIR/supervisor-celery.conf" ]; then
        deploy_supervisor
    else
        echo "⚠️  No configuration files found"
    fi
    
    echo "✅ All configurations deployed"
}

show_help() {
    echo "Usage: $0 [command]"
    echo ""
    echo "Commands:"
    echo "  supervisor  Deploy Supervisor configuration only"
    echo "  all         Deploy all configurations"
    echo "  --help      Show this help message"
    echo ""
    echo "Configuration files location: $CONFIG_DIR/"
}

case "$1" in
    supervisor)
        deploy_supervisor
        ;;
    all)
        deploy_all
        ;;
    --help|-h)
        show_help
        ;;
    *)
        show_help
        exit 1
        ;;
esac
