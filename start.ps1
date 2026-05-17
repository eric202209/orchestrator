# start.ps1

Write-Host "Starting Docker backend..." -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoExit", "-Command", `
  "cd '$PWD'; docker compose -f docker-compose.windows.yml up"

Start-Sleep -Seconds 5

Write-Host "Starting frontend..." -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoExit", "-Command", `
  "cd '$PWD\frontend'; `$env:VITE_API_URL='http://localhost:8080/api/v1'; pnpm dev"

Write-Host ""
Write-Host "Services starting up:" -ForegroundColor Cyan
Write-Host "  Backend API : http://localhost:8080/docs" -ForegroundColor White
Write-Host "  Frontend    : http://localhost:3000" -ForegroundColor White
Write-Host "  Qdrant      : http://localhost:6333/dashboard" -ForegroundColor White