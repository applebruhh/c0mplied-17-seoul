# BlockBucks / VaultPay demo launcher for a Windows laptop.
#
#   powershell -ExecutionPolicy Bypass -File scripts\start-demo.ps1            # mock engine
#   powershell -ExecutionPolicy Bypass -File scripts\start-demo.ps1 -External `
#       -EngineUrl https://<engine-host> -HmacSecret <test-key>                 # Stockbucks test engine
#
# It installs dependencies on first run, builds the frontend, forwards port 8000 to every
# USB-connected Android phone (Galaxy reader + customer phone) and starts the server.
param(
    [switch]$External,
    [string]$EngineUrl = "",
    [string]$HmacSecret = "",
    [string]$Database = "",
    [switch]$Lan,          # also listen on the Wi-Fi address so a phone can open http://<laptop-ip>:8000/app
    [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating Python virtual environment (.venv)..." -ForegroundColor Cyan
    py -3 -m venv .venv
    & $python -m pip install --upgrade pip
    & $python -m pip install -e ".\backend[dev]"
}

if (-not $SkipBuild) {
    if (-not (Test-Path "frontend\node_modules")) { npm --prefix frontend ci }
    npm --prefix frontend run build
}

$adb = Get-Command adb -ErrorAction SilentlyContinue
if ($adb) {
    $devices = & adb devices | Select-String "`tdevice$" | ForEach-Object { ($_ -split "`t")[0] }
    if (-not $devices) { Write-Host "No Android device found. Enable USB debugging and accept the prompt on the phone." -ForegroundColor Yellow }
    foreach ($serial in $devices) {
        & adb -s $serial reverse tcp:8000 tcp:8000 | Out-Null
        Write-Host "Forwarded localhost:8000 to $serial" -ForegroundColor Green
    }
} else {
    Write-Host "adb not found. Install Android Platform Tools and add it to PATH for the Galaxy reader." -ForegroundColor Yellow
}

if ($External) {
    if (-not $EngineUrl -or -not $HmacSecret) { throw "-External needs -EngineUrl and -HmacSecret from the engine owner." }
    $env:PAYMENT_ENGINE_MODE = "external"
    $env:PAYMENT_ENGINE_BASE_URL = $EngineUrl
    $env:SB_POS_HMAC_SECRET = $HmacSecret
    if (-not $Database) { $Database = "data\vaultpay-external.sqlite3" }
} else {
    $env:PAYMENT_ENGINE_MODE = "mock"
}
if ($Database) { $env:VAULTPAY_DATABASE_PATH = $Database }

$bind = "127.0.0.1"
if ($Lan) {
    $bind = "0.0.0.0"
    $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -in "Dhcp", "Manual" -and $_.IPAddress -notlike "169.*" } | Select-Object -First 1).IPAddress
    Write-Host "LAN mode: customer phone on the same Wi-Fi can open http://${ip}:8000/app (allow Python through Windows Firewall)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Laptop  POS      http://127.0.0.1:8000/pos" -ForegroundColor Green
Write-Host "  Laptop  Ops      http://127.0.0.1:8000/ops" -ForegroundColor Green
Write-Host "  Galaxy  reader   http://localhost:8000/terminal   (Chrome, NFC on)" -ForegroundColor Green
Write-Host "  Phone   app      http://localhost:8000/app" -ForegroundColor Green
Write-Host ""
Set-Location (Join-Path $root "backend")
& $python -m uvicorn app.main:app --host $bind --port 8000
