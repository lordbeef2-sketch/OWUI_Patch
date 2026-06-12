param(
  [string]$OpenWebUiTarget = "",
  [string]$PythonExe = "",
  [string]$TesseractExe = "",
  [switch]$SkipDependencyInstall,
  [switch]$SkipVersionCheck,
  [switch]$NonInteractive
)

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Installer = Join-Path $PatcherRoot 'install.ps1'

& $Installer @PSBoundParameters
exit $LASTEXITCODE
