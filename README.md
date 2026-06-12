# OWUI Patch

Reusable patcher for `Open WebUI 0.9.6`.

This package recreates the local OWUI changes we made for:

- TWC OIDC sign-in flow that lands back in OWUI already logged in
- `Admin Panel -> Settings -> SSO`
- Langflow-style SSO field names for shared settings like `discovery_url` and `user_id_claim`
- left-sidebar `Workbench` bridge panel inside OWUI with per-user Workbench selectors
- dropdown-based SSO claim mapping UI
- native PDF extraction with Tesseract fallback for scanned pages
- uploaded image OCR for agents using Tesseract

The actual patcher lives in [`patcher/`](patcher).

Quick start:

```powershell
.\patcher\gui.bat
```

The GUI gives you three buttons:

- `Install`: create `.venv`, install `open-webui==0.9.6`, then optionally patch it.
- `Patch`: apply the TWC patch to an existing OWUI install.
- `Launch`: start the patched OWUI server with the saved local config.

CLI patch-only quick start:

```powershell
.\patcher\install.bat
```

If you run that from an Open WebUI folder with a local `.venv`, the patcher now auto-detects that environment instead of making you browse to `python.exe` or `Lib\site-packages`.

The installer now prompts for the Open WebUI target and optional Tesseract path, then saves those answers to `patcher\local.settings.json` for later `status`, `restore`, and `start_openwebui` runs.

Or patch a different OWUI install explicitly:

```powershell
.\patcher\install.ps1 -OpenWebUiTarget C:\path\to\OpenWebUI
```

You can still point straight at Python if you prefer:

```powershell
.\patcher\install.ps1 -PythonExe C:\path\to\python.exe
```

This patch is packaged for `Open WebUI 0.9.6` and validates that version by default.
It installs Python-side OCR dependencies, but it expects a local `tesseract` binary to already exist on the machine or be provided through `TESSERACT_CMD`.
