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

function Get-DependencyImportName([string]$PackageName) {
  switch ($PackageName.ToLowerInvariant()) {
    'pillow' { return 'PIL' }
    'pymupdf' { return 'fitz' }
    default { return $PackageName }
  }
}

function Get-MissingDependencies([string]$PythonPath, $Dependencies) {
  $pythonScript = @'
import importlib
import json
import os
import sys

payload = json.loads(os.environ["OWUI_PATCHER_DEPENDENCY_PAYLOAD"])
missing = []
for entry in payload:
    module_name = entry["module"]
    try:
        importlib.import_module(module_name)
    except Exception:
        missing.append(entry["package"])
print(json.dumps(missing))
'@

  $payload = @()
  foreach ($dependency in $Dependencies) {
    $payload += @{
      package = [string]$dependency
      module = (Get-DependencyImportName -PackageName ([string]$dependency))
    }
  }

  $jsonPayload = $payload | ConvertTo-Json -Compress
  $previousPayload = $env:OWUI_PATCHER_DEPENDENCY_PAYLOAD
  $env:OWUI_PATCHER_DEPENDENCY_PAYLOAD = [string]$jsonPayload

  try {
    $result = $pythonScript | & $PythonPath - 2>$null
    if ($LASTEXITCODE -ne 0) {
      Warn "Could not verify installed dependency imports ahead of time. Falling back to installer flow."
      return @($Dependencies)
    }
  } finally {
    if ($null -eq $previousPayload) {
      Remove-Item Env:OWUI_PATCHER_DEPENDENCY_PAYLOAD -ErrorAction SilentlyContinue
    } else {
      $env:OWUI_PATCHER_DEPENDENCY_PAYLOAD = $previousPayload
    }
  }

  try {
    $parsed = ($result -join "`n" | ConvertFrom-Json)
    if ($null -eq $parsed) {
      return @()
    }
    return @($parsed)
  } catch {
    Warn "Dependency import check returned unreadable output. Falling back to installer flow."
    return @($Dependencies)
  }
}

function Test-PipAvailable([string]$PythonPath) {
  $pythonScript = @'
import importlib.util
print("1" if importlib.util.find_spec("pip") is not None else "0")
'@

  try {
    $result = $pythonScript | & $PythonPath - 2>$null
  } catch {
    return $false
  }

  if ($LASTEXITCODE -eq 0 -and (($result -join "`n").Trim() -eq '1')) {
    return $true
  }

  return $false
}

function Try-BootstrapPip([string]$PythonPath) {
  Warn "pip is not available in the selected Python environment. Attempting to bootstrap it with ensurepip."
  try {
    & $PythonPath -m ensurepip --upgrade 1>$null 2>$null
  } catch {
  }

  if (Test-PipAvailable -PythonPath $PythonPath) {
    return $true
  }

  return $false
}

function Resolve-UvCommand() {
  $uvCmd = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($uvCmd -and -not [string]::IsNullOrWhiteSpace([string]$uvCmd.Source)) {
    return [string]$uvCmd.Source
  }

  return $null
}

function Resolve-HostPipPython([string]$TargetPythonPath) {
  $candidates = @()

  $pythonCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($pythonCmd) {
    $candidates += $pythonCmd.Source
  }

  $pyCmd = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($pyCmd) {
    $candidates += $pyCmd.Source
  }

  foreach ($candidate in $candidates | Select-Object -Unique) {
    if (-not $candidate) {
      continue
    }

    if ($candidate -eq $TargetPythonPath) {
      continue
    }

    if ((Split-Path -Leaf $candidate).ToLowerInvariant() -eq 'py.exe') {
      & $candidate -m pip --version 1>$null 2>$null
    } else {
      & $candidate -m pip --version 1>$null 2>$null
    }

    if ($LASTEXITCODE -eq 0) {
      return $candidate
    }
  }

  return $null
}

function Install-PatcherDependencies([string]$TargetPythonPath, [string]$SitePackagesPath, $Dependencies) {
  if (Test-PipAvailable -PythonPath $TargetPythonPath) {
    & $TargetPythonPath -m pip install --disable-pip-version-check @($Dependencies)
    return $LASTEXITCODE
  }

  $uvPath = Resolve-UvCommand
  if ($uvPath) {
    Info ("Using uv to install into {0}" -f $TargetPythonPath)
    & $uvPath pip install --python $TargetPythonPath @($Dependencies)
    if ($LASTEXITCODE -eq 0) {
      return 0
    }

    Warn "uv dependency install failed. Trying other installation methods."
  }

  if (Try-BootstrapPip -PythonPath $TargetPythonPath) {
    & $TargetPythonPath -m pip install --disable-pip-version-check @($Dependencies)
    return $LASTEXITCODE
  }

  $hostPipPython = Resolve-HostPipPython -TargetPythonPath $TargetPythonPath
  if (-not $hostPipPython) {
    Fail "Dependency installation failed because no usable pip or uv was found. Install pip on a host Python, install uv, or rerun with -SkipDependencyInstall."
  }

  Warn "ensurepip could not provision pip in the selected environment. Falling back to a host Python pip if one is available."
  Info ("Using host pip from {0} to install into {1}" -f $hostPipPython, $SitePackagesPath)
  & $hostPipPython -m pip install --disable-pip-version-check --upgrade --target $SitePackagesPath @($Dependencies)
  return $LASTEXITCODE
}

$Root = (Resolve-Path (Join-Path $PatcherRoot '..')).Path
$ManifestPath = Join-Path $PatcherRoot 'patch_manifest.json'

if (-not (Test-Path $ManifestPath)) {
  Fail ("Missing manifest: {0}" -f $ManifestPath)
}

$Manifest = Get-Content -Path $ManifestPath -Raw | ConvertFrom-Json
$SavedConfig = Load-PatcherConfig -PatcherRoot $PatcherRoot
$CanPrompt = (Get-IsInteractiveSession) -and (-not $NonInteractive)
$ConfirmOwuiTarget = $false
$ConfirmTesseract = $false

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
  $missingDependencies = @(Get-MissingDependencies -PythonPath $PythonPath -Dependencies $Manifest.dependencies)
  if ($missingDependencies.Count -eq 0) {
    Ok "Python OCR dependencies are already installed"
  } else {
    Info ("Installing patch dependencies: {0}" -f ($missingDependencies -join ', '))
    $dependencyExitCode = Install-PatcherDependencies -TargetPythonPath $PythonPath -SitePackagesPath $SitePackagesRoot -Dependencies $missingDependencies
    if ($dependencyExitCode -ne 0) {
      Fail "Dependency installation failed"
    }

    $remainingMissingDependencies = @(Get-MissingDependencies -PythonPath $PythonPath -Dependencies $missingDependencies)
    if ($remainingMissingDependencies.Count -gt 0) {
      $remainingList = $remainingMissingDependencies -join ', '
      Fail ("Dependency installation completed but these imports are still missing: {0}" -f $remainingList)
    }

    Ok "Python OCR dependencies are installed"
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
