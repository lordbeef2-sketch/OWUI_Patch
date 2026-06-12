param(
  [string]$OpenWebUiTarget = "",
  [string]$PythonExe = "",
  [string]$TesseractExe = "",
  [Alias('Host')]
  [string]$ListenHost = "",
  [int]$Port = 0,
  [switch]$ValidateOnly,
  [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $PatcherRoot 'common.ps1')

$SavedConfig = Load-PatcherConfig -PatcherRoot $PatcherRoot
$CanPrompt = (Get-IsInteractiveSession) -and (-not $NonInteractive)
$SavedHost = Get-ConfigValue -Config $SavedConfig -Name 'host'
$SavedPort = Get-ConfigValue -Config $SavedConfig -Name 'port'

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
$ScriptsDir = Split-Path -Parent $PythonPath
$OpenWebUiExe = Join-Path $ScriptsDir 'open-webui.exe'
$UseModuleLaunch = -not (Test-Path $OpenWebUiExe)
$TesseractPath = Resolve-TesseractPath `
  -PatcherRoot $PatcherRoot `
  -RequestedPath $TesseractExe `
  -SavedConfig $SavedConfig `
  -AllowPrompt:$CanPrompt `
  -PromptForConfirmation:$false `
  -Optional
$ResolvedHost = Resolve-SettingValue `
  -Prompt 'Open WebUI host' `
  -RequestedValue $ListenHost `
  -SavedValue $SavedHost `
  -FallbackValue '127.0.0.1' `
  -AllowPrompt:$CanPrompt `
  -PromptForConfirmation:$false
$ResolvedPort = Resolve-PortValue `
  -RequestedValue $Port `
  -SavedValue $SavedPort `
  -FallbackValue 8080 `
  -AllowPrompt:$CanPrompt `
  -PromptForConfirmation:$false

if ($ValidateOnly) {
  Write-Host "VALIDATION_OK"
  Write-Host ("Python: {0}" -f $PythonPath)
  Write-Host ("Open WebUI package root: {0}" -f $InstallInfo.package_root)
  if ($UseModuleLaunch) {
    Write-Host "Launcher: python -m open_webui serve"
  } else {
    Write-Host ("Launcher: {0}" -f $OpenWebUiExe)
  }
  Write-Host ("Tesseract: {0}" -f $(if ($TesseractPath) { $TesseractPath } else { '<not set>' }))
  Write-Host ("Host: {0}" -f $ResolvedHost)
  Write-Host ("Port: {0}" -f $ResolvedPort)
  exit 0
}

$env:ENABLE_OAUTH_PERSISTENT_CONFIG = "true"
$env:PDF_EXTRACT_IMAGES = "true"
if ($TesseractPath) {
  $env:TESSERACT_CMD = $TesseractPath
}

$configPath = Save-PatcherConfig -PatcherRoot $PatcherRoot -Updates @{
  owui_target = $ConfigOwuiTarget
  python_exe = $PythonPath
  tesseract_exe = $TesseractPath
  host = $ResolvedHost
  port = $ResolvedPort
}

Info ("Using local patcher settings: {0}" -f $configPath)
Info ("Starting patched Open WebUI on {0}:{1}" -f $ResolvedHost, $ResolvedPort)

if ($UseModuleLaunch) {
  & $PythonPath -m open_webui serve --host $ResolvedHost --port $ResolvedPort
} else {
  & $OpenWebUiExe serve --host $ResolvedHost --port $ResolvedPort
}
