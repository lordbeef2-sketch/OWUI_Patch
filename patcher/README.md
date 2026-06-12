# Open WebUI patcher

This follows the same repo layout pattern as the Langflow patcher:

```text
OWUI_Patch/
  patcher/
    install.ps1
    install.bat
    patcher.ps1
    status.ps1
    restore.ps1
    start_openwebui.ps1
    start_openwebui.bat
    patch_manifest.json
    payload/
      open_webui/
```

You can use it in either of these ways:

1. Keep this repo anywhere and point it at an OWUI Python environment with `-PythonExe`.
2. Drop the `patcher` folder inside an OWUI root that already has `.venv`, then run `install.ps1` with no arguments.

The patcher now saves your chosen Open WebUI target, Python path, Tesseract path, and launcher host/port in `patcher\local.settings.json` so follow-up commands reuse the same machine-local answers.

## What it patches

- OWUI OIDC callback redirect back to `/` instead of `/auth`
- cookie-backed session restore so the returned user is already logged in
- automatic redirect to the sole configured OAuth provider
- `Admin Panel -> Settings -> SSO`
- left-sidebar `Workbench` bridge panel that keeps OWUI as the main chat surface
- full-height SSO settings page
- dropdown-first SSO claim mapping UI with custom fallback fields
- per-user Workbench connection, server, project, branch, and branch-model selectors
- Tesseract OCR for uploaded standalone images
- hybrid PDF ingestion: PyMuPDF native extraction first, Tesseract fallback for scanned/image-heavy pages

## Install

```powershell
.\patcher\install.bat
```

Or:

```powershell
.\patcher\install.ps1 -OpenWebUiTarget C:\path\to\OpenWebUI
```

The installer:

- prompts for the OWUI install location instead of assuming one
- lets you confirm or override the Tesseract executable path
- verifies the target Open WebUI version is `0.9.2` unless you use `-SkipVersionCheck`
- installs Python OCR dependencies
- backs up every replaced file under `patcher\backups\...`
- copies the patched payload into the target `site-packages\open_webui`

You can still run fully unattended with explicit flags:

```powershell
.\patcher\install.ps1 -PythonExe C:\path\to\python.exe -TesseractExe C:\Program Files\Tesseract-OCR\tesseract.exe -NonInteractive
```

Tesseract itself is not bundled by this patcher. The target machine should already have a local `tesseract` binary on `PATH`, or you should set `TESSERACT_CMD` to the full executable path before starting Open WebUI.

## Check

```powershell
.\patcher\status.ps1
```

## Restore

```powershell
.\patcher\restore.ps1
```

If you omit `-BackupName`, the latest backup is restored.

## Start helper

If the patcher is sitting inside an OWUI root with a local `.venv`, you can use:

```powershell
.\patcher\start_openwebui.bat
```

That helper sets:

- `ENABLE_OAUTH_PERSISTENT_CONFIG=true`
- `PDF_EXTRACT_IMAGES=true`

before launching `open-webui serve`.

If `open-webui.exe` is not present next to the selected Python runtime, the helper falls back to `python -m open_webui serve`.
