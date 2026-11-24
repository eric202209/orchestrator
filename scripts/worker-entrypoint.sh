#!/bin/sh
set -eu

# This image is deliberately test-only until the C2 gateway and Host
# capability protocol are implemented. The default path is a validation-only
# path; starting Celery requires an explicit disposable-start opt-in.

fail() {
    echo "orchestrator-worker: configuration error: $*" >&2
    exit 78
}

value_of() {
    printenv "$1" 2>/dev/null || true
}

require_value() {
    required_name="$1"
    required_value=$(value_of "$required_name")
    [ -n "$required_value" ] || fail "$required_name is required"
}

require_exact() {
    exact_name="$1"
    exact_expected="$2"
    exact_value=$(value_of "$exact_name")
    [ "$exact_value" = "$exact_expected" ] \
        || fail "$exact_name must be '$exact_expected'"
}

require_absolute_path() {
    path_name="$1"
    path_value=$(value_of "$path_name")
    case "$path_value" in
        /*) ;;
        *) fail "$path_name must be an absolute path" ;;
    esac
}

ensure_writable_dir() {
    writable_path="$1"
    if [ ! -d "$writable_path" ]; then
        mkdir -p "$writable_path" 2>/dev/null \
            || fail "required writable directory is unavailable: $writable_path"
    fi
    writable_probe="$writable_path/.worker-entrypoint-probe.$$"
    (umask 077 && : > "$writable_probe") 2>/dev/null \
        || fail "required path is not writable: $writable_path"
    rm -f "$writable_probe" \
        || fail "could not remove writable-path probe: $writable_path"
}

assert_not_writable() {
    protected_path="$1"
    protected_probe="$protected_path/.worker-entrypoint-write-probe.$$"
    if (umask 077 && : > "$protected_probe") 2>/dev/null; then
        rm -f "$protected_probe" 2>/dev/null || true
        fail "application release is writable: $protected_path"
    fi
}

assert_redis_database_is_not_default() {
    redis_name="$1"
    redis_url=$(value_of "$redis_name")
    case "$redis_url" in
        redis://*/*|rediss://*/*)
            redis_db=${redis_url##*/}
            redis_db=${redis_db%%\?*}
            case "$redis_db" in
                ""|0|1) fail "$redis_name must use a disposable non-default Redis database" ;;
            esac
            ;;
        *) fail "$redis_name must be a redis:// or rediss:// URL with an explicit database" ;;
    esac
}

assert_backend_is_structured_http() {
    backend_name="$1"
    backend_value=$(value_of "$backend_name")
    [ -z "$backend_value" ] && return 0
    case "$backend_value" in
        direct_ollama|openai_chat_completions) ;;
        *)
            fail "$backend_name='$backend_value' is not an approved structured HTTP backend"
            ;;
    esac
}

require_exact WORKER_ENVIRONMENT c2-c1-test
require_exact WORKER_ISOLATION_MODE disposable
require_exact WORKER_BROKER_NAMESPACE p43-c2-c1-disposable
# WORKER_BRIDGE_MODE: 'disabled' by default. 'c2b-sandbox' is admitted only
# for the single disposable C2 live slice (C2-C3A) with its exact opt-in and
# the isolated strict C2 Launcher socket/key; see the bridge block below.
case "$(value_of WORKER_BRIDGE_MODE)" in
    disabled|c2b-sandbox) ;;
    *) fail "WORKER_BRIDGE_MODE must be 'disabled' or 'c2b-sandbox'" ;;
esac
require_exact EXECUTION_ENABLED false
require_exact CONTROLLED_WORKER_IDENTITY_REQUIRED true
require_exact CONTROLLED_WORKER_NODE_IDENTITY orchestrator-worker-c2-c1
require_exact WORKER_IDENTITY_SOURCE container-start
require_exact PLANNING_PROVIDER direct_openai_compatible
require_exact WORKER_TASK_ALLOWLIST "app.tasks.worker.answer_human_intervention_query,app.tasks.worker.execute_orchestration_task,app.tasks.worker.receive_execution_task_dispatch"

require_value WORKER_INSTANCE_ID
case "$(value_of WORKER_INSTANCE_ID)" in
    c2-c1-*) ;;
    *) fail "WORKER_INSTANCE_ID must be a fresh c2-c1-* runtime identity" ;;
esac
[ "$(value_of WORKER_INSTANCE_ID)" != "$(value_of HOSTNAME)" ] \
    || fail "WORKER_INSTANCE_ID must not reuse the container hostname"

require_value WORKER_QUEUE_NAME
require_exact WORKER_QUEUE_NAME orchestrator-c2-c1-test
require_exact CELERY_QUEUE_NAME orchestrator-c2-c1-test
require_value CELERY_BROKER_URL
require_value CELERY_RESULT_BACKEND
assert_redis_database_is_not_default CELERY_BROKER_URL
assert_redis_database_is_not_default CELERY_RESULT_BACKEND

require_value DATABASE_URL
require_value WORKSPACE_ROOT
require_value RUNTIME_ROOT
require_value CHECKPOINT_DIR
require_value WORKER_DATABASE_DIR
require_value WORKER_RUNTIME_ROOT
require_value WORKER_CHECKPOINT_DIR
require_value WORKER_IDENTITY_ROOT
require_value WORKER_SOURCE_ROOT
require_value WORKER_CANDIDATE_PIN_PATH
require_value WORKER_CANDIDATE_PIN_PROTECTED_ROOT
require_absolute_path WORKER_APP_ROOT
require_absolute_path WORKER_DATABASE_DIR
require_absolute_path WORKER_RUNTIME_ROOT
require_absolute_path WORKER_CHECKPOINT_DIR
require_absolute_path WORKER_IDENTITY_ROOT
require_absolute_path WORKER_SOURCE_ROOT
require_absolute_path WORKSPACE_ROOT
require_absolute_path RUNTIME_ROOT
require_absolute_path CHECKPOINT_DIR

app_root=$(value_of WORKER_APP_ROOT)
database_dir=$(value_of WORKER_DATABASE_DIR)
runtime_root=$(value_of WORKER_RUNTIME_ROOT)
checkpoint_dir=$(value_of WORKER_CHECKPOINT_DIR)
identity_root=$(value_of WORKER_IDENTITY_ROOT)
workspace_root=$(value_of WORKSPACE_ROOT)

[ "$(value_of WORKER_SOURCE_ROOT)" = "$app_root" ] \
    || fail "WORKER_SOURCE_ROOT must equal the immutable application root"
[ "$(value_of DATABASE_URL)" = "sqlite:///$database_dir/orchestrator.db" ] \
    || fail "DATABASE_URL must point to the Worker-owned SQLite database"
[ "$(value_of RUNTIME_ROOT)" = "$runtime_root" ] \
    || fail "RUNTIME_ROOT must equal WORKER_RUNTIME_ROOT"
[ "$(value_of CHECKPOINT_DIR)" = "$checkpoint_dir" ] \
    || fail "CHECKPOINT_DIR must equal WORKER_CHECKPOINT_DIR"
[ "$(value_of WORKER_CANDIDATE_PIN_PROTECTED_ROOT)" = "$identity_root" ] \
    || fail "Worker identity protection root is not the Worker identity mount"
case "$(value_of WORKER_CANDIDATE_PIN_PATH)" in
    "$identity_root"/*) ;;
    *) fail "Worker candidate pin must remain inside the Worker identity mount" ;;
esac

assert_disjoint_path() {
    left_path="$1"
    right_path="$2"
    case "$right_path" in
        "$left_path"|"$left_path"/*)
            fail "Worker authority paths overlap: $left_path and $right_path"
            ;;
    esac
    case "$left_path" in
        "$right_path"|"$right_path"/*)
            fail "Worker authority paths overlap: $left_path and $right_path"
            ;;
    esac
}

for authority_path in \
    "$database_dir" \
    "$runtime_root" \
    "$checkpoint_dir" \
    "$identity_root" \
    "$workspace_root"
do
    assert_disjoint_path "$app_root" "$authority_path"
done
assert_disjoint_path "$database_dir" "$runtime_root"
assert_disjoint_path "$database_dir" "$checkpoint_dir"
assert_disjoint_path "$database_dir" "$identity_root"
assert_disjoint_path "$database_dir" "$workspace_root"
assert_disjoint_path "$runtime_root" "$checkpoint_dir"
assert_disjoint_path "$runtime_root" "$identity_root"
assert_disjoint_path "$runtime_root" "$workspace_root"
assert_disjoint_path "$checkpoint_dir" "$identity_root"
assert_disjoint_path "$checkpoint_dir" "$workspace_root"
assert_disjoint_path "$identity_root" "$workspace_root"

worker_env_file=$(value_of WORKER_ENV_FILE)
case "$worker_env_file" in
    ""|/etc/orchestrator-worker/worker.env) ;;
    *) fail "WORKER_ENV_FILE must be the curated Worker env file, never the project .env" ;;
esac

for forbidden_name in \
    SECRET_KEY \
    GITHUB_TOKEN \
    GITHUB_APP_PRIVATE_KEY \
    MOBILE_GATEWAY_API_KEY \
    ADMIN_EMAILS \
    LANGFUSE_PUBLIC_KEY \
    LANGFUSE_SECRET_KEY \
    OPENCLAW_API_KEY \
    OPENCLAW_CLI_PATH \
    OPENCLAW_CLI_ARGS \
    OPENCLAW_CONFIG_PATH \
    OPENCLAW_STATE_DIR \
    DOCKER_HOST \
    HOST_BRIDGE_SOCKET \
    HOST_BRIDGE_KEY_FILE \
    MODEL_WORKSPACE_ROOT \
    WORKER_MODEL_WORKSPACE_ROOT
do
    [ -z "$(value_of "$forbidden_name")" ] \
        || fail "$forbidden_name is not part of the Worker credential allowlist"
done

require_value AGENT_BACKEND
assert_backend_is_structured_http AGENT_BACKEND
assert_backend_is_structured_http AGENT_SECONDARY_BACKEND
assert_backend_is_structured_http PLANNING_BACKEND
assert_backend_is_structured_http EXECUTION_BACKEND
assert_backend_is_structured_http DEBUG_REPAIR_BACKEND
assert_backend_is_structured_http REPAIR_BACKEND
assert_backend_is_structured_http COMPLETION_REPAIR_BACKEND

[ -d "$app_root" ] || fail "application release is unavailable: $app_root"
[ ! -e "$app_root/.env" ] || fail "full .env is present in the Worker application release"
for env_candidate in "$app_root"/.env.*; do
    [ -e "$env_candidate" ] || continue
    [ "$(basename "$env_candidate")" = ".env.example" ] \
        || fail "an environment file is present in the Worker application release"
done
assert_not_writable "$app_root"

c2_socket=/run/p43-sandbox-bridge-c2/launcher.sock
c2_key=/run/secrets/p43-sandbox-bridge-c2.key
if [ "$(value_of WORKER_BRIDGE_MODE)" = "disabled" ]; then
    for bridge_name in \
        SANDBOX_BRIDGE_SOCKET \
        SANDBOX_BRIDGE_KEY_FILE \
        C2_DISPOSABLE_EXECUTION_ADMISSION
    do
        [ -z "$(value_of "$bridge_name")" ] \
            || fail "$bridge_name is not part of the Worker credential allowlist"
    done
    [ ! -e "$c2_socket" ] && [ ! -e "$c2_key" ] \
        || fail "C2 Launcher material is visible while the bridge is disabled"
else
    require_exact C2_DISPOSABLE_EXECUTION_ADMISSION c2-c3-single-sandbox-slice
    require_exact SANDBOX_BRIDGE_SOCKET "$c2_socket"
    require_exact SANDBOX_BRIDGE_KEY_FILE "$c2_key"
    require_exact WORKER_CONCURRENCY 1
    [ -S "$c2_socket" ] || fail "C2 Launcher socket is not mounted: $c2_socket"
    [ -f "$c2_key" ] && [ ! -L "$c2_key" ] \
        || fail "C2 Worker bridge key is not mounted: $c2_key"
    [ "$(stat -c '%u' "$c2_key")" = "$(id -u)" ] \
        && case "$(stat -c '%a' "$c2_key")" in 400|600) true ;; *) false ;; esac \
        || fail "C2 Worker bridge key must be owner-only and owned by the Worker UID"
fi

# These paths are intentionally checked even though they are not configurable
# by the foundation contract. The deployed C1B Launcher socket/key belong to
# ai-agent and are never Worker inputs, in any mode.
for forbidden_socket in \
    /var/run/docker.sock \
    /run/orchestrator/host-launcher.sock \
    /run/orchestrator/bridge.sock \
    /run/host-launcher.sock \
    /run/p43-sandbox-bridge/launcher.sock \
    /run/secrets/p43-sandbox-bridge.key
do
    [ ! -e "$forbidden_socket" ] \
        || fail "unapproved privileged socket is visible: $forbidden_socket"
done

ensure_writable_dir "$database_dir"
ensure_writable_dir "$runtime_root"
ensure_writable_dir "$checkpoint_dir"
ensure_writable_dir "$identity_root"
ensure_writable_dir "$workspace_root"

if [ "$(value_of WORKER_ENTRYPOINT_CHECK_ONLY)" = "1" ]; then
    echo "orchestrator-worker: foundation contract accepted (validation only; uid=$(id -u))"
    exit 0
fi

[ "$(value_of WORKER_ALLOW_DISPOSABLE_START)" = "1" ] \
    || fail "Celery start is disabled; set WORKER_ALLOW_DISPOSABLE_START=1 only for a disposable test"

worker_concurrency=$(value_of WORKER_CONCURRENCY)
worker_concurrency=${worker_concurrency:-1}
case "$worker_concurrency" in
    ''|*[!0-9]*) fail "WORKER_CONCURRENCY must be numeric" ;;
esac

exec celery -A app.celery_app worker \
    --queues="$(value_of WORKER_QUEUE_NAME)" \
    --concurrency="$worker_concurrency" \
    --loglevel="${WORKER_LOGLEVEL:-INFO}" \
    --hostname="$(value_of CONTROLLED_WORKER_NODE_IDENTITY)@$(value_of WORKER_INSTANCE_ID)"
