Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PatcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $PatcherRoot 'common.ps1')

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

function Get-DefaultInstallRoot {
  return (Resolve-Path (Join-Path $PatcherRoot '..')).Path
}

function Get-DefaultVersion {
  $manifestPath = Join-Path $PatcherRoot 'patch_manifest.json'
  if (Test-Path $manifestPath) {
    try {
      $manifest = Get-Content -Path $manifestPath -Raw | ConvertFrom-Json
      if ($manifest.target_version) {
        return [string]$manifest.target_version
      }
    } catch {
    }
  }
  return '0.9.6'
}

function Quote-Arg([string]$Value) {
  if ($null -eq $Value) {
    return "''"
  }
  return "'" + ($Value -replace "'", "''") + "'"
}

function Select-Folder([string]$InitialDirectory) {
  $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description = 'Select the Open WebUI install root'
  $dialog.ShowNewFolderButton = $true
  if (-not [string]::IsNullOrWhiteSpace($InitialDirectory) -and (Test-Path $InitialDirectory)) {
    $dialog.SelectedPath = (Resolve-Path -LiteralPath $InitialDirectory).Path
  }
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    return $dialog.SelectedPath
  }
  return ''
}

function Select-File([string]$Title, [string]$Filter) {
  $dialog = New-Object System.Windows.Forms.OpenFileDialog
  $dialog.Title = $Title
  $dialog.Filter = $Filter
  $dialog.CheckFileExists = $true
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    return $dialog.FileName
  }
  return ''
}

$saved = Load-PatcherConfig -PatcherRoot $PatcherRoot
$form = New-Object System.Windows.Forms.Form
$form.Text = 'OWUI TWC Patch Manager'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(920, 680)
$form.MinimumSize = New-Object System.Drawing.Size(860, 620)
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)

$header = New-Object System.Windows.Forms.Label
$header.Text = 'OWUI TWC Patch Manager'
$header.Font = New-Object System.Drawing.Font('Segoe UI Semibold', 16)
$header.AutoSize = $true
$header.Location = New-Object System.Drawing.Point(18, 16)
$form.Controls.Add($header)

$sub = New-Object System.Windows.Forms.Label
$sub.Text = 'Install Open WebUI, apply the TWC patch, or launch the patched server from one place.'
$sub.AutoSize = $true
$sub.Location = New-Object System.Drawing.Point(20, 50)
$form.Controls.Add($sub)

$layout = New-Object System.Windows.Forms.TableLayoutPanel
$layout.Location = New-Object System.Drawing.Point(22, 86)
$layout.Size = New-Object System.Drawing.Size(860, 206)
$layout.Anchor = 'Top,Left,Right'
$layout.ColumnCount = 3
$layout.RowCount = 6
$layout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 130))) | Out-Null
$layout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100))) | Out-Null
$layout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 92))) | Out-Null
$form.Controls.Add($layout)

function Add-Row([int]$Row, [string]$LabelText, [System.Windows.Forms.Control]$Control, [System.Windows.Forms.Control]$Button) {
  $label = New-Object System.Windows.Forms.Label
  $label.Text = $LabelText
  $label.TextAlign = 'MiddleLeft'
  $label.Dock = 'Fill'
  $layout.Controls.Add($label, 0, $Row)
  $Control.Dock = 'Fill'
  $layout.Controls.Add($Control, 1, $Row)
  if ($Button) {
    $Button.Dock = 'Fill'
    $layout.Controls.Add($Button, 2, $Row)
  }
}

$installRoot = New-Object System.Windows.Forms.TextBox
$installRoot.Text = if (Get-ConfigValue -Config $saved -Name 'owui_target') { [string](Get-ConfigValue -Config $saved -Name 'owui_target') } else { Get-DefaultInstallRoot }
$browseRoot = New-Object System.Windows.Forms.Button
$browseRoot.Text = 'Browse'
$browseRoot.Add_Click({
  $selected = Select-Folder -InitialDirectory $installRoot.Text
  if ($selected) { $installRoot.Text = $selected }
})
Add-Row -Row 0 -LabelText 'Install root' -Control $installRoot -Button $browseRoot

$pythonExe = New-Object System.Windows.Forms.TextBox
$pythonExe.Text = ''
$browsePython = New-Object System.Windows.Forms.Button
$browsePython.Text = 'Browse'
$browsePython.Add_Click({
  $selected = Select-File -Title 'Select Python 3.11 or 3.12' -Filter 'python.exe|python.exe|Executables|*.exe|All files|*.*'
  if ($selected) { $pythonExe.Text = $selected }
})
Add-Row -Row 1 -LabelText 'Host Python' -Control $pythonExe -Button $browsePython

$tesseractExe = New-Object System.Windows.Forms.TextBox
$tesseractExe.Text = if (Get-ConfigValue -Config $saved -Name 'tesseract_exe') { [string](Get-ConfigValue -Config $saved -Name 'tesseract_exe') } else { '' }
$browseTesseract = New-Object System.Windows.Forms.Button
$browseTesseract.Text = 'Browse'
$browseTesseract.Add_Click({
  $selected = Select-File -Title 'Select tesseract.exe' -Filter 'tesseract.exe|tesseract.exe|Executables|*.exe|All files|*.*'
  if ($selected) { $tesseractExe.Text = $selected }
})
Add-Row -Row 2 -LabelText 'Tesseract' -Control $tesseractExe -Button $browseTesseract

$version = New-Object System.Windows.Forms.TextBox
$version.Text = Get-DefaultVersion
Add-Row -Row 3 -LabelText 'OWUI version' -Control $version -Button $null

$hostBox = New-Object System.Windows.Forms.TextBox
$hostBox.Text = if (Get-ConfigValue -Config $saved -Name 'host') { [string](Get-ConfigValue -Config $saved -Name 'host') } else { '127.0.0.1' }
Add-Row -Row 4 -LabelText 'Launch host' -Control $hostBox -Button $null

$portBox = New-Object System.Windows.Forms.TextBox
$portBox.Text = if (Get-ConfigValue -Config $saved -Name 'port') { [string](Get-ConfigValue -Config $saved -Name 'port') } else { '8080' }
Add-Row -Row 5 -LabelText 'Launch port' -Control $portBox -Button $null

$patchAfterInstall = New-Object System.Windows.Forms.CheckBox
$patchAfterInstall.Text = 'Patch immediately after install'
$patchAfterInstall.Checked = $true
$patchAfterInstall.Location = New-Object System.Drawing.Point(25, 306)
$patchAfterInstall.AutoSize = $true
$form.Controls.Add($patchAfterInstall)

$installDeps = New-Object System.Windows.Forms.CheckBox
$installDeps.Text = 'Install/update OCR Python dependencies'
$installDeps.Checked = $true
$installDeps.Location = New-Object System.Drawing.Point(260, 306)
$installDeps.AutoSize = $true
$form.Controls.Add($installDeps)

$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true
$log.ScrollBars = 'Vertical'
$log.ReadOnly = $true
$log.Font = New-Object System.Drawing.Font('Consolas', 9)
$log.Location = New-Object System.Drawing.Point(22, 346)
$log.Size = New-Object System.Drawing.Size(860, 250)
$log.Anchor = 'Top,Bottom,Left,Right'
$form.Controls.Add($log)

$installButton = New-Object System.Windows.Forms.Button
$installButton.Text = 'Install'
$installButton.Location = New-Object System.Drawing.Point(22, 606)
$installButton.Size = New-Object System.Drawing.Size(130, 34)
$installButton.Anchor = 'Bottom,Left'
$form.Controls.Add($installButton)

$patchButton = New-Object System.Windows.Forms.Button
$patchButton.Text = 'Patch'
$patchButton.Location = New-Object System.Drawing.Point(164, 606)
$patchButton.Size = New-Object System.Drawing.Size(130, 34)
$patchButton.Anchor = 'Bottom,Left'
$form.Controls.Add($patchButton)

$launchButton = New-Object System.Windows.Forms.Button
$launchButton.Text = 'Launch'
$launchButton.Location = New-Object System.Drawing.Point(306, 606)
$launchButton.Size = New-Object System.Drawing.Size(130, 34)
$launchButton.Anchor = 'Bottom,Left'
$form.Controls.Add($launchButton)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = 'Ready'
$statusLabel.AutoSize = $true
$statusLabel.Location = New-Object System.Drawing.Point(456, 615)
$statusLabel.Anchor = 'Bottom,Left'
$form.Controls.Add($statusLabel)

$buttons = @($installButton, $patchButton, $launchButton)

function Append-Log([string]$Text) {
  if ($log.InvokeRequired) {
    $log.BeginInvoke([Action[string]]{ param($line) Append-Log -Text $line }, $Text) | Out-Null
    return
  }
  $log.AppendText($Text + [Environment]::NewLine)
}

function Set-Busy([bool]$Busy, [string]$Message) {
  foreach ($button in $buttons) {
    $button.Enabled = -not $Busy
  }
  $statusLabel.Text = $Message
}

function Start-PatcherProcess([string]$ScriptPath, [string[]]$Arguments, [string]$Label) {
  if (-not (Test-Path $ScriptPath)) {
    [System.Windows.Forms.MessageBox]::Show("Missing script: $ScriptPath", 'OWUI patcher', 'OK', 'Error') | Out-Null
    return
  }

  Set-Busy -Busy $true -Message ("Running {0}..." -f $Label)
  Append-Log ""
  Append-Log ("=== {0} ===" -f $Label)

  $psExe = (Get-Command powershell.exe -ErrorAction Stop).Source
  $allArgs = @('-ExecutionPolicy', 'Bypass', '-NoProfile', '-File', $ScriptPath) + $Arguments
  $argumentText = ($allArgs | ForEach-Object { Quote-Arg -Value $_ }) -join ' '

  $process = New-Object System.Diagnostics.Process
  $process.StartInfo.FileName = $psExe
  $process.StartInfo.Arguments = $argumentText
  $process.StartInfo.UseShellExecute = $false
  $process.StartInfo.RedirectStandardOutput = $true
  $process.StartInfo.RedirectStandardError = $true
  $process.StartInfo.CreateNoWindow = $true
  $process.EnableRaisingEvents = $true

  $outputHandler = [System.Diagnostics.DataReceivedEventHandler]{
    param($sender, $eventArgs)
    if ($eventArgs.Data) { Append-Log -Text $eventArgs.Data }
  }
  $errorHandler = [System.Diagnostics.DataReceivedEventHandler]{
    param($sender, $eventArgs)
    if ($eventArgs.Data) { Append-Log -Text $eventArgs.Data }
  }
  $exitHandler = [System.EventHandler]{
    param($sender, $eventArgs)
    $code = $sender.ExitCode
    Append-Log -Text ("=== {0} exited with code {1} ===" -f $Label, $code)
    $form.BeginInvoke([Action]{
      Set-Busy -Busy $false -Message $(if ($code -eq 0) { 'Ready' } else { 'Failed' })
    }) | Out-Null
  }

  $process.add_OutputDataReceived($outputHandler)
  $process.add_ErrorDataReceived($errorHandler)
  $process.add_Exited($exitHandler)
  [void]$process.Start()
  $process.BeginOutputReadLine()
  $process.BeginErrorReadLine()
}

function Get-CommonTargetArgs {
  $args = @()
  if (-not [string]::IsNullOrWhiteSpace($installRoot.Text)) {
    $args += @('-OpenWebUiTarget', $installRoot.Text)
  }
  if (-not [string]::IsNullOrWhiteSpace($tesseractExe.Text)) {
    $args += @('-TesseractExe', $tesseractExe.Text)
  }
  return $args
}

$installButton.Add_Click({
  $args = @('-InstallRoot', $installRoot.Text, '-OpenWebUiVersion', $version.Text, '-NonInteractive')
  if (-not [string]::IsNullOrWhiteSpace($pythonExe.Text)) {
    $args += @('-PythonExe', $pythonExe.Text)
  }
  if (-not [string]::IsNullOrWhiteSpace($tesseractExe.Text)) {
    $args += @('-TesseractExe', $tesseractExe.Text)
  }
  if ($patchAfterInstall.Checked) {
    $args += '-PatchAfterInstall'
  }
  if (-not $installDeps.Checked) {
    $args += '-SkipDependencyInstall'
  }
  Start-PatcherProcess -ScriptPath (Join-Path $PatcherRoot 'install_openwebui.ps1') -Arguments $args -Label 'Install'
})

$patchButton.Add_Click({
  $args = (Get-CommonTargetArgs) + @('-NonInteractive')
  if (-not $installDeps.Checked) {
    $args += '-SkipDependencyInstall'
  }
  Start-PatcherProcess -ScriptPath (Join-Path $PatcherRoot 'install.ps1') -Arguments $args -Label 'Patch'
})

$launchButton.Add_Click({
  $args = (Get-CommonTargetArgs) + @('-ListenHost', $hostBox.Text, '-Port', $portBox.Text, '-NonInteractive')
  Start-PatcherProcess -ScriptPath (Join-Path $PatcherRoot 'start_openwebui.ps1') -Arguments $args -Label 'Launch'
})

Append-Log 'Ready. Install creates .venv and installs Open WebUI into the selected root; Patch applies the TWC payload; Launch starts OWUI with patch runtime settings.'
[void]$form.ShowDialog()
