param(
  [string]$OpenWebUiTarget = "",
  [string]$PythonExe = "",
  [string]$BackupName = "",
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

$Root = (Resolve-Path (Join-Path $PatcherRoot '..')).Path
$BackupsRoot = Join-Path $PatcherRoot 'backups'

if (-not (Test-Path $BackupsRoot)) {
  Fail ("No backups directory found: {0}" -f $BackupsRoot)
}

$SavedConfig = Load-PatcherConfig -PatcherRoot $PatcherRoot
$CanPrompt = (Get-IsInteractiveSession) -and (-not $NonInteractive)

$BackupDir = Select-BackupDirectory -BackupsRoot $BackupsRoot -RequestedBackupName $BackupName -AllowPrompt:$CanPrompt
$ResolvedOwui = Resolve-OpenWebUiTarget `
  -PatcherRoot $PatcherRoot `
  -RequestedTarget $OpenWebUiTarget `
  -RequestedPython $PythonExe `
  -SavedConfig $SavedConfig `
  -AllowPrompt:$CanPrompt
$ConfigOwuiTarget = if ($OpenWebUiTarget) {
  $OpenWebUiTarget
} elseif ($PythonExe) {
  $PythonExe
} else {
  $ResolvedOwui.SelectedInput
}

$PythonPath = $ResolvedOwui.PythonPath
$InstallInfo = $ResolvedOwui.InstallInfo
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

$configPath = Save-PatcherConfig -PatcherRoot $PatcherRoot -Updates @{
  owui_target = $ConfigOwuiTarget
  python_exe = $PythonPath
}

Ok ("Restored {0} files" -f $restored)
Ok ("Saved local patcher defaults to {0}" -f $configPath)
Write-Host "Restart Open WebUI after restoring." -ForegroundColor White
