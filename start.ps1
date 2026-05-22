param(
  [switch]$Build,
  [switch]$ForceRecreate,
  [switch]$StartOllama,
  [string]$OllamaExe = $env:OLLAMA_EXE
)

# start.ps1

if ($StartOllama) {
  Write-Host "Starting Ollama..." -ForegroundColor Green

  if (-not $OllamaExe) {
    $ollamaCommand = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($ollamaCommand) {
      $OllamaExe = $ollamaCommand.Source
    }
  }

  if (-not $OllamaExe -or -not (Test-Path -LiteralPath $OllamaExe)) {
    Write-Host "  Ollama executable not found. Set OLLAMA_EXE or start Ollama manually." -ForegroundColor Yellow
  } else {
    Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 3

    $env:CUDA_VISIBLE_DEVICES = if ($env:CUDA_VISIBLE_DEVICES) { $env:CUDA_VISIBLE_DEVICES } else { "0" }
    $env:OLLAMA_HOST = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "0.0.0.0" }
    $env:OLLAMA_NUM_PARALLEL = if ($env:OLLAMA_NUM_PARALLEL) { $env:OLLAMA_NUM_PARALLEL } else { "1" }
    $env:OLLAMA_MAX_LOADED_MODELS = if ($env:OLLAMA_MAX_LOADED_MODELS) { $env:OLLAMA_MAX_LOADED_MODELS } else { "1" }

    Start-Process $OllamaExe -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 5
    Write-Host "  Ollama API  : http://localhost:11434" -ForegroundColor White
  }
}

$requiredDirs = @("checkpoints", "logs", "knowledge", "data")
foreach ($dir in $requiredDirs) {
  New-Item -ItemType Directory -Force -Path (Join-Path $PWD $dir) | Out-Null
}

$dbPath = Join-Path $PWD "orchestrator.db"
if (-not (Test-Path -LiteralPath $dbPath)) {
  New-Item -ItemType File -Path $dbPath | Out-Null
}

if (-not $env:WINDOWS_PROJECTS_DIR) {
  $envPath = Join-Path $PWD ".env"
  $envWorkspaceDir = $null
  if (Test-Path -LiteralPath $envPath) {
    $envWorkspaceDir = Get-Content -LiteralPath $envPath |
      Where-Object { $_ -match '^\s*WINDOWS_PROJECTS_DIR\s*=' } |
      Select-Object -Last 1
    if ($envWorkspaceDir) {
      $envWorkspaceDir = ($envWorkspaceDir -replace '^\s*WINDOWS_PROJECTS_DIR\s*=\s*', '').Trim().Trim('"').Trim("'")
    }
  }
  $defaultProjectsDir = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Projects"
  $env:WINDOWS_PROJECTS_DIR = if ($envWorkspaceDir) { $envWorkspaceDir } else { $defaultProjectsDir }
}

Write-Host "Host projects folder: $env:WINDOWS_PROJECTS_DIR" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $env:WINDOWS_PROJECTS_DIR | Out-Null

$composeArgs = @("compose", "-f", "docker-compose.windows.yml", "up")
if ($Build) {
  $composeArgs += "--build"
}
if ($ForceRecreate) {
  $composeArgs += "--force-recreate"
}

$composeCommand = "docker " + ($composeArgs -join " ")

Write-Host "Starting Docker backend..." -ForegroundColor Green
Write-Host "Docker command: $composeCommand" -ForegroundColor DarkGray
Start-Process powershell -ArgumentList "-NoExit", "-Command", `
  "cd '$PWD'; $composeCommand"

Start-Sleep -Seconds 5

Write-Host "Starting frontend..." -ForegroundColor Green
$frontendRunning = (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue) -ne $null
if ($frontendRunning) {
  Write-Host "  Frontend already running on port 3000, skipping launch." -ForegroundColor Yellow
} else {
  Start-Process powershell -ArgumentList "-NoExit", "-Command", `
    "cd '$PWD\frontend'; `$env:VITE_API_URL='http://localhost:8080/api/v1'; pnpm dev"
}

Write-Host ""
Write-Host "Services starting up:" -ForegroundColor Cyan
Write-Host "  Backend API : http://localhost:8080/docs" -ForegroundColor White
Write-Host "  Frontend    : http://localhost:3000" -ForegroundColor White
Write-Host "  Qdrant      : http://localhost:6333/dashboard" -ForegroundColor White
