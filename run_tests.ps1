# AI School — one-shot helper for Windows PowerShell
# Usage:
#   cd C:\aischool\artifacts
#   .\run_tests.ps1              # module tests only
#   .\run_tests.ps1 -Api         # also HTTP tests (start uvicorn in another window first)

param(
  [switch]$Api,
  [string]$DatabaseUrl = "postgresql+psycopg://aischool:aischool@127.0.0.1:5432/aischool",
  [string]$JwtSecret = "dev-secret-change-me-32chars-min!!",
  [string]$ApiBase = "http://127.0.0.1:8080"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$env:DATABASE_URL = $DatabaseUrl
$env:JWT_SECRET = $JwtSecret
$env:API_BASE = $ApiBase

Write-Host "Running python run_tests.py ..." -ForegroundColor Cyan
if ($Api) {
  python run_tests.py --api --base $ApiBase
} else {
  python run_tests.py
}
