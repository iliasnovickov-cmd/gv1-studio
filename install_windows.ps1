[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $path = (& py -3.11 -c "import sys;print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $path) { return $path.Trim() }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $path = (& python -c "import sys;assert sys.version_info >= (3,11);print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $path) { return $path.Trim() }
    }
    return $null
}

function Add-WinGetFfmpegToPath {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { return $true }
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft/WinGet/Packages'
    if (Test-Path $packages) {
        $exe = Get-ChildItem $packages -Filter ffmpeg.exe -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($exe) { $env:PATH = $exe.DirectoryName + ';' + $env:PATH; return $true }
    }
    return $false
}

Write-Host 'GV1 Studio - local helper setup' -ForegroundColor Red
$BasePython = Find-Python
if (-not $BasePython) { throw 'Python 3.11+ is required. Install it with: winget install Python.Python.3.11' }
if (-not (Add-WinGetFfmpegToPath)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw 'FFmpeg was not found. Install FFmpeg and run this script again.' }
    Write-Host 'Installing FFmpeg...' -ForegroundColor Yellow
    winget install --id Gyan.FFmpeg --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0 -or -not (Add-WinGetFfmpegToPath)) { throw 'FFmpeg was installed but PATH is not refreshed. Run this script again.' }
}
if (-not (Test-Path '.venv/Scripts/python.exe')) {
    Write-Host 'Creating an isolated Python environment...'
    & $BasePython -m venv .venv
}
$Python = Join-Path $Root '.venv/Scripts/python.exe'
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv pip install --python $Python -r helper/requirements.txt
} else {
    & $Python -m pip install --disable-pip-version-check -r helper/requirements.txt
}
& $Python -c "import fastapi,httpx,uvicorn,yt_dlp;print('Python dependencies: OK')"
ffmpeg -hide_banner -version | Select-Object -First 1
Write-Host ''
Write-Host 'Setup complete. Run start_windows.bat' -ForegroundColor Green
