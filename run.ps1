<#
    Launcher for vyctl.

    Creates a local .venv on first run, installs the dependencies into it, then starts
    the app.

    Usage:
      .\run.ps1                         start normally
      .\run.ps1 -Debug                  also write vyctl.log next to config.json
      .\run.ps1 -DebugRaw               ...plus every byte the consoles emit
                                        (contains your session text -- use briefly)
      .\run.ps1 -Config D:\other\config.json
#>
param(
    [string]$Config = "",
    [switch]$Debug,
    [switch]$DebugRaw
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    py -3 -m venv (Join-Path $root ".venv")
    & $python -m pip install --upgrade pip | Out-Null
    & $python -m pip install -r (Join-Path $root "requirements.txt")
}

# Build the argument list rather than branching per combination.
$arguments = @("-m", "vyctl")
if ($Config)   { $arguments += @("--config", $Config) }
if ($DebugRaw) { $arguments += "--debug-raw" }
elseif ($Debug){ $arguments += "--debug" }

if ($Debug -or $DebugRaw) {
    Write-Host "Debug log: $(Join-Path $root 'vyctl.log')" -ForegroundColor Cyan
}

Push-Location $root
try {
    & $python @arguments
} finally {
    Pop-Location
}
