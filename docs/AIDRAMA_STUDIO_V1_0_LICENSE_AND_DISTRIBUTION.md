# AIDrama Studio V1.0 license and distribution audit

## Product and upstream code

- Repository license: MIT (`LICENSE`).
- Upstream: MoneyPrinterTurbo, https://github.com/harry0703/MoneyPrinterTurbo.
- Product attribution: preserved in `NOTICE`.
- Product version frozen by the default brand configuration: `1.0.0`.

This is a technical distribution audit, not legal advice. It records only
licenses and binary facts observed in the committed sources or the current
locked Windows environment; it does not infer missing rights.

## Runtime dependency evidence

The canonical dependency inventory is `uv.lock`. The dependency-free release
tool walks the runtime closure rooted at the `moneyprinterturbo` package and
emits CycloneDX 1.5 JSON. The current direct runtime declarations include MIT,
Apache-2.0, BSD, LGPL and proprietary components. Examples verified from the
installed distribution metadata include:

| Component | Locked version | Observed distribution metadata |
| --- | ---: | --- |
| moviepy | 2.2.1 | MIT |
| streamlit | 1.59.1 | Apache-2.0 |
| streamlit-tour | 1.1.0 | BSD-3-Clause |
| edge-tts | 7.2.7 | LGPLv3 classifier and bundled LICENSE |
| fastapi | 0.136.3 | MIT |
| uvicorn | 0.32.1 | BSD-3-Clause |
| openai | 2.24.0 | Apache-2.0 |
| faster-whisper | 1.1.0 | MIT |
| dashscope | 1.20.14 | Apache 2.0 metadata |
| azure-cognitiveservices-speech | 1.41.1 | proprietary metadata and bundled LICENSE.md |
| imageio-ffmpeg | 0.6.0 | BSD-2-Clause Python wrapper; bundled FFmpeg is separately licensed |

The final package audit cannot be declared complete until an actual
PyInstaller tree exists and its physical inventory is compared with the SBOM
and retained license texts. In particular, proprietary SDK redistribution
terms require release-owner review at the shipped version.

## FFmpeg

The executable resolved in the current test environment is:

`imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe`

Its own `-version` / `-L` output identifies a Gyan FFmpeg 7.1 essentials build
with `--enable-gpl --enable-version3`, statically linked GPL codecs including
libx264/libx265, and GPLv3-or-later terms. The BSD-2-Clause license of the
Python `imageio-ffmpeg` wrapper does not relicense that executable.

Therefore:

- `FFMPEG_DISTRIBUTION_LICENSE_AUDIT=BLOCKED_LEGAL_APPROVAL_GPL_BINARY`
- The build metadata audit reports every packaged `ffmpeg*.exe`.
- No installer checksum or packaged-media PASS may be claimed until the exact
  selected FFmpeg binary and compliance materials are approved.

## Fonts

The upstream source tree contains Microsoft YaHei and STHeiti files without a
release-approved redistribution record. They are not added by the AIDrama
desktop build definition. The release audit fails closed if those filenames
are found in the physical distribution tree. Chinese subtitle rendering must
use an installed system font or a separately approved redistributable font;
the repository does not grant rights to bundle the proprietary files.

## Release metadata and installer boundary

`desktop/release.py` generates, without third-party tooling:

- CycloneDX runtime-lock SBOM;
- streaming SHA-256 package manifest;
- build provenance containing AIDrama version, exact Git commit, latest schema
  migration, timestamp, platform, Python version and metadata hashes;
- SHA-256 manifests for final installer/distributable files.

`installer/AIDramaStudio.iss` is an Inno Setup definition with a stable AppId,
per-user installation, upgrade-in-place semantics and no deletion rule for the
separate `%LOCALAPPDATA%\AIDrama Studio` project directory. It cannot be built
on the current machine because Inno Setup is absent; no tool was installed.

## Current gate status

- `LICENSE_NOTICE=PASS`
- `THIRD_PARTY_NOTICES=PASS` (source/build definition)
- `RELEASE_SBOM=PASS` (generator and tests; final built SBOM pending build)
- `BUILD_PROVENANCE=PASS` (generator and tests; final artifact pending build)
- `THIRD_PARTY_LICENSE_AUDIT=BLOCKED_FINAL_PACKAGE_AND_PROPRIETARY_SDK_REVIEW`
- `INSTALLER_CHECKSUM=BLOCKED_INSTALLER_BUILD`
- `CODE_SIGNING_STATUS=BLOCKED_NO_CERTIFICATE`

No dependency, build tool or installer tool was installed during this audit.
