$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUTF8 = '1'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c 'import sys; assert sys.version_info[:2] == (3, 12), "Install Python 3.12 first"'
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
        & python -m venv .venv
    } else {
        throw 'Install Python 3.12 from https://www.python.org/downloads/ and run this installer again.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python 3.12 environment.' }
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed. Check your network and retry.' }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network and retry.' }
& .\.venv\Scripts\python.exe -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Chromium download failed. Checking installed Chrome/Edge instead.'
}
& .\.venv\Scripts\python.exe tools\check_browser.py
if ($LASTEXITCODE -ne 0) { throw 'Install Chrome/Edge, or retry the Chromium download.' }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Host 'FFmpeg is required for video and audio processing.'
    Write-Host 'Run: winget install --id Gyan.FFmpeg --exact'
    Write-Host 'Then reopen your terminal and run install.bat again.'
    exit 1
}
Write-Host 'Ready. Optional local vision: run install_local_ai.bat (large model download).'
