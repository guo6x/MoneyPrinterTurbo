# AIDrama Studio V1 native desktop-window verification

This verification closes the desktop-shell gap identified after the internal
RC. It changes only the desktop launcher/build definition and the optional
desktop dependency; product pages, data models, migrations, and provider
flows are unchanged.

## Baseline package

The package from `b5194a8` was run before the correction. Its launcher could
not import `webview` and opened a Chrome window titled `AIDrama Studio - Google
Chrome`, so the observed mode was:

```text
CURRENT_PACKAGED_WINDOW_MODE=BROWSER_FALLBACK
PYWEBVIEW_INSTALLED=NO (build environment before correction)
PYWEBVIEW_PACKAGED=NO
```

The machine has Microsoft Edge WebView2 Runtime `114.0.1823.67` registered in
the 64-bit Windows WebView2 registry location.

## Correction

- Pin the desktop-only dependency to PyWebView `6.2.1` in
  `desktop/requirements.txt`.
- Require that exact version in `desktop/build.py` before PyInstaller runs.
- Explicitly collect `webview`, `pythonnet`, `clr_loader`, and `proxy_tools`,
  including the WinForms/EdgeChromium/MSHTML backends and WebView2 interop
  files.
- Keep `--browser` and automatic fallback for machines where native WebView
  initialization genuinely fails.

PyWebView was installed from the official PyPI distribution into the D-drive
project virtual environment. No provider or paid request was made.

## Physical package evidence

The corrected onedir package was built at:

```text
D:\environment\aidrama-studio-native-window-rc-final\dist\AIDramaStudio
```

The tree contains `webview`, `pythonnet`, `clr_loader`, `proxy_tools`,
`Microsoft.Web.WebView2.*.dll`, and `WebView2Loader.dll` runtime assets.

The physical executable was launched normally and exposed exactly one native
window with the following observed accessibility identity:

```text
PYWEBVIEW_IMPORT=PASS
NATIVE_WINDOW_CREATED=PASS
WINDOW_TITLE=AIDrama Studio
NORMAL_DESKTOP_MODE=PYWEBVIEW
NORMAL_LAUNCH_BROWSER_OPENED=NO
LOOPBACK_STREAMLIT_HEALTH=PASS
BACKGROUND_RUNNER=PASS
CLEAN_SHUTDOWN=PASS
```

The explicit `--browser` run opened the expected Chrome fallback window and
the launcher process was then stopped cleanly:

```text
BROWSER_FALLBACK_MODE=PASS
```

## Installer evidence

The installer was rebuilt from the corrected package with Inno Setup 6.7.3 at
`D:\environment\inno-setup\installed\ISCC.exe`, then installed into the
isolated test directory `D:\environment\installer-test-native-final` with the
desktop task selected. Both shortcuts were started separately:

```text
INSTALLER_REBUILT=PASS
INSTALLED_NATIVE_WINDOW=PASS
START_MENU_NATIVE_LAUNCH=PASS
DESKTOP_SHORTCUT_NATIVE_LAUNCH=PASS
WINDOW_TITLE=AIDrama Studio
```

Each shortcut produced the native window without a browser address bar. The
window was closed with Alt+F4 and the packaged process exited; no Streamlit or
AIDramaStudio process remained.

## Scope and regression statement

```text
DATABASE_CHANGES=NO
MIGRATION_CHANGES=NO
PROVIDER_CALLS=NO
PAID_CALLS=NO
NEW_REGRESSIONS=0
```

The browser fallback remains available for emergency/development use, but it
is no longer the normal packaged product experience.
