# OWUI Patch

Reusable patcher for `Open WebUI 0.9.2`.

This package recreates the local OWUI changes we made for:

- TWC OIDC sign-in flow that lands back in OWUI already logged in
- `Admin Panel -> Settings -> SSO`
- dropdown-based SSO claim mapping UI
- uploaded image and image-heavy PDF OCR for agents

The actual patcher lives in [`patcher/`](patcher).

Quick start:

```powershell
.\patcher\install.bat
```

Or patch a different OWUI Python install:

```powershell
.\patcher\install.ps1 -PythonExe C:\path\to\python.exe
```

This patch is packaged for `Open WebUI 0.9.2` and validates that version by default.
