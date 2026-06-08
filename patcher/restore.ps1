param(
  [string]$PythonExe = "",
  [string]$BackupName = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Info([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Cyan }
function Ok([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Green }
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

  $pythonCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($pythonCmd) {
    return $pythonCmd.Source
  }

  Fail "No Python executable found. Pass -PythonExe or place patcher inside an OWUI root that has .venv."
}

function Get-InstallInfo([string]$PythonPath) {
  $script = @'
import json
import pathlib
import sys

import open_webui

package_root = pathlib.Path(open_webui.__file__).resolve().parent
print(json.dumps({
    "python": sys.executable,
    "package_root": str(package_root),
    "site_packages": str(package_root.parent),
}))
'@

  $json = $script | & $PythonPath -
  if ($LASTEXITCODE -ne 0) {
    Fail ("Failed to inspect Open WebUI install using {0}" -f $PythonPath)
  }

  return ($json | ConvertFrom-Json)
}

function Ensure-ParentDirectory([string]$Path) {
  $parent = Split-Path -Parent $Path
  if ($parent -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
}

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $PatcherRoot '..')).Path
$BackupsRoot = Join-Path $PatcherRoot 'backups'

if (-not (Test-Path $BackupsRoot)) {
  Fail ("No backups directory found: {0}" -f $BackupsRoot)
}

if ($BackupName) {
  $BackupDir = Join-Path $BackupsRoot $BackupName
  if (-not (Test-Path $BackupDir)) {
    Fail ("Backup not found: {0}" -f $BackupDir)
  }
} else {
  $BackupDir = Get-ChildItem -Path $BackupsRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1
  if (-not $BackupDir) {
    Fail "No backups are available to restore"
  }
  $BackupDir = $BackupDir.FullName
}

$PythonPath = Resolve-Python -Root $Root -RequestedPython $PythonExe
$InstallInfo = Get-InstallInfo -PythonPath $PythonPath
$SitePackagesRoot = $InstallInfo.site_packages

Info ("Restoring from {0}" -f $BackupDir)

$restored = 0
Get-ChildItem -Path $BackupDir -Recurse -File | ForEach-Object {
  $relative = $_.FullName.Substring($BackupDir.Length).TrimStart('\')
  $destination = Join-Path $SitePackagesRoot $relative
  Ensure-ParentDirectory -Path $destination
  Copy-Item -Path $_.FullName -Destination $destination -Force
  $restored++
}

Ok ("Restored {0} files" -f $restored)
Write-Host "Restart Open WebUI after restoring." -ForegroundColor White
