#!/bin/bash

# ============================================================================
# WebSocket Health Monitor Script
# ============================================================================
# Monitors WebSocket endpoint health and logs status every 5 minutes
# Helps detect timeout issues and connection problems early
# ============================================================================

LOG_FILE="/root/.openclaw/workspace/projects/orchestrator/logs/websocket-health.log"
ENDPOINT="http://127.0.0.1:8080/api/v1/docs"
HEARTBEAT_CHECK_INTERVAL=300  # 5 minutes

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_message() {
    local level=$1
    local message=$2
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    echo -e "${timestamp} [${level}] ${message}" >> "$LOG_FILE"
    
    case $level in
        "ERROR")   echo -e "${RED}[${timestamp}] [${level}] ${message}${NC}" ;;
        "WARN")    echo -e "${YELLOW}[${timestamp}] [${WARN}] ${message}${NC}" ;;
        "INFO")    echo -e "${GREEN}[${timestamp}] [${level}] ${message}${NC}" ;;
        *)         echo "[${timestamp}] [${level}] ${message}" ;;
    esac
}

check_backend_health() {
    log_message "INFO" "Checking backend API health..."
    
    # Check if backend is responding
    if curl -s --max-time 5 "$ENDPOINT" > /dev/null 2>&1; then
        local response_code=$(curl -s -o /dev/null -w "%{http_code}" "$ENDPOINT")
        
        if [ "$response_code" = "200" ]; then
            log_message "INFO" "✅ Backend API is responding (HTTP $response_code)"
            return 0
        else
            log_message "WARN" "⚠️  Backend API returned unexpected code: HTTP $response_code"
            return 1
        fi
    else
        log_message "ERROR" "❌ Backend API is NOT responding"
        return 2
    fi
}

check_websocket_logs() {
    log_message "INFO" "Checking WebSocket-related logs..."
    
    local backend_log="/tmp/backend.log"
    
    if [ ! -f "$backend_log" ]; then
        log_message "WARN" "⚠️  Backend log file not found: $backend_log"
        return 1
    fi
    
    # Count recent WebSocket events (last 100 lines)
    local ws_connections=$(tail -100 "$backend_log" | grep -c "WebSocket connected" || echo "0")
    local ws_disconnects=$(tail -100 "$backend_log" | grep -c "WebSocket disconnected" || echo "0")
    local ws_errors=$(tail -100 "$backend_log" | grep -c "WebSocket error" || echo "0")
    
    log_message "INFO" "Recent WebSocket activity (last 100 log lines):"
    log_message "INFO" "   • Connections: $ws_connections"
    log_message "INFO" "   • Disconnections: $ws_disconnects"
    log_message "INFO" "   • Errors: $ws_errors"
    
    # Warn if too many errors
    if [ "$ws_errors" -gt 10 ]; then
        log_message "WARN" "⚠️  High WebSocket error count detected!"
        return 1
    fi
    
    return 0
}

check_process_status() {
    log_message "INFO" "Checking process status..."
    
    # Check uvicorn process
    local uvicorn_count=$(pgrep -f "uvicorn app.main:app" | wc -l)
    if [ "$uvicorn_count" -gt 0 ]; then
        log_message "INFO" "✅ Uvicorn processes running: $uvicorn_count"
    else
        log_message "ERROR" "❌ No Uvicorn processes found!"
        return 1
    fi
    
    # Check if port 8080 is listening
    if ss -tlnp | grep -q ":8080 "; then
        log_message "INFO" "✅ Port 8080 is listening"
    else
        log_message "ERROR" "❌ Port 8080 is NOT listening"
        return 1
    fi
    
    return 0
}

check_heartbeat_config() {
    log_message "INFO" "Checking WebSocket heartbeat configuration..."
    
    # Check if sessions.py has heartbeat implementation
    local sessions_file="/root/.openclaw/workspace/projects/orchestrator/app/api/v1/endpoints/sessions.py"
    
    if [ -f "$sessions_file" ]; then
        if grep -q "heartbeat_sender\|asyncio.sleep(30)" "$sessions_file"; then
            log_message "INFO" "✅ Heartbeat mechanism found in sessions.py"
            
            # Check for ping/pong handling
            if grep -q '"ping"\|"pong"' "$sessions_file"; then
                log_message "INFO" "✅ Ping/Pong handling implemented"
            else
                log_message "WARN" "⚠️  Ping/Pong handling not found in code"
            fi
        else
            log_message "ERROR" "❌ Heartbeat mechanism NOT found in sessions.py"
            log_message "INFO" "   Please apply the WebSocket timeout fix"
            return 1
        fi
    else
        log_message "WARN" "⚠️  Sessions.py not found at expected location"
        return 1
    fi
    
    return 0
}

display_summary() {
    echo ""
    echo "=========================================="
    echo "  WebSocket Health Check Summary"
    echo "=========================================="
    echo ""
    
    local health_status=$1
    local error_count=$2
    
    if [ "$health_status" -eq 0 ] && [ "$error_count" -eq 0 ]; then
        echo -e "${GREEN}✅ All checks passed!${NC}"
        echo "WebSocket endpoint is healthy and configured correctly."
    elif [ "$health_status" -lt 3 ]; then
        echo -e "${YELLOW}⚠️  Some warnings detected.${NC}"
        echo "Review the logs above for details."
    else
        echo -e "${RED}❌ Critical issues found!${NC}"
        echo "Please investigate immediately."
    fi
    
    echo ""
    echo "Log file: $LOG_FILE"
    echo "Next check in ${HEARTBEAT_CHECK_INTERVAL/60} minutes"
    echo "=========================================="
    echo ""
}

main() {
    # Create log directory if it doesn't exist
    mkdir -p "$(dirname "$LOG_FILE")"
    
    log_message "INFO" "=========================================="
    log_message "INFO" "Starting WebSocket health check cycle"
    log_message "INFO" "=========================================="
    
    local overall_status=0
    local error_count=0
    
    # Run all checks
    check_backend_health || ((overall_status++)) || true
    check_websocket_logs || ((error_count++)) || true
    check_process_status || ((overall_status++)) || true
    check_heartbeat_config || ((overall_status++)) || true
    
    # Display summary
    display_summary $overall_status $error_count
    
    # Exit with error code if critical issues found
    if [ "$overall_status" -ge 3 ]; then
        exit 1
    fi
    
    exit 0
}

# Run main function
main "$@"
