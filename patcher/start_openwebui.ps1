param(
  [string]$PythonExe = "",
  [string]$Host = "127.0.0.1",
  [int]$Port = 8080,
  [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Info([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Cyan }
function Fail([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Red; exit 1 }

function Resolve-Python([string]$Root, [string]$RequestedPython) {
  if ($RequestedPython) {
    if (-not (Test-Path $RequestedPython)) {
      Fail ("Python executable not found: {0}" -f $RequestedPython)
    }
    return (Resolve-Path $RequestedPython).Path
  }

  $localVenvPython = Join-Path $Root ".venv\Scripts\python.exe"
  if (Test-Path $localVenvPython) {
    return (Resolve-Path $localVenvPython).Path
  }

  Fail "This helper expects a local OWUI venv or an explicit -PythonExe."
}

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $PatcherRoot '..')).Path
$PythonPath = Resolve-Python -Root $Root -RequestedPython $PythonExe
$ScriptsDir = Split-Path -Parent $PythonPath
$OpenWebUiExe = Join-Path $ScriptsDir 'open-webui.exe'

if (-not (Test-Path $OpenWebUiExe)) {
  Fail ("open-webui.exe not found next to Python: {0}" -f $OpenWebUiExe)
}

if ($ValidateOnly) {
  Write-Host "VALIDATION_OK"
  Write-Host ("Python: {0}" -f $PythonPath)
  Write-Host ("Open WebUI launcher: {0}" -f $OpenWebUiExe)
  Write-Host ("Host: {0}" -f $Host)
  Write-Host ("Port: {0}" -f $Port)
  exit 0
}

$env:ENABLE_OAUTH_PERSISTENT_CONFIG = "true"
$env:PDF_EXTRACT_IMAGES = "true"

Info ("Starting patched Open WebUI on {0}:{1}" -f $Host, $Port)
& $OpenWebUiExe serve --host $Host --port $Port
