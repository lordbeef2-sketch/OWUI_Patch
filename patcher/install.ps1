param(
  [string]$OpenWebUiTarget = "",
  [string]$PythonExe = "",
  [string]$TesseractExe = "",
  [switch]$SkipDependencyInstall,
  [switch]$SkipVersionCheck,
  [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $PatcherRoot 'common.ps1')

function Ensure-ParentDirectory([string]$Path) {
  $parent = Split-Path -Parent $Path
  if ($parent -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
}

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
$ConfirmOwuiTarget = $CanPrompt -and (-not $OpenWebUiTarget) -and (-not $PythonExe)
$ConfirmTesseract = $CanPrompt -and (-not $TesseractExe)

$ResolvedOwui = Resolve-OpenWebUiTarget `
  -PatcherRoot $PatcherRoot `
  -RequestedTarget $OpenWebUiTarget `
  -RequestedPython $PythonExe `
  -SavedConfig $SavedConfig `
  -AllowPrompt:$CanPrompt `
  -PromptForConfirmation:$ConfirmOwuiTarget

$InstallInfo = $ResolvedOwui.InstallInfo
$PythonPath = $ResolvedOwui.PythonPath
$SitePackagesRoot = $InstallInfo.site_packages
$DetectedVersion = if ($null -ne $InstallInfo.version -and [string]$InstallInfo.version -ne "") { [string]$InstallInfo.version } else { "<unknown>" }
$TesseractPath = Resolve-TesseractPath `
  -PatcherRoot $PatcherRoot `
  -RequestedPath $TesseractExe `
  -SavedConfig $SavedConfig `
  -AllowPrompt:$CanPrompt `
  -PromptForConfirmation:$ConfirmTesseract `
  -Optional
$ConfigOwuiTarget = if ($OpenWebUiTarget) {
  $OpenWebUiTarget
} elseif ($PythonExe) {
  $PythonExe
} else {
  $ResolvedOwui.SelectedInput
}

Info ("Using Python: {0}" -f $InstallInfo.python)
Info ("Detected Open WebUI package root: {0}" -f $InstallInfo.package_root)
Info ("Detected Open WebUI version: {0}" -f $DetectedVersion)
if ($TesseractPath) {
  Info ("Using Tesseract: {0}" -f $TesseractPath)
} else {
  Warn "Tesseract was skipped or not found. OCR stays unavailable until TESSERACT_CMD is configured at runtime."
}

if (-not $SkipVersionCheck -and $Manifest.target_version -and ($InstallInfo.version -ne $Manifest.target_version)) {
  $message = "This patcher targets Open WebUI {0}, but found {1}." -f $Manifest.target_version, $InstallInfo.version
  if ($CanPrompt -and (Read-YesNoPrompt -Prompt ($message + " Continue anyway?") -DefaultValue:$false)) {
    Warn "Continuing with version check bypassed for this run."
  } else {
    Fail ($message + " Use -SkipVersionCheck only if you already validated the file layout.")
  }
}

$InstallDependencies = -not $SkipDependencyInstall
if (-not $SkipDependencyInstall -and $CanPrompt) {
  $InstallDependencies = Read-YesNoPrompt -Prompt "Install or update Python OCR dependencies now?" -DefaultValue:$true
}

if ($InstallDependencies -and $Manifest.dependencies.Count -gt 0) {
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

  if ((Get-FileHashHex -Path $src) -ne (Get-FileHashHex -Path $dst)) {
    $mismatches += $entry.target
  }
}

if ($mismatches.Count -gt 0) {
  $list = ($mismatches | ForEach-Object { " - $_" }) -join "`n"
  Fail ("Patch verification failed. Mismatched files:`n{0}" -f $list)
}

$configPath = Save-PatcherConfig -PatcherRoot $PatcherRoot -Updates @{
  owui_target = $ConfigOwuiTarget
  python_exe = $PythonPath
  tesseract_exe = $TesseractPath
}

Ok ("Patched {0} files" -f $copied)
Ok ("Backup created at {0}" -f $backupDir)
Ok ("Saved local patcher defaults to {0}" -f $configPath)
Write-Host ""
Write-Host "Next:" -ForegroundColor White
Write-Host "  1. Restart Open WebUI." -ForegroundColor White
Write-Host "  2. Hard refresh the browser." -ForegroundColor White
Write-Host "  3. Launch through start_openwebui.ps1 if you want the patcher to set ENABLE_OAUTH_PERSISTENT_CONFIG, PDF_EXTRACT_IMAGES, and TESSERACT_CMD for you." -ForegroundColor White
if (-not $TesseractPath) {
  Write-Host "  4. Set TESSERACT_CMD before starting OWUI if you want OCR enabled." -ForegroundColor White
}
