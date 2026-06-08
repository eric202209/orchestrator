param(
  [string]$ComposeFile = "docker-compose.windows.yml",
  [string]$ProjectService = "orchestrator",
  [string]$WorkerService = "celery_worker",
  [string]$Model = $env:OLLAMA_AGENT_MODEL
)

$ErrorActionPreference = "Stop"

if (-not $Model) {
  $Model = "qwen3:8b-hybrid"
}

function Invoke-Check {
  param(
    [string]$Name,
    [scriptblock]$Command
  )

  Write-Host "[check] $Name"
  & $Command
  if ($LASTEXITCODE -ne 0) {
    throw "Check failed: $Name"
  }
}

Invoke-Check "orchestrator has node" {
  docker compose -f $ComposeFile exec -T $ProjectService node --version
}

Invoke-Check "celery_worker has node" {
  docker compose -f $ComposeFile exec -T $WorkerService node --version
}

Invoke-Check "container resolves and reaches host Ollama" {
  docker compose -f $ComposeFile exec -T $ProjectService sh -lc "getent hosts host.docker.internal >/dev/null && curl -fsS http://host.docker.internal:11434/api/tags >/tmp/ollama-tags.json"
}

Invoke-Check "required Ollama model exists on host" {
  $match = ollama list | Select-String -SimpleMatch $Model
  if (-not $match) {
    throw "Ollama model not found: $Model"
  }
}

Invoke-Check "database file is writable" {
  docker compose -f $ComposeFile exec -T $ProjectService sh -lc "test -w /app/orchestrator.db"
}

Invoke-Check "runtime directories are writable" {
  docker compose -f $ComposeFile exec -T $ProjectService sh -lc "for d in /app/checkpoints /app/logs /app/projects; do test -d `$d && test -w `$d || exit 1; done"
}

Invoke-Check "API health endpoint responds" {
  curl.exe -fsS http://localhost:8080/health | Out-Null
}

Write-Host "[ok] Windows Docker/Ollama health checks passed"
