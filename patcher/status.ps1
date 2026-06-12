param(
  [string]$PythonExe = "",
  [switch]$SkipVersionCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Info([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Cyan }
function Ok([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Green }
function Warn([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Yellow }
function Fail([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Red; exit 1 }

function Resolve-Tesseract {
  if ($env:TESSERACT_CMD -and (Test-Path $env:TESSERACT_CMD)) {
    return (Resolve-Path $env:TESSERACT_CMD).Path
  }

  $tesseractCmd = Get-Command tesseract -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($tesseractCmd) {
    return $tesseractCmd.Source
  }

  $commonPaths = @(
    "C:\Program Files\Tesseract-OCR\tesseract.exe",
    "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"
  )
  foreach ($candidate in $commonPaths) {
    if (Test-Path $candidate) {
      return (Resolve-Path $candidate).Path
    }
  }

  return $null
}

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
$TesseractPath = Resolve-Tesseract
$DetectedVersion = if ($null -ne $InstallInfo.version -and [string]$InstallInfo.version -ne "") { [string]$InstallInfo.version } else { "<unknown>" }

Write-Host ("Python: {0}" -f $InstallInfo.python)
Write-Host ("Open WebUI package root: {0}" -f $InstallInfo.package_root)
Write-Host ("Open WebUI version: {0}" -f $DetectedVersion)
if ($TesseractPath) {
  Write-Host ("Tesseract: {0}" -f $TesseractPath)
} else {
  Warn "Tesseract binary not found on PATH and TESSERACT_CMD is not set."
}

if (-not $SkipVersionCheck -and $Manifest.target_version -and ($InstallInfo.version -ne $Manifest.target_version)) {
  Warn ("Version mismatch. Expected {0}, found {1}" -f $Manifest.target_version, $InstallInfo.version)
}

$missing = @()
$outdated = @()
$okFiles = 0

foreach ($entry in $Manifest.files) {
  $src = Join-Path $PatcherRoot $entry.source
  $dst = Join-Path $SitePackagesRoot $entry.target

  if (-not (Test-Path $src)) {
    $missing += ("payload missing: {0}" -f $entry.source)
    continue
  }

  if (-not (Test-Path $dst)) {
    $missing += ("target missing: {0}" -f $entry.target)
    continue
  }

  if ((Get-FileHashHex $src) -ne (Get-FileHashHex $dst)) {
    $outdated += $entry.target
  } else {
    $okFiles++
  }
}

$checkFailures = @()
foreach ($check in $Manifest.contains_checks) {
  $dst = Join-Path $SitePackagesRoot $check.target
  if (-not (Test-Path $dst)) {
    $checkFailures += ("missing for check: {0}" -f $check.target)
    continue
  }

  $text = Get-Content -Path $dst -Raw
  if (-not $text.Contains([string]$check.needle)) {
    $checkFailures += ("{0}: missing text in {1}" -f $check.label, $check.target)
  }
}

Write-Host ("Validated files: {0}/{1}" -f $okFiles, $Manifest.files.Count)
if ($missing.Count -gt 0) {
  Warn "Missing items:"
  $missing | ForEach-Object { Write-Host (" - {0}" -f $_) }
}
if ($outdated.Count -gt 0) {
  Warn "Outdated or modified targets:"
  $outdated | ForEach-Object { Write-Host (" - {0}" -f $_) }
}
if ($checkFailures.Count -gt 0) {
  Warn "Validation checks failing:"
  $checkFailures | ForEach-Object { Write-Host (" - {0}" -f $_) }
}

if ($missing.Count -eq 0 -and $outdated.Count -eq 0 -and $checkFailures.Count -eq 0) {
  Ok "OWUI patch validation PASS"
  exit 0
}

Fail "OWUI patch validation FAILED"
