param(
  [string]$PythonExe = "",
  [switch]$SkipDependencyInstall,
  [switch]$SkipVersionCheck
)

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Installer = Join-Path $PatcherRoot 'install.ps1'

& $Installer @PSBoundParameters
exit $LASTEXITCODE
