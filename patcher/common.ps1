function Info([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Cyan }
function Ok([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Green }
function Warn([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Yellow }
function Fail([string]$msg) { Write-Host "[patcher] $msg" -ForegroundColor Red; exit 1 }

function Get-IsInteractiveSession {
  try {
    return [Environment]::UserInteractive -and ($Host.Name -ne 'ServerRemoteHost')
  } catch {
    return $false
  }
}

function Get-PatcherConfigPath([string]$PatcherRoot) {
  return Join-Path $PatcherRoot 'local.settings.json'
}

function Load-PatcherConfig([string]$PatcherRoot) {
  $configPath = Get-PatcherConfigPath -PatcherRoot $PatcherRoot
  if (-not (Test-Path $configPath)) {
    return [pscustomobject]@{}
  }

  try {
    $raw = Get-Content -Path $configPath -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
      return [pscustomobject]@{}
    }
    return ($raw | ConvertFrom-Json)
  } catch {
    Warn ("Ignoring unreadable config file: {0}" -f $configPath)
    return [pscustomobject]@{}
  }
}

function Save-PatcherConfig([string]$PatcherRoot, [hashtable]$Updates) {
  $configPath = Get-PatcherConfigPath -PatcherRoot $PatcherRoot
  $merged = [ordered]@{}
  $existing = Load-PatcherConfig -PatcherRoot $PatcherRoot

  foreach ($prop in $existing.PSObject.Properties) {
    $merged[$prop.Name] = $prop.Value
  }

  foreach ($key in $Updates.Keys) {
    $value = $Updates[$key]
    if ($null -eq $value -or ([string]::IsNullOrWhiteSpace([string]$value))) {
      $merged.Remove($key) | Out-Null
      continue
    }
    $merged[$key] = $value
  }

  $merged['updated_at'] = (Get-Date).ToString('o')
  $merged | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
  return $configPath
}

function Get-ConfigValue($Config, [string]$Name) {
  if ($null -eq $Config) {
    return $null
  }

  $property = $Config.PSObject.Properties[$Name]
  if ($null -eq $property) {
    return $null
  }

  return $property.Value
}

function Read-TextPrompt([string]$Prompt, [string]$Default = "", [switch]$AllowBlank) {
  while ($true) {
    $fullPrompt = if ($Default) {
      "{0} [{1}]" -f $Prompt, $Default
    } else {
      $Prompt
    }

    $answer = Read-Host $fullPrompt
    if ([string]::IsNullOrWhiteSpace($answer)) {
      if ($Default) {
        return $Default
      }
      if ($AllowBlank) {
        return ""
      }
      Warn "A value is required."
      continue
    }

    return $answer.Trim()
  }
}

function Read-YesNoPrompt([string]$Prompt, [bool]$DefaultValue) {
  $suffix = if ($DefaultValue) { '[Y/n]' } else { '[y/N]' }
  while ($true) {
    $answer = Read-Host ("{0} {1}" -f $Prompt, $suffix)
    if ([string]::IsNullOrWhiteSpace($answer)) {
      return $DefaultValue
    }

    switch ($answer.Trim().ToLowerInvariant()) {
      'y' { return $true }
      'yes' { return $true }
      'n' { return $false }
      'no' { return $false }
      default { Warn "Enter y or n." }
    }
  }
}

function Resolve-AbsolutePath([string]$CandidatePath, [string]$BaseRoot) {
  if ([string]::IsNullOrWhiteSpace($CandidatePath)) {
    throw "Path is empty."
  }

  $expanded = [Environment]::ExpandEnvironmentVariables($CandidatePath.Trim())
  $pathToCheck = if ([System.IO.Path]::IsPathRooted($expanded)) {
    $expanded
  } else {
    Join-Path $BaseRoot $expanded
  }

  if (-not (Test-Path $pathToCheck)) {
    throw ("Path not found: {0}" -f $CandidatePath)
  }

  return (Resolve-Path $pathToCheck).Path
}

function Resolve-PythonFromTarget([string]$TargetPath, [string]$BaseRoot) {
  $resolved = Resolve-AbsolutePath -CandidatePath $TargetPath -BaseRoot $BaseRoot
  $item = Get-Item -LiteralPath $resolved

  if ($item.PSIsContainer) {
    $probes = @(
      '.venv\Scripts\python.exe',
      'venv\Scripts\python.exe',
      'Scripts\python.exe'
    )
    foreach ($probe in $probes) {
      $candidate = Join-Path $resolved $probe
      if (Test-Path $candidate) {
        return (Resolve-Path $candidate).Path
      }
    }
    throw ("No python.exe was found under {0}. Point to an Open WebUI root with .venv or directly to python.exe." -f $resolved)
  }

  $leaf = (Split-Path -Leaf $resolved).ToLowerInvariant()
  if ($leaf -eq 'python.exe') {
    return $resolved
  }

  if ($leaf -eq 'open-webui.exe') {
    $candidate = Join-Path (Split-Path -Parent $resolved) 'python.exe'
    if (Test-Path $candidate) {
      return (Resolve-Path $candidate).Path
    }
  }

  throw ("Expected a python.exe path or an Open WebUI root folder, but got {0}" -f $resolved)
}

function Get-InstallInfo([string]$PythonPath, [switch]$Quiet) {
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

  $json = $script | & $PythonPath - 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $json) {
    if ($Quiet) {
      return $null
    }
    Fail ("Failed to inspect Open WebUI install using {0}" -f $PythonPath)
  }

  try {
    return (($json -join "`n") | ConvertFrom-Json)
  } catch {
    if ($Quiet) {
      return $null
    }
    Fail ("Open WebUI inspection returned unreadable data for {0}" -f $PythonPath)
  }
}

function Resolve-OpenWebUiTarget(
  [string]$PatcherRoot,
  [string]$RequestedTarget,
  [string]$RequestedPython,
  $SavedConfig,
  [switch]$AllowPrompt,
  [switch]$PromptForConfirmation
) {
  $repoRoot = (Resolve-Path (Join-Path $PatcherRoot '..')).Path
  $candidates = [System.Collections.Generic.List[object]]::new()
  $savedOwuiTarget = Get-ConfigValue -Config $SavedConfig -Name 'owui_target'
  $savedPythonExe = Get-ConfigValue -Config $SavedConfig -Name 'python_exe'

  function Add-TargetCandidate([string]$Value, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
      return
    }
    foreach ($existing in $candidates) {
      if ($existing.Value -eq $Value.Trim()) {
        return
      }
    }
    $candidates.Add([pscustomobject]@{
      Value = $Value.Trim()
      Label = $Label
    })
  }

  Add-TargetCandidate -Value $RequestedTarget -Label 'requested OWUI target'
  Add-TargetCandidate -Value $RequestedPython -Label 'requested Python'
  Add-TargetCandidate -Value $savedOwuiTarget -Label 'saved OWUI target'
  Add-TargetCandidate -Value $savedPythonExe -Label 'saved Python'

  $localRoot = $repoRoot
  if (Test-Path (Join-Path $localRoot '.venv\Scripts\python.exe')) {
    Add-TargetCandidate -Value $localRoot -Label 'local OWUI root'
  }

  $pythonCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($pythonCmd) {
    Add-TargetCandidate -Value $pythonCmd.Source -Label 'python on PATH'
  }

  $resolvedTarget = $null
  $defaultPrompt = ""
  $lastError = "No Open WebUI install was detected."

  foreach ($candidate in $candidates) {
    try {
      $pythonPath = Resolve-PythonFromTarget -TargetPath $candidate.Value -BaseRoot $repoRoot
      $installInfo = Get-InstallInfo -PythonPath $pythonPath -Quiet
      if ($installInfo) {
        $resolvedTarget = [pscustomobject]@{
          SelectedInput = $candidate.Value
          Source = $candidate.Label
          PythonPath = $pythonPath
          InstallInfo = $installInfo
        }
        $defaultPrompt = $candidate.Value
        break
      }
      $lastError = ("Python does not appear to have Open WebUI installed: {0}" -f $pythonPath)
    } catch {
      $lastError = $_.Exception.Message
    }
  }

  if ($resolvedTarget -and -not $PromptForConfirmation) {
    return $resolvedTarget
  }

  if (-not $AllowPrompt) {
    Fail $lastError
  }

  if ($resolvedTarget) {
    Info ("Detected Open WebUI via {0}" -f $resolvedTarget.Source)
  } else {
    Warn $lastError
  }

  while ($true) {
    $inputValue = Read-TextPrompt -Prompt "Open WebUI root folder or python.exe" -Default $defaultPrompt
    try {
      $pythonPath = Resolve-PythonFromTarget -TargetPath $inputValue -BaseRoot $repoRoot
      $installInfo = Get-InstallInfo -PythonPath $pythonPath -Quiet
      if (-not $installInfo) {
        throw ("Open WebUI is not importable from {0}" -f $pythonPath)
      }

      return [pscustomobject]@{
        SelectedInput = $inputValue
        Source = 'prompted input'
        PythonPath = $pythonPath
        InstallInfo = $installInfo
      }
    } catch {
      Warn $_.Exception.Message
    }
  }
}

function Get-AutoTesseractPath {
  if ($env:TESSERACT_CMD -and (Test-Path $env:TESSERACT_CMD)) {
    return (Resolve-Path $env:TESSERACT_CMD).Path
  }

  $tesseractCmd = Get-Command tesseract -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($tesseractCmd) {
    return $tesseractCmd.Source
  }

  $commonPaths = @(
    'C:\Program Files\Tesseract-OCR\tesseract.exe',
    'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe'
  )
  foreach ($candidate in $commonPaths) {
    if (Test-Path $candidate) {
      return (Resolve-Path $candidate).Path
    }
  }

  return $null
}

function Resolve-TesseractPath(
  [string]$PatcherRoot,
  [string]$RequestedPath,
  $SavedConfig,
  [switch]$AllowPrompt,
  [switch]$PromptForConfirmation,
  [switch]$Optional
) {
  $repoRoot = (Resolve-Path (Join-Path $PatcherRoot '..')).Path
  $defaultPath = $null
  $savedTesseractExe = Get-ConfigValue -Config $SavedConfig -Name 'tesseract_exe'

  foreach ($candidate in @($RequestedPath, $savedTesseractExe, (Get-AutoTesseractPath))) {
    if ([string]::IsNullOrWhiteSpace([string]$candidate)) {
      continue
    }
    try {
      $defaultPath = Resolve-AbsolutePath -CandidatePath $candidate -BaseRoot $repoRoot
      break
    } catch {
      continue
    }
  }

  if ($defaultPath -and -not $PromptForConfirmation) {
    return $defaultPath
  }

  if (-not $AllowPrompt) {
    return $defaultPath
  }

  while ($true) {
    $prompt = if ($Optional -and $defaultPath) {
      'Tesseract executable (press Enter to use default, type skip to disable)'
    } elseif ($Optional) {
      'Tesseract executable (optional, press Enter to skip)'
    } else {
      'Tesseract executable'
    }

    $answer = Read-TextPrompt -Prompt $prompt -Default $defaultPath -AllowBlank:$Optional
    if ($Optional -and $answer.Trim().ToLowerInvariant() -eq 'skip') {
      return $null
    }
    if ([string]::IsNullOrWhiteSpace($answer)) {
      return $null
    }

    try {
      $resolved = Resolve-AbsolutePath -CandidatePath $answer -BaseRoot $repoRoot
      $item = Get-Item -LiteralPath $resolved
      if ($item.PSIsContainer) {
        throw "Point to tesseract.exe, not just the folder."
      }
      return $resolved
    } catch {
      Warn $_.Exception.Message
    }
  }
}

function Resolve-SettingValue(
  [string]$Prompt,
  [string]$RequestedValue,
  [string]$SavedValue,
  [string]$FallbackValue,
  [switch]$AllowPrompt,
  [switch]$PromptForConfirmation
) {
  $resolved = if (-not [string]::IsNullOrWhiteSpace($RequestedValue)) {
    $RequestedValue
  } elseif (-not [string]::IsNullOrWhiteSpace($SavedValue)) {
    $SavedValue
  } else {
    $FallbackValue
  }

  if (-not $AllowPrompt -or (-not $PromptForConfirmation -and -not [string]::IsNullOrWhiteSpace($RequestedValue))) {
    return $resolved
  }

  if (-not $PromptForConfirmation -and -not [string]::IsNullOrWhiteSpace($SavedValue)) {
    return $resolved
  }

  return (Read-TextPrompt -Prompt $Prompt -Default $resolved)
}

function Resolve-PortValue(
  [int]$RequestedValue,
  $SavedValue,
  [int]$FallbackValue,
  [switch]$AllowPrompt,
  [switch]$PromptForConfirmation
) {
  $resolved = if ($RequestedValue -gt 0) {
    $RequestedValue
  } elseif ($null -ne $SavedValue -and [int]$SavedValue -gt 0) {
    [int]$SavedValue
  } else {
    $FallbackValue
  }

  if (-not $AllowPrompt -or -not $PromptForConfirmation) {
    return $resolved
  }

  while ($true) {
    $answer = Read-TextPrompt -Prompt 'Open WebUI port' -Default ([string]$resolved)
    if ($answer -as [int]) {
      $port = [int]$answer
      if ($port -ge 1 -and $port -le 65535) {
        return $port
      }
    }
    Warn "Enter a port between 1 and 65535."
  }
}

function Select-BackupDirectory([string]$BackupsRoot, [string]$RequestedBackupName, [switch]$AllowPrompt) {
  if ($RequestedBackupName) {
    $selected = Join-Path $BackupsRoot $RequestedBackupName
    if (-not (Test-Path $selected)) {
      Fail ("Backup not found: {0}" -f $selected)
    }
    return $selected
  }

  $available = Get-ChildItem -Path $BackupsRoot -Directory | Sort-Object Name -Descending
  if (-not $available) {
    Fail "No backups are available to restore"
  }

  if (-not $AllowPrompt) {
    return $available[0].FullName
  }

  Info "Available backups:"
  $available | Select-Object -First 5 | ForEach-Object { Write-Host (" - {0}" -f $_.Name) }
  $defaultName = $available[0].Name
  $selection = Read-TextPrompt -Prompt 'Backup folder name to restore' -Default $defaultName
  $selected = Join-Path $BackupsRoot $selection
  if (-not (Test-Path $selected)) {
    Fail ("Backup not found: {0}" -f $selected)
  }
  return $selected
}
