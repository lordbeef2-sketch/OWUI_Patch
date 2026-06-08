param(
  [string]$PythonExe = "",
  [switch]$SkipDependencyInstall,
  [switch]$SkipVersionCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Info([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Cyan }
function Ok([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Green }
function Warn([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Yellow }
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
import importlib.metadata

import open_webui

package_root = pathlib.Path(open_webui.__file__).resolve().parent
try:
    version = importlib.metadata.version("open-webui")
except Exception:
    version = ""

print(json.dumps({
    "python": sys.executable,
    "package_root": str(package_root),
    "site_packages": str(package_root.parent),
    "version": version,
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

function Get-FileHashHex([string]$Path) {
  return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
}

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $PatcherRoot '..')).Path
$ManifestPath = Join-Path $PatcherRoot 'patch_manifest.json'

if (-not (Test-Path $ManifestPath)) {
  Fail ("Missing manifest: {0}" -f $ManifestPath)
}

$Manifest = Get-Content -Path $ManifestPath -Raw | ConvertFrom-Json
$PythonPath = Resolve-Python -Root $Root -RequestedPython $PythonExe
$InstallInfo = Get-InstallInfo -PythonPath $PythonPath
$SitePackagesRoot = $InstallInfo.site_packages

Info ("Using Python: {0}" -f $InstallInfo.python)
Info ("Detected Open WebUI package root: {0}" -f $InstallInfo.package_root)
Info ("Detected Open WebUI version: {0}" -f ($InstallInfo.version ?? "<unknown>"))

if (-not $SkipVersionCheck -and $Manifest.target_version -and ($InstallInfo.version -ne $Manifest.target_version)) {
  Fail ("This patcher targets Open WebUI {0}, but found {1}. Use -SkipVersionCheck only if you already validated the file layout." -f $Manifest.target_version, $InstallInfo.version)
}

if (-not $SkipDependencyInstall -and $Manifest.dependencies.Count -gt 0) {
  Info "Installing patch dependencies"
  & $PythonPath -m pip install --disable-pip-version-check @($Manifest.dependencies)
  if ($LASTEXITCODE -ne 0) {
    Fail "Dependency installation failed"
  }
} else {
  Warn "Skipping dependency installation"
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDir = Join-Path $PatcherRoot ("backups\{0}-{1}" -f $timestamp, $Manifest.patch_id)
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

$copied = 0
foreach ($entry in $Manifest.files) {
  $src = Join-Path $PatcherRoot $entry.source
  $dst = Join-Path $SitePackagesRoot $entry.target
  $bak = Join-Path $backupDir $entry.target

  if (-not (Test-Path $src)) {
    Fail ("Missing payload file: {0}" -f $src)
  }

  if (-not (Test-Path $dst)) {
    Warn ("Target file missing before patch: {0}" -f $dst)
  } else {
    Ensure-ParentDirectory -Path $bak
    Copy-Item -Path $dst -Destination $bak -Force
  }

  Ensure-ParentDirectory -Path $dst
  Copy-Item -Path $src -Destination $dst -Force
  $copied++
}

$mismatches = @()
foreach ($entry in $Manifest.files) {
  $src = Join-Path $PatcherRoot $entry.source
  $dst = Join-Path $SitePackagesRoot $entry.target

  if (-not (Test-Path $dst)) {
    $mismatches += $entry.target
    continue
  }

  if ((Get-FileHashHex $src) -ne (Get-FileHashHex $dst)) {
    $mismatches += $entry.target
  }
}

if ($mismatches.Count -gt 0) {
  $list = ($mismatches | ForEach-Object { " - $_" }) -join "`n"
  Fail ("Patch verification failed. Mismatched files:`n{0}" -f $list)
}

Ok ("Patched {0} files" -f $copied)
Ok ("Backup created at {0}" -f $backupDir)
Write-Host ""
Write-Host "Next:" -ForegroundColor White
Write-Host "  1. Restart Open WebUI." -ForegroundColor White
Write-Host "  2. Hard refresh the browser." -ForegroundColor White
Write-Host "  3. Set ENABLE_OAUTH_PERSISTENT_CONFIG=true in the OWUI launch environment if you want SSO settings saved from the UI to persist across restarts." -ForegroundColor White
