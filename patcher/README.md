# Open WebUI patcher

This follows the same repo layout pattern as the Langflow patcher:

```text
OWUI_Patch/
  patcher/
    install.ps1
    install.bat
    install_openwebui.ps1
    gui.ps1
    gui.bat
    patcher.ps1
    status.ps1
    restore.ps1
    start_openwebui.ps1
    start_openwebui.bat
    patch_manifest.json
    payload/
      open_webui/
```

The easiest entry point on Windows is now:

```powershell
.\patcher\gui.bat
```

The GUI has three actions:

- `Install`: creates `.venv` in the selected root, installs `open-webui==0.9.6`, saves the selected paths, and can immediately apply the patch.
- `Patch`: applies this patcher to an existing Open WebUI install.
- `Launch`: starts Open WebUI with the patch runtime settings, including `ENABLE_OAUTH_PERSISTENT_CONFIG`, `PDF_EXTRACT_IMAGES`, and optional `TESSERACT_CMD`.

You can also use it in either of these ways:

1. Keep this repo anywhere and point it at an OWUI Python environment with `-PythonExe`.
2. Drop the `patcher` folder inside an OWUI root, then run `install.ps1` with no arguments. The installer checks one level above `patcher` first, so `C:\sand\fresh\OWUI_V4\patcher` resolves `C:\sand\fresh\OWUI_V4` automatically.

The patcher now saves your chosen Open WebUI target, Python path, Tesseract path, and launcher host/port in `patcher\local.settings.json` so follow-up commands reuse the same machine-local answers.

If you run the patcher from an Open WebUI root that already has a local `.venv`, the resolver now checks the current working directory first and uses that environment automatically.

## What it patches

- OWUI OIDC callback redirect back to `/` instead of `/auth`
- cookie-backed session restore so the returned user is already logged in
- automatic redirect to the sole configured OAuth provider
- `Admin Panel -> Settings -> SSO`
- Workbench/TWC-matching SSO field names in the admin editor, including `TWC_AUTH_CLIENT_ID`, `TWC_AUTH_CLIENT_SECRET`, `TWC_AUTH_SCOPE`, `TWC_SAML_*`, and `TWC_AUTH_SERVER_OVERRIDES`
- an `Auto Login With TWC` toggle backed by OWUI's `OAUTH_AUTO_REDIRECT`
- left-sidebar `Workbench` bridge panel that keeps OWUI as the main chat surface
- full-height SSO settings page
- dropdown-first SSO claim mapping UI with custom fallback fields
- per-user Workbench connection, server, project, branch, and branch-model selectors
- Tesseract OCR for uploaded standalone images
- hybrid PDF ingestion: PyMuPDF native extraction first, Tesseract fallback for scanned/image-heavy pages

## Install

Full Open WebUI install into the folder one level above `patcher`:

```powershell
.\patcher\install_openwebui.ps1 -PatchAfterInstall
```

Patch an existing Open WebUI install:

```powershell
.\patcher\install.bat
```

Or:

```powershell
.\patcher\install.ps1 -OpenWebUiTarget C:\path\to\OpenWebUI
```

The installer:

- auto-detects the OWUI install from the current working directory first, then from the folder one level above `patcher`, using local source, `.venv`, `venv`, `Lib\site-packages`, or `site-packages` layouts
- opens a Windows folder picker only if no usable OWUI install is found relative to `patcher`
- auto-detects Tesseract without interrupting the install; pass `-TesseractExe` to override it
- verifies the target Open WebUI version is `0.9.6` unless you use `-SkipVersionCheck`
- checks whether the Python OCR dependencies are already importable and only installs the missing ones
- backs up every replaced file under `patcher\backups\...`
- copies the patched payload into the target `site-packages\open_webui`

Relative values passed to `-OpenWebUiTarget` or `-PythonExe` are resolved from the current working directory first, then from the patcher repo.

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
