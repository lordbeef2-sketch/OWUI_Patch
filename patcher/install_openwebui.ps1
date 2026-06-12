param(
  [string]$InstallRoot = "",
  [string]$PythonExe = "",
  [string]$OpenWebUiVersion = "",
  [string]$TesseractExe = "",
  [switch]$PatchAfterInstall,
  [switch]$SkipDependencyInstall,
  [switch]$ValidateOnly,
  [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $PatcherRoot 'common.ps1')

function Resolve-InstallRoot([string]$RequestedRoot) {
  if ([string]::IsNullOrWhiteSpace($RequestedRoot)) {
    return (Resolve-Path (Join-Path $PatcherRoot '..')).Path
  }

  $expanded = [Environment]::ExpandEnvironmentVariables($RequestedRoot.Trim())
  if (-not [System.IO.Path]::IsPathRooted($expanded)) {
    $expanded = Join-Path (Get-Location).Path $expanded
  }

  if (-not (Test-Path $expanded)) {
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null
  }

  return (Resolve-Path -LiteralPath $expanded).Path
}

function Test-InstallerPython([string]$Candidate) {
  if ([string]::IsNullOrWhiteSpace($Candidate) -or -not (Test-Path $Candidate)) {
    return $false
  }

  $script = @'
import sys
import venv
import ensurepip
major, minor = sys.version_info[:2]
if major != 3 or minor < 11 or minor > 12:
    raise SystemExit(2)
print(sys.executable)
'@

  try {
    $result = $script | & $Candidate - 2>$null
  } catch {
    return $false
  }
  return ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($result -join "`n").Trim()))
}

function Resolve-InstallerPython([string]$RequestedPython) {
  if (-not [string]::IsNullOrWhiteSpace($RequestedPython)) {
    try {
      $resolved = Resolve-AbsolutePath -CandidatePath $RequestedPython -BaseRoot (Get-Location).Path
    } catch {
      Fail $_.Exception.Message
    }
    if (Test-InstallerPython -Candidate $resolved) {
      return $resolved
    }
    Fail ("Requested Python is not usable for Open WebUI install. Use Python 3.11 or 3.12 with venv/ensurepip: {0}" -f $resolved)
  }

  $candidates = [System.Collections.Generic.List[string]]::new()
  $pyLauncher = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($pyLauncher) {
    foreach ($versionArg in @('-3.11', '-3.12')) {
      $probe = 'import sys; print(sys.executable)'
      $result = $null
      try {
        $result = & $pyLauncher.Source $versionArg -c $probe 2>$null
      } catch {
        $result = $null
      }
      if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($result -join "`n").Trim())) {
        Add-UniqueString -List $candidates -Value (($result -join "`n").Trim())
      }
    }
  }

  foreach ($cmdName in @('python', 'python3')) {
    $cmd = Get-Command $cmdName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd -and -not [string]::IsNullOrWhiteSpace([string]$cmd.Source)) {
      Add-UniqueString -List $candidates -Value ([string]$cmd.Source)
    }
  }

  foreach ($candidate in $candidates) {
    if (Test-InstallerPython -Candidate $candidate) {
      return (Resolve-Path -LiteralPath $candidate).Path
    }
  }

  Fail "No usable Python was found. Install Python 3.11 or 3.12 from python.org, then rerun Install."
}

function Test-PipReady([string]$PythonPath) {
  & $PythonPath -m pip --version 1>$null 2>$null
  return ($LASTEXITCODE -eq 0)
}

function Ensure-Venv([string]$Root, [string]$HostPython) {
  $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
  if (Test-Path $venvPython) {
    Info ("Reusing existing virtual environment: {0}" -f $venvPython)
    return (Resolve-Path -LiteralPath $venvPython).Path
  }

  Info ("Creating virtual environment under {0}" -f (Join-Path $Root '.venv'))
  & $HostPython -m venv (Join-Path $Root '.venv')
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
    Fail "Virtual environment creation failed."
  }

  return (Resolve-Path -LiteralPath $venvPython).Path
}

$ManifestPath = Join-Path $PatcherRoot 'patch_manifest.json'
if (-not (Test-Path $ManifestPath)) {
  Fail ("Missing manifest: {0}" -f $ManifestPath)
}

$Manifest = Get-Content -Path $ManifestPath -Raw | ConvertFrom-Json
$targetVersion = if (-not [string]::IsNullOrWhiteSpace($OpenWebUiVersion)) {
  $OpenWebUiVersion
} elseif ($Manifest.target_version) {
  [string]$Manifest.target_version
} else {
  '0.9.6'
}

$root = Resolve-InstallRoot -RequestedRoot $InstallRoot
$hostPython = Resolve-InstallerPython -RequestedPython $PythonExe

if ($ValidateOnly) {
  Write-Host "VALIDATION_OK"
  Write-Host ("Install root: {0}" -f $root)
  Write-Host ("Host Python: {0}" -f $hostPython)
  Write-Host ("Target Open WebUI version: {0}" -f $targetVersion)
  exit 0
}

$venvPython = Ensure-Venv -Root $root -HostPython $hostPython

Info ("Install root: {0}" -f $root)
Info ("Host Python: {0}" -f $hostPython)
Info ("Open WebUI Python: {0}" -f $venvPython)

if (-not (Test-PipReady -PythonPath $venvPython)) {
  Info "Bootstrapping pip in the Open WebUI virtual environment"
  & $venvPython -m ensurepip --upgrade
  if ($LASTEXITCODE -ne 0 -or -not (Test-PipReady -PythonPath $venvPython)) {
    Fail "pip could not be bootstrapped in the Open WebUI virtual environment."
  }
}

Info "Upgrading pip tooling"
& $venvPython -m pip install --disable-pip-version-check --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
  Fail "pip tooling upgrade failed."
}

Info ("Installing Open WebUI {0}" -f $targetVersion)
& $venvPython -m pip install --disable-pip-version-check --upgrade ("open-webui=={0}" -f $targetVersion)
if ($LASTEXITCODE -ne 0) {
  Fail "Open WebUI installation failed."
}

$configPath = Save-PatcherConfig -PatcherRoot $PatcherRoot -Updates @{
  owui_target = $root
  python_exe = $venvPython
  tesseract_exe = $TesseractExe
}

Ok ("Open WebUI {0} installed into {1}" -f $targetVersion, $root)
Ok ("Saved local patcher defaults to {0}" -f $configPath)

if ($PatchAfterInstall) {
  Info "Applying OWUI patch after install"
  $installArgs = @(
    '-ExecutionPolicy', 'Bypass',
    '-NoProfile',
    '-File', (Join-Path $PatcherRoot 'install.ps1'),
    '-OpenWebUiTarget', $root,
    '-PythonExe', $venvPython,
    '-NonInteractive'
  )

  if (-not [string]::IsNullOrWhiteSpace($TesseractExe)) {
    $installArgs += @('-TesseractExe', $TesseractExe)
  }
  if ($SkipDependencyInstall) {
    $installArgs += '-SkipDependencyInstall'
  }

  & powershell.exe @installArgs
  if ($LASTEXITCODE -ne 0) {
    Fail "Open WebUI installed, but patch application failed."
  }
}

Write-Host ""
Write-Host "Next:" -ForegroundColor White
Write-Host "  1. Use the GUI Launch button or run .\patcher\start_openwebui.ps1." -ForegroundColor White
Write-Host "  2. Browse to http://127.0.0.1:8080 unless you changed host/port." -ForegroundColor White
