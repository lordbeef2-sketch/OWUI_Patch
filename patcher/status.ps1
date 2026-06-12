param(
  [string]$OpenWebUiTarget = "",
  [string]$PythonExe = "",
  [string]$TesseractExe = "",
  [switch]$SkipVersionCheck,
  [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $PatcherRoot 'common.ps1')

function Get-FileHashHex([string]$Path) {
  return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
}

$Root = (Resolve-Path (Join-Path $PatcherRoot '..')).Path
$ManifestPath = Join-Path $PatcherRoot 'patch_manifest.json'

if (-not (Test-Path $ManifestPath)) {
  Fail ("Missing manifest: {0}" -f $ManifestPath)
}

$Manifest = Get-Content -Path $ManifestPath -Raw | ConvertFrom-Json
$SavedConfig = Load-PatcherConfig -PatcherRoot $PatcherRoot
$CanPrompt = (Get-IsInteractiveSession) -and (-not $NonInteractive)

$ResolvedOwui = Resolve-OpenWebUiTarget `
  -PatcherRoot $PatcherRoot `
  -RequestedTarget $OpenWebUiTarget `
  -RequestedPython $PythonExe `
  -SavedConfig $SavedConfig `
  -AllowPrompt:$CanPrompt

$InstallInfo = $ResolvedOwui.InstallInfo
$SitePackagesRoot = $InstallInfo.site_packages
$DetectedVersion = if ($null -ne $InstallInfo.version -and [string]$InstallInfo.version -ne "") { [string]$InstallInfo.version } else { "<unknown>" }
$TesseractPath = Resolve-TesseractPath `
  -PatcherRoot $PatcherRoot `
  -RequestedPath $TesseractExe `
  -SavedConfig $SavedConfig `
  -AllowPrompt:$false `
  -Optional

Write-Host ("Python: {0}" -f $InstallInfo.python)
Write-Host ("Open WebUI package root: {0}" -f $InstallInfo.package_root)
Write-Host ("Open WebUI version: {0}" -f $DetectedVersion)
Write-Host ("Config file: {0}" -f (Get-PatcherConfigPath -PatcherRoot $PatcherRoot))
if ($TesseractPath) {
  Write-Host ("Tesseract: {0}" -f $TesseractPath)
} else {
  Warn "Tesseract binary not found and no local override is saved."
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

  if ((Get-FileHashHex -Path $src) -ne (Get-FileHashHex -Path $dst)) {
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
  if ($null -eq $text) {
    $text = ''
  }
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
