# ModelLink ProDesk Setup Script
# Run this on your ProDesk in PowerShell as Administrator

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  ModelLink Server Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$INSTALL_DIR = "C:\Modellink"

# Step 1: Install Python if not present
Write-Host "[1/5] Checking Python..." -ForegroundColor Yellow
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    $ver = python --version 2>&1
    Write-Host "  Python found: $ver" -ForegroundColor Green
} else {
    Write-Host "  Installing Python 3.11..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile "$env:TEMP\python-installer.exe"
    Start-Process "$env:TEMP\python-installer.exe" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
    Write-Host "  Python installed. Restart this script after install." -ForegroundColor Green
    exit
}

# Step 2: Install Git if not present
Write-Host "[2/5] Checking Git..." -ForegroundColor Yellow
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    Write-Host "  Git found" -ForegroundColor Green
} else {
    Write-Host "  Installing Git..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://github.com/git-for-windows/git/releases/download/v2.45.2.windows.1/Git-2.45.2-64-bit.exe" -OutFile "$env:TEMP\git-installer.exe"
    Start-Process "$env:TEMP\git-installer.exe" -ArgumentList "/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /COMPONENTS=icons,ext\reg\shellhere,assoc,assoc_sh" -Wait
    Write-Host "  Git installed. Restart this script after install." -ForegroundColor Green
    exit
}

# Step 3: Clone or update the repo
Write-Host "[3/5] Downloading ModelLink..." -ForegroundColor Yellow
if (Test-Path "$INSTALL_DIR\.git") {
    Set-Location $INSTALL_DIR
    git pull origin main
    Write-Host "  Updated to latest version" -ForegroundColor Green
} else {
    git clone https://github.com/Tribbninja/ModelLink.git $INSTALL_DIR
    Set-Location $INSTALL_DIR
    Write-Host "  Downloaded" -ForegroundColor Green
}

# Step 4: Install Python dependencies
Write-Host "[4/5] Installing Python packages..." -ForegroundColor Yellow
python -m venv venv
& "$INSTALL_DIR\venv\Scripts\Activate.ps1"
pip install --upgrade pip
pip install -r requirements.txt
pip install uvicorn
Write-Host "  Done" -ForegroundColor Green

# Step 5: Create .env if it doesn't exist
Write-Host "[5/5] Checking API keys..." -ForegroundColor Yellow
if (-not (Test-Path "$INSTALL_DIR\.env")) {
    @"
# Add your API keys below
# Get Groq key at: https://console.groq.com/keys
# Get Gemini key at: https://aistudio.google.com/apikey

GROQ_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
LANGUAGE=English
"@ | Out-File "$INSTALL_DIR\.env" -Encoding UTF8
    Write-Host "  Created .env - ADD YOUR API KEYS!" -ForegroundColor Red
} else {
    Write-Host "  .env exists" -ForegroundColor Green
}

# Step 6: Install cloudflared (Cloudflare Tunnel)
Write-Host "[6/6] Installing Cloudflare Tunnel..." -ForegroundColor Yellow
$cfPath = "C:\Modellink\cloudflared.exe"
if (-not (Test-Path $cfPath)) {
    Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $cfPath
    Write-Host "  Installed" -ForegroundColor Green
} else {
    Write-Host "  Already installed" -ForegroundColor Green
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Setup Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Edit your API keys: notepad C:\Modellink\.env" -ForegroundColor White
Write-Host "  2. Run: C:\Modellink\start.bat" -ForegroundColor White
Write-Host "  3. Follow the Cloudflare login steps" -ForegroundColor White
Write-Host ""
