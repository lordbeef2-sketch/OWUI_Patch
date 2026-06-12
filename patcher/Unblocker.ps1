# Unblock all .ps1 files in a selected folder

Add-Type -AssemblyName System.Windows.Forms

$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "Select folder containing PowerShell scripts to unblock"
$dialog.ShowNewFolderButton = $false

$result = $dialog.ShowDialog()

if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
    Write-Host "No folder selected. Exiting."
    exit 1
}

$folder = $dialog.SelectedPath

Write-Host "Scanning folder:"
Write-Host $folder
Write-Host ""

$files = Get-ChildItem -Path $folder -Filter "*.ps1" -Recurse -File

if (-not $files) {
    Write-Host "No .ps1 files found."
    exit 0
}

foreach ($file in $files) {
    try {
        Unblock-File -Path $file.FullName
        Write-Host "[unblocked] $($file.FullName)"
    }
    catch {
        Write-Warning "[failed] $($file.FullName) - $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "Done. Unblocked $($files.Count) PowerShell script(s)."