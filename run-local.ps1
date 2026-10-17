$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force user_data\runtime_logs | Out-Null
docker compose -f compose.mongodb.yaml up -d
if ($LASTEXITCODE -ne 0) { throw 'MongoDB startup failed.' }
$env:NEXT_PUBLIC_API_BASE = 'http://127.0.0.1:8200'
$env:NEXT_TELEMETRY_DISABLED = '1'
$env:NEXT_DIST_DIR = '.next-local'
$env:PYTHONIOENCODING = 'utf-8'
if (-not (Test-Path "frontend/$env:NEXT_DIST_DIR/BUILD_ID")) {
    Push-Location frontend
    try { npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' } }
    finally { Pop-Location }
}
if (-not (Get-NetTCPConnection -LocalPort 8200 -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\python.exe" -ArgumentList 'main.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput "$PSScriptRoot\user_data\runtime_logs\backend.out.log" -RedirectStandardError "$PSScriptRoot\user_data\runtime_logs\backend.err.log"
}
if (-not (Get-NetTCPConnection -LocalPort 3300 -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath (Get-Command node.exe).Source -ArgumentList 'node_modules/next/dist/bin/next','start','--hostname','127.0.0.1','--port','3300' -WorkingDirectory "$PSScriptRoot\frontend" -WindowStyle Hidden -RedirectStandardOutput "$PSScriptRoot\user_data\runtime_logs\frontend.out.log" -RedirectStandardError "$PSScriptRoot\user_data\runtime_logs\frontend.err.log"
}
Write-Host 'Open http://127.0.0.1:3300'
