# AIDrama Forge MAX Windows installer preparation

This branch prepares the existing Python/Streamlit application for a
reproducible Windows package. It does not migrate the product to Tauri or
Electron and does not change the integration worktree.

## Current packaging audit

The mature stack already present in the repository is:

- PyInstaller onedir (`desktop/build.py`) with a pinned PyWebView 6.2.1 shell;
- `desktop/launcher.py`, which starts Streamlit on loopback, waits for
  `/_stcore/health`, opens PyWebView (with an explicit browser fallback), and
  records safe startup diagnostics;
- bundled `imageio-ffmpeg` resources and the existing FFmpeg resolver;
- Inno Setup (`installer/AIDramaStudio.iss`) with a stable AppId, a per-user
  Start Menu shortcut, an unchecked optional desktop shortcut, and no
  uninstall rule for user data.

No Nuitka, NSIS, MSIX, portable-Python installer, or Docker-only mechanism is
introduced. Docker remains a server/development path, not the desktop
installer runtime.

## Immutable delivery build

Run from a clean checkout of this packaging branch only after the noon product
SHA is known:

```powershell
$env:DELIVERY_HEAD = '<40 character SHA>'
$env:VERSION = '1.0.0'
.\build_windows_delivery.ps1 -DeliveryHead $env:DELIVERY_HEAD -Version $env:VERSION -PythonExecutable 'D:\path\to\dedicated\python.exe'
```

The script rejects a dirty source tree, resolves and verifies the exact commit,
creates a detached temporary worktree from that commit, and passes that tree
as `--source-root` to the packaging helpers. Thus the product modules/resources
come from the immutable DELIVERY_HEAD while today's packaging-only launcher,
build helper, and installer definition remain available even if the product
commit predates them. It then verifies embedded provenance, bundles FFmpeg,
scans packaged text for credential-like literals, compiles Inno Setup, and
writes `delivery-manifest.json` and `SHA256SUMS`. Existing artifact directories
are never overwritten.

The low-level PyInstaller helper can also be inspected with:

```powershell
python -m desktop.build --output-dir <artifact-root> --version 1.0.0 --delivery-head <sha>
```

The installer compiler receives `/DMyAppVersion`, `/DDeliveryHead`,
`/DSourceDir`, and `/DOutputDir`; the installer filename contains the version
and a 12-character SHA prefix while `build-info.json` and
`release/build-provenance.json` retain the full SHA.
The installed executable also supports `AIDramaStudio.exe --version` for a
human-readable version/SHA check without starting the application server.

## Runtime/data boundary

Installed immutable files live under the Inno per-user program directory.
The frozen launcher sets `MPT_CONFIG_DIR` and `AIDRAMA_DATA_DIR` to
`%LOCALAPPDATA%\AIDramaStudio` (or an explicit test override), creates
`logs\`, and copies only the credential-free `config.example.toml` on first
start. The packaging boundary redirects legacy MPT `storage_dir` and
`task_dir` helpers into `%LOCALAPPDATA%\AIDramaStudio\storage`; bundled
songs/public resources remain read-only (proprietary system-font files are
not redistributed). Existing databases/projects are not
removed by upgrade or uninstall.

Credentials are entered after launch through the Settings/readiness flow and
remain owned by `WindowsCredentialStore`; no API key is copied into the
package or installer.

## Smoke and legal boundary

`packaging_smoke.ps1` performs an isolated install, bundled FFmpeg version
probe, fresh AppData database/health smoke, shortcut check, and uninstall data
preservation check. It must be run only against an explicitly supplied test
installer and reports `PACKAGING_INFRA_SMOKE=PASS`; it is not final customer
acceptance.

The bundled `imageio-ffmpeg` executable is controlled and discoverable without
global PATH. The application currently performs media probing through FFmpeg
itself; if a separately supplied `ffprobe.exe` is present, the launcher also
exports its discovered path. The exact GPL-enabled FFmpeg binary still
requires release-owner legal approval before external redistribution. A final
noon installer is therefore not built by this preparation branch.

During each build, `desktop/license_materials.py` copies license and notice
files from the exact installed runtime/build-tool distributions into the
package's `licenses/python` and `licenses/build-tools` directories, emits
`THIRD_PARTY_NOTICES.txt`, and records the exact FFmpeg `-version` and `-L`
output under `licenses/ffmpeg`. Missing upstream license files are marked for
review rather than replaced with guessed text. The current application does
not ship a separate ffprobe binary; `FFPROBE_DISCOVERY=NOT_SHIPPED` is the
honest result for this package.
Checked-in `licenses/ffmpeg/COPYING.GPLv3`, `COPYING.LGPLv3`, and `LICENSE.md`
are copied into each package, alongside `CORRESPONDING_SOURCE_OFFER.txt`
which records the exact PyPI wheel/payload hashes and source-offer checklist.
