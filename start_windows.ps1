[CmdletBinding()]
param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Python = Join-Path $Root '.venv/Scripts/python.exe'
if (-not (Test-Path $Python)) { & (Join-Path $Root 'install_windows.ps1') }
if (-not (Test-Path $Python)) { throw 'Could not create .venv' }

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft/WinGet/Packages'
    $exe = Get-ChildItem $packages -Filter ffmpeg.exe -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($exe) { $env:PATH = $exe.DirectoryName + ';' + $env:PATH }
}
$Url = "http://127.0.0.1:$Port/"
try {
    $health = Invoke-RestMethod -TimeoutSec 1 ($Url + 'api/health')
    if ($health) { Start-Process $Url; Write-Host "Helper is already running: $Url"; exit 0 }
} catch {}
$OpenCommand = "Start-Sleep -Seconds 2; Start-Process '$Url'"
Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-Command',$OpenCommand
Write-Host "GV1 helper: $Url" -ForegroundColor Green
Write-Host 'Keep this window open. Press Ctrl+C to stop.' -ForegroundColor DarkGray
Set-Location (Join-Path $Root 'helper')
& $Python run.py --port $Port
