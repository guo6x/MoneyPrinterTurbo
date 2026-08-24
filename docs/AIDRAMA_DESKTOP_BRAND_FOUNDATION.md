# AIDrama Studio desktop and brand foundation

This foundation keeps the existing Streamlit application as the canonical
product surface while adding an optional local desktop shell.

## Desktop launcher

`desktop/launcher.py` starts `aidrama_studio/Main.py` with an explicit
loopback-only address (`127.0.0.1` by default), selects an available local
port, waits for `/_stcore/health`, and terminates the child process on exit.
PyWebView is optional: a machine without it receives a browser fallback rather
than an automatic dependency installation or a LAN-exposed server.

Repeatable local health smoke:

```powershell
& .\.venv\Scripts\python.exe -m desktop.launcher --smoke
```

The optional PyInstaller onedir command is exposed by `desktop/build.py`. The
repository environment currently has neither PyWebView nor PyInstaller, so no
packaging dependency was installed. `build.py` exits with a clear prerequisite
message until the operator deliberately provisions the minimal packaging
tools.

## Product brand and readiness

`aidrama_studio/branding.py` is the single public brand configuration. The
replaceable text mark lives at `aidrama_studio/assets/brand-mark.svg`; setting
`AIDRAMA_LOGO_PATH` replaces it without page changes. User-facing Streamlit
surfaces read the product name/tagline/version from that config. Upstream
attribution remains in `LICENSE` and `NOTICE`.

`ProviderReadinessService` reports LLM, image, generative video (Wan), vision,
and TTS capability state without making network calls or rendering secrets.
Provider-ready and provider-live are intentionally separate states.
