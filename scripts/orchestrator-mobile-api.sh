#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

load_env_file() {
    local env_file="$1"

    [[ -f "${env_file}" ]] || return 0

    while IFS= read -r line || [[ -n "${line}" ]]; do
        line="${line%$'\r'}"

        [[ -n "${line}" ]] || continue
        [[ "${line}" =~ ^[[:space:]]*# ]] && continue
        [[ "${line}" == *=* ]] || continue

        local key="${line%%=*}"
        local value="${line#*=}"

        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"

        if [[ -z "${!key:-}" ]]; then
            export "${key}=${value}"
        fi
    done < "${env_file}"
}

load_env_file "${PROJECT_ROOT}/.env"
load_env_file "${PROJECT_ROOT}/.env.local"

BASE_URL="${ORCHESTRATOR_MOBILE_BASE_URL:-http://127.0.0.1:8080/api/v1}"
API_KEY="${MOBILE_GATEWAY_API_KEY:-${OPENCLAW_API_KEY:-}}"

usage() {
    cat <<'EOF'
Usage:
  orchestrator-mobile-api.sh dashboard
  orchestrator-mobile-api.sh projects
  orchestrator-mobile-api.sh project-status <project_id>
  orchestrator-mobile-api.sh sessions [project_id] [status]
  orchestrator-mobile-api.sh session-summary <session_id>
  orchestrator-mobile-api.sh project-tasks <project_id> [status]

Environment:
  MOBILE_GATEWAY_API_KEY       Shared key required by Orchestrator /api/v1/mobile/*
  OPENCLAW_API_KEY             Fallback if MOBILE_GATEWAY_API_KEY is not set
  ORCHESTRATOR_MOBILE_BASE_URL Defaults to ORCHESTRATOR_MOBILE_BASE_URL
  .env / .env.local            Auto-loaded from the orchestrator project root
EOF
}

require_api_key() {
    if [[ -z "${API_KEY}" ]]; then
        echo "Error: set MOBILE_GATEWAY_API_KEY or OPENCLAW_API_KEY before calling Orchestrator mobile endpoints." >&2
        exit 1
    fi
}

api_get() {
    local path="$1"
    local url="${BASE_URL}${path}"

    if command -v jq >/dev/null 2>&1; then
        curl -fsS \
            -H "X-OpenClaw-API-Key: ${API_KEY}" \
            "${url}" | jq .
    else
        curl -fsS \
            -H "X-OpenClaw-API-Key: ${API_KEY}" \
            "${url}"
        printf '\n'
    fi
}

main() {
    local command="${1:-}"

    if [[ -z "${command}" ]]; then
        usage
        exit 1
    fi

    require_api_key

    case "${command}" in
        dashboard)
            api_get "/mobile/dashboard"
            ;;
        projects)
            api_get "/mobile/projects"
            ;;
        project-status)
            local project_id="${2:-}"
            [[ -n "${project_id}" ]] || { usage; exit 1; }
            api_get "/mobile/projects/${project_id}/status"
            ;;
        sessions)
            local project_id="${2:-}"
            local status="${3:-}"
            local query=""

            if [[ -n "${project_id}" ]]; then
                query="?project_id=${project_id}"
            fi
            if [[ -n "${status}" ]]; then
                if [[ -n "${query}" ]]; then
                    query="${query}&status=${status}"
                else
                    query="?status=${status}"
                fi
            fi

            api_get "/mobile/sessions${query}"
            ;;
        session-summary)
            local session_id="${2:-}"
            [[ -n "${session_id}" ]] || { usage; exit 1; }
            api_get "/mobile/sessions/${session_id}/summary"
            ;;
        project-tasks)
            local project_id="${2:-}"
            local status="${3:-}"
            [[ -n "${project_id}" ]] || { usage; exit 1; }

            if [[ -n "${status}" ]]; then
                api_get "/mobile/projects/${project_id}/tasks?status=${status}"
            else
                api_get "/mobile/projects/${project_id}/tasks"
            fi
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            echo "Unknown command: ${command}" >&2
            usage
            exit 1
            ;;
    esac
}

main "$@"
