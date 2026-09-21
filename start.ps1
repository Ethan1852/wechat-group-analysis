$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
    & $pythonPath -m pip install -r scripts\requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
try {
    $existing = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/' -TimeoutSec 2 -UseBasicParsing
    if ($existing.Content -match 'EVENING REVIEW') {
        Start-Process 'http://127.0.0.1:8765/'
        exit 0
    }
} catch { }
& $pythonPath -X utf8 app.py
