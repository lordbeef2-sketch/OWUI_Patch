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


function Test-OpenWebUiPackageRoot([string]$PackageRoot) {
  if ([string]::IsNullOrWhiteSpace($PackageRoot) -or -not (Test-Path $PackageRoot)) {
    return $false
  }

  $item = Get-Item -LiteralPath $PackageRoot
  if (-not $item.PSIsContainer) {
    return $false
  }

  $required = @('config.py', 'main.py')
  foreach ($leaf in $required) {
    if (-not (Test-Path (Join-Path $PackageRoot $leaf))) {
      return $false
    }
  }

  return $true
}

function Get-OpenWebUiVersionFromPackageRoot([string]$PackageRoot) {
  try {
    $sitePackagesRoot = Split-Path -Parent $PackageRoot
    $distInfo = Get-ChildItem -LiteralPath $sitePackagesRoot -Directory -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -like 'open_webui-*.dist-info' -or $_.Name -like 'open-webui-*.dist-info' } |
      Sort-Object Name -Descending |
      Select-Object -First 1

    if ($distInfo) {
      $metadata = Join-Path $distInfo.FullName 'METADATA'
      if (Test-Path $metadata) {
        $line = Get-Content -Path $metadata -ErrorAction SilentlyContinue |
          Where-Object { $_ -match '^Version:\s*(.+)$' } |
          Select-Object -First 1
        if ($line -and $line -match '^Version:\s*(.+)$') {
          return $Matches[1].Trim()
        }
      }
    }
  } catch {
    return ""
  }

  return ""
}

function New-OpenWebUiInstallInfoFromPackageRoot([string]$PackageRoot, [string]$PythonPath) {
  $resolvedPackageRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
  $sitePackagesRoot = Split-Path -Parent $resolvedPackageRoot
  $resolvedPython = ""
  if (-not [string]::IsNullOrWhiteSpace($PythonPath) -and (Test-Path $PythonPath)) {
    $resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
  }

  return [pscustomobject]@{
    python = $resolvedPython
    package_root = $resolvedPackageRoot
    site_packages = $sitePackagesRoot
    version = (Get-OpenWebUiVersionFromPackageRoot -PackageRoot $resolvedPackageRoot)
  }
}

function Add-UniqueString([System.Collections.Generic.List[string]]$List, [string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value)) {
    return
  }

  $trimmed = $Value.Trim()
  foreach ($existing in $List) {
    if ($existing -ieq $trimmed) {
      return
    }
  }
  $List.Add($trimmed) | Out-Null
}

function Get-LocalPythonCandidates([string]$RepoRoot) {
  $items = [System.Collections.Generic.List[string]]::new()
  $direct = @(
    (Join-Path $RepoRoot '.venv\Scripts\python.exe'),
    (Join-Path $RepoRoot 'venv\Scripts\python.exe'),
    (Join-Path $RepoRoot 'env\Scripts\python.exe'),
    (Join-Path $RepoRoot 'Scripts\python.exe'),
    (Join-Path $RepoRoot 'python.exe')
  )

  foreach ($candidate in $direct) {
    if (Test-Path $candidate) {
      Add-UniqueString -List $items -Value ((Resolve-Path -LiteralPath $candidate).Path)
    }
  }

  try {
    Get-ChildItem -LiteralPath $RepoRoot -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
      foreach ($relative in @('Scripts\python.exe', '.venv\Scripts\python.exe', 'venv\Scripts\python.exe')) {
        $candidate = Join-Path $_.FullName $relative
        if (Test-Path $candidate) {
          Add-UniqueString -List $items -Value ((Resolve-Path -LiteralPath $candidate).Path)
        }
      }
    }
  } catch {
  }

  return @($items)
}

function Get-LocalOpenWebUiPackageCandidates([string]$RepoRoot) {
  $items = [System.Collections.Generic.List[string]]::new()
  $direct = @(
    (Join-Path $RepoRoot 'open_webui'),
    (Join-Path $RepoRoot 'src\open_webui'),
    (Join-Path $RepoRoot 'Lib\site-packages\open_webui'),
    (Join-Path $RepoRoot 'site-packages\open_webui'),
    (Join-Path $RepoRoot '.venv\Lib\site-packages\open_webui'),
    (Join-Path $RepoRoot 'venv\Lib\site-packages\open_webui'),
    (Join-Path $RepoRoot 'env\Lib\site-packages\open_webui')
  )

  foreach ($candidate in $direct) {
    if (Test-OpenWebUiPackageRoot -PackageRoot $candidate) {
      Add-UniqueString -List $items -Value ((Resolve-Path -LiteralPath $candidate).Path)
    }
  }

  try {
    Get-ChildItem -LiteralPath $RepoRoot -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
      foreach ($relative in @('Lib\site-packages\open_webui', 'site-packages\open_webui', 'src\open_webui', 'open_webui')) {
        $candidate = Join-Path $_.FullName $relative
        if (Test-OpenWebUiPackageRoot -PackageRoot $candidate) {
          Add-UniqueString -List $items -Value ((Resolve-Path -LiteralPath $candidate).Path)
        }
      }
    }
  } catch {
  }

  return @($items)
}

function Select-OpenWebUiRootWithGui([string]$InitialDirectory) {
  try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = 'Select the Open WebUI root folder. This should be the folder one level above patcher, or a folder containing open_webui / Lib\site-packages\open_webui.'
    $dialog.ShowNewFolderButton = $false
    if (-not [string]::IsNullOrWhiteSpace($InitialDirectory) -and (Test-Path $InitialDirectory)) {
      $dialog.SelectedPath = (Resolve-Path -LiteralPath $InitialDirectory).Path
    }

    $result = $dialog.ShowDialog()
    if ($result -eq [System.Windows.Forms.DialogResult]::OK -and -not [string]::IsNullOrWhiteSpace($dialog.SelectedPath)) {
      return $dialog.SelectedPath
    }
    return ""
  } catch {
    Warn ('GUI picker could not be opened: {0}' -f $_.Exception.Message)
    return ""
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
  $lastError = "No Open WebUI install was detected one level above patcher."
  $savedOwuiTarget = Get-ConfigValue -Config $SavedConfig -Name 'owui_target'
  $savedPythonExe = Get-ConfigValue -Config $SavedConfig -Name 'python_exe'
  $localPythonCandidates = @(Get-LocalPythonCandidates -RepoRoot $repoRoot)
  $localPackageCandidates = @(Get-LocalOpenWebUiPackageCandidates -RepoRoot $repoRoot)

  function Get-FirstUsablePython([string[]]$PreferredCandidates) {
    foreach ($candidate in $PreferredCandidates) {
      if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path $candidate)) {
        return (Resolve-Path -LiteralPath $candidate).Path
      }
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pythonCmd) {
      return $pythonCmd.Source
    }

    return ""
  }

  function New-ResolvedResultFromPackage([string]$PackageRoot, [string]$Source, [string]$SelectedInput, [string[]]$PreferredPythonCandidates) {
    if (-not (Test-OpenWebUiPackageRoot -PackageRoot $PackageRoot)) {
      return $null
    }

    $pythonForInfo = Get-FirstUsablePython -PreferredCandidates $PreferredPythonCandidates
    $installInfo = New-OpenWebUiInstallInfoFromPackageRoot -PackageRoot $PackageRoot -PythonPath $pythonForInfo
    if ([string]::IsNullOrWhiteSpace([string]$installInfo.python)) {
      throw ('Open WebUI files were found at {0}, but no usable python.exe was found. Create/use .venv under {1}, or rerun with -PythonExe.' -f $PackageRoot, $repoRoot)
    }

    return [pscustomobject]@{
      SelectedInput = $SelectedInput
      Source = $Source
      PythonPath = $installInfo.python
      InstallInfo = $installInfo
    }
  }

  function Try-ResolvePathCandidate([string]$Value, [string]$Label, [string[]]$PreferredPythonCandidates) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
      return $null
    }

    try {
      $resolvedValue = Resolve-AbsolutePath -CandidatePath $Value -BaseRoot $repoRoot
      $item = Get-Item -LiteralPath $resolvedValue
      if ($item.PSIsContainer) {
        $folderPackageCandidates = @(Get-LocalOpenWebUiPackageCandidates -RepoRoot $resolvedValue)
        foreach ($packageRoot in $folderPackageCandidates) {
          $fromPackage = New-ResolvedResultFromPackage -PackageRoot $packageRoot -Source $Label -SelectedInput $Value -PreferredPythonCandidates $PreferredPythonCandidates
          if ($fromPackage) {
            return $fromPackage
          }
        }
      }

      $pythonPath = Resolve-PythonFromTarget -TargetPath $Value -BaseRoot $repoRoot
      $installInfo = Get-InstallInfo -PythonPath $pythonPath -Quiet
      if ($installInfo) {
        return [pscustomobject]@{
          SelectedInput = $Value
          Source = $Label
          PythonPath = $pythonPath
          InstallInfo = $installInfo
        }
      }
      $scriptPackageRoots = @(
        (Join-Path (Split-Path -Parent (Split-Path -Parent $pythonPath)) 'Lib\site-packages\open_webui')
      )
      foreach ($packageRoot in $scriptPackageRoots) {
        if (Test-OpenWebUiPackageRoot -PackageRoot $packageRoot) {
          return (New-ResolvedResultFromPackage -PackageRoot $packageRoot -Source $Label -SelectedInput $Value -PreferredPythonCandidates @($pythonPath))
        }
      }
      Set-Variable -Name lastError -Scope 1 -Value ('Open WebUI is not importable from {0}' -f $pythonPath)
    } catch {
      Set-Variable -Name lastError -Scope 1 -Value $_.Exception.Message
    }

    return $null
  }

  # Highest priority: the patcher folder's parent. This is the required layout: .\owui\patcher.
  foreach ($packageRoot in $localPackageCandidates) {
    try {
      $resolved = New-ResolvedResultFromPackage -PackageRoot $packageRoot -Source 'Open WebUI folder above patcher' -SelectedInput $repoRoot -PreferredPythonCandidates $localPythonCandidates
      if ($resolved) {
        Info ('Detected Open WebUI one level above patcher: {0}' -f $resolved.InstallInfo.package_root)
        return $resolved
      }
    } catch {
      $lastError = $_.Exception.Message
    }
  }

  foreach ($pythonPath in $localPythonCandidates) {
    $resolved = Try-ResolvePathCandidate -Value $pythonPath -Label 'local Python above patcher' -PreferredPythonCandidates @($pythonPath)
    if ($resolved) {
      Info ('Detected Open WebUI through local Python above patcher: {0}' -f $resolved.PythonPath)
      return $resolved
    }
  }

  # Explicit command-line values come next. They should still work when patcher is kept outside OWUI.
  foreach ($pair in @(
    [pscustomobject]@{ Value = $RequestedTarget; Label = 'requested OWUI target' },
    [pscustomobject]@{ Value = $RequestedPython; Label = 'requested Python' },
    [pscustomobject]@{ Value = $savedOwuiTarget; Label = 'saved OWUI target' },
    [pscustomobject]@{ Value = $savedPythonExe; Label = 'saved Python' }
  )) {
    $preferredPython = @($localPythonCandidates)
    if ($pair.Label -like '*Python' -and -not [string]::IsNullOrWhiteSpace([string]$pair.Value)) {
      $preferredPython = @([string]$pair.Value) + $preferredPython
    }
    $resolved = Try-ResolvePathCandidate -Value ([string]$pair.Value) -Label $pair.Label -PreferredPythonCandidates $preferredPython
    if ($resolved) {
      Info ('Detected Open WebUI via {0}' -f $resolved.Source)
      return $resolved
    }
  }

  if (-not $AllowPrompt) {
    Fail $lastError
  }

  Warn $lastError
  while ($true) {
    $selectedRoot = Select-OpenWebUiRootWithGui -InitialDirectory $repoRoot
    if ([string]::IsNullOrWhiteSpace($selectedRoot)) {
      Fail 'Open WebUI target selection was cancelled.'
    }

    $selectedPythonCandidates = @(Get-LocalPythonCandidates -RepoRoot $selectedRoot)
    $resolved = Try-ResolvePathCandidate -Value $selectedRoot -Label 'GUI selected OWUI root' -PreferredPythonCandidates $selectedPythonCandidates
    if ($resolved) {
      return $resolved
    }

    Warn ('Selected folder is not a usable Open WebUI install: {0}' -f $selectedRoot)
    try {
      Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
      [System.Windows.Forms.MessageBox]::Show(
        "That folder does not contain a usable Open WebUI install. Select the OWUI root that contains open_webui or Lib\site-packages\open_webui.",
        "Open WebUI patcher",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning
      ) | Out-Null
    } catch {
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
