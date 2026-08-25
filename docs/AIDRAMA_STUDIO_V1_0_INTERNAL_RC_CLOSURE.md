# AIDrama Studio 1.0 Internal Release Candidate Closure

This is the physical, internal-only Windows RC evidence for the release branch.
It does not publish a GitHub Release, claim commercial redistribution rights,
or run paid providers without credentials and explicit authorization.

## Identity and scope

```text
GOAL=AIDRAMA_STUDIO_V1_0_INTERNAL_RELEASE_CANDIDATE_CLOSURE
PRODUCT=AIDrama Studio 1.0 Internal
BASE_HEAD=ca9b9c592cab78b68ade449bd231e03310c59431
BRANCH=release/aidrama-studio-v1-0-internal-rc
RELEASE_SCOPE=INTERNAL_ONLY
PUBLIC_DISTRIBUTION=NO
COMMERCIAL_SALE=NO
```

## Tooling and physical package

```text
TOOLS_INSTALLED=PyInstaller; Inno Setup
TOOL_VERSIONS=PyInstaller 6.22.2; Inno Setup 6.7.3
PYINSTALLER_BUILD=PASS
DESKTOP_ONEDIR_EXISTS=PASS
PHYSICAL_PACKAGE_AUDIT=PASS
PACKAGED_DESKTOP_SMOKE=PASS
PACKAGED_LOOPBACK_ONLY=PASS
PACKAGED_BACKGROUND_RUNNER=PASS
PACKAGED_APPDATA=PASS
PACKAGED_CLEAN_SHUTDOWN=PASS
PACKAGED_FEATURE_SMOKE=PASS
```

The packaged executable is `desktop/launcher.py`; `aidrama_studio/Main.py`
remains the Streamlit child script. The frozen launcher initializes the
credential-free config template under `%LOCALAPPDATA%\AIDramaStudio`, keeps
the loopback port explicit, and does not write user data beside the EXE.
The package physically loaded Dashboard, Story, Assets, Director, Production,
QC, Postproduction, and Settings. Settings reported `核心媒体模块已就绪`.

## Local media evidence

```text
PACKAGED_MEDIA_RUNTIME=PASS
PACKAGED_REAL_QC=PASS
PACKAGED_FINAL_ASSEMBLY=PASS
PACKAGED_POST_RENDER=PASS
PACKAGED_4K_DELIVERY=PASS
```

Using only the bundled FFmpeg from the physical package, a deterministic local
media harness passed version probe, decode, probe, QC probe, FinalAssembly,
subtitle render, BGM generation, audio mix, 1080p delivery, and 1080p→4K
delivery upscale. No Provider request was made.

```text
FFMPEG_LICENSE_IDENTIFIED=PASS
FFMPEG_INTERNAL_RUNTIME=PASS
FFMPEG_PUBLIC_REDISTRIBUTION=OUT_OF_SCOPE_INTERNAL_V1
CODE_SIGNING_STATUS=NOT_REQUIRED_INTERNAL_V1
NO_UNAPPROVED_PROPRIETARY_FONT=PASS
```

The exact GPL-enabled FFmpeg binary is inventoried in the package evidence;
public/external redistribution approval is intentionally not claimed.

## Installer and data lifecycle

```text
WINDOWS_INSTALLER_BUILD=PASS
INSTALLER_EXISTS=PASS
INSTALLER_SHA256=PASS
FRESH_INSTALL=PASS
INSTALLER_LAUNCH=PASS
UPGRADE_INSTALL=PASS
INSTALLER_DATA_PRESERVATION=PASS
UNINSTALL_USER_DATA_PRESERVATION=PASS
INTERNAL_RELEASE_ARTIFACT=PASS
```

The installer was compiled from `installer/AIDramaStudio.iss` with the D-drive
Inno compiler. Fresh install and upgrade completed successfully; the existing
`%LOCALAPPDATA%\AIDramaStudio\aidrama.db` SHA-256 remained unchanged. Exact
uninstall removed installed program files while preserving that database.
The private artifact directory is:

```text
D:\environment\aidrama-studio-v1.0-internal-rc
```

It contains the installer, `SHA256SUMS`, release notes, license/notice files,
and physical package SBOM/provenance/checksum evidence. It is not published.

## Live-provider and end-to-end boundary

```text
LIVE_LLM_GATE=NOT_RUN_WITH_REASON: credentials and explicit paid authorization unavailable
LIVE_IMAGE_GATE=NOT_RUN_WITH_REASON: credentials and explicit paid authorization unavailable
LIVE_VIDEO_GATE=NOT_RUN_WITH_REASON: credentials and explicit paid authorization unavailable
LIVE_VISION_GATE=NOT_RUN_WITH_REASON: credentials and explicit paid authorization unavailable
LIVE_TTS_GATE=NOT_RUN_WITH_REASON: credentials and explicit paid authorization unavailable
INTERNAL_LIVE_MODEL_GATE=BLOCKED_EXTERNAL_CREDENTIALS_AUTHORIZATION
REAL_MULTI_REFERENCE_GENERATION=BLOCKED_EXTERNAL_CREDENTIALS_AUTHORIZATION
REAL_MULTI_SHOT_GENERATION=BLOCKED_EXTERNAL_CREDENTIALS_AUTHORIZATION
REAL_USER_JOURNEY_E2E=BLOCKED_EXTERNAL_CREDENTIALS_AUTHORIZATION
FULL_PIPELINE_COLD_RESUME=NOT_RUN_WITH_REASON: live provider gate blocked
```

Required credentials are provider-specific API keys for the configured LLM,
image (GPT Image), video (Seedance or Wan), vision (Gemini candidate), and TTS
runtime, plus explicit authorization for bounded paid requests. No key or
secret is recorded in this document.

## Verification summary

```text
INTERNAL_ENGINEERING_GATE=PASS (accepted from BASE_HEAD; no new P0/P1 after fixes)
NEW_REGRESSIONS=0
PYTHON_CHECK=PASS
GIT_DIFF_CHECK=PASS
SECRET_SCAN=PASS
```

The only release-specific code changes are the minimal frozen-runtime config
root, bundled config template, Streamlit frozen-mode flag, and imageio package
metadata needed by the physical media-engine check. The accidental initial
Inno Setup installer location under `C:\Program Files (x86)` could not be
removed without elevated permissions; all release compilation and testing used
the verified D-drive copy at `D:\environment\inno-setup\installed\ISCC.exe`.

## Gate result

```text
INTERNAL_DESKTOP_RELEASE_GATE=PASS
INTERNAL_RELEASE_CANDIDATE_GATE=BLOCKED_EXTERNAL_CREDENTIALS_AUTHORIZATION
FINAL_GATE_INTERNAL=BLOCKED_EXTERNAL_CREDENTIALS_AUTHORIZATION
```

The RC is therefore ready for authorized internal desktop distribution and is
not represented as a complete live-provider release.
