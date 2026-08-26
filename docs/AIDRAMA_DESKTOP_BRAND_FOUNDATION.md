# AIDrama Studio desktop and brand foundation

This foundation keeps the existing Streamlit application as the canonical
product surface while adding a local desktop shell for the Windows release.

## Desktop launcher

`desktop/launcher.py` starts `aidrama_studio/Main.py` with an explicit
loopback-only address (`127.0.0.1` by default), selects an available local
port, waits for `/_stcore/health`, and terminates the child process on exit.
PyWebView 6.2.1 is the normal packaged window implementation. A browser is
still available only as an explicit development/emergency fallback when native
WebView initialization genuinely fails (or `--browser` is supplied); the
loopback server is never exposed to a LAN.

Repeatable local health smoke:

```powershell
& .\.venv\Scripts\python.exe -m desktop.launcher --smoke
```

The PyInstaller onedir command is exposed by `desktop/build.py`. Provision the
desktop-only runtime with `desktop/requirements.txt` and the free build tool
separately; `build.py` fails closed unless the pinned PyWebView version is
installed, so a frozen build cannot silently regress to browser-only mode.

## Product brand and readiness

`aidrama_studio/branding.py` is the single public brand configuration. The
replaceable text mark lives at `aidrama_studio/assets/brand-mark.svg`; setting
`AIDRAMA_LOGO_PATH` replaces it without page changes. User-facing Streamlit
surfaces read the product name/tagline/version from that config. Upstream
attribution remains in `LICENSE` and `NOTICE`.

`ProviderReadinessService` reports LLM, image, generative video (Wan), vision,
and TTS capability state without making network calls or rendering secrets.
Provider-ready and provider-live are intentionally separate states.
