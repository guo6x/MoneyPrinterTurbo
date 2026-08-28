# AIDrama Forge MAX Windows H.264 packaging evidence (P1)

Status: technical redistribution/licensing evidence only. This document is not
legal advice or a legal approval for external redistribution.

## Selected payload

- Provider/source build repository: `BtbN/FFmpeg-Builds`
  (`https://github.com/BtbN/FFmpeg-Builds`)
- FFmpeg upstream source location: `https://ffmpeg.org/download.html`
- GitHub release asset ID: `532622813`
- Asset: `ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip`
- Asset URL: `https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip`
- Required SHA-256:
  `54b56d8f7e3fdeb3a987650a93cf4d4ed2f446f893f109dce191deec2007d155`

The URL carries a rolling `latest` label, but packaging accepts it only when
the exact SHA-256 above matches. Any changed asset fails closed.

## Observed binary characteristics

The validated Windows x64 shared binary reports:

- Version: `n8.1.2-47-g156bb4d299-20260827`
- License output: GNU Lesser General Public License, version 3 or later
- Configuration includes `--enable-shared`, `--disable-static`,
  `--enable-version3`, `--disable-libx264`, `--disable-libx265`, and
  `--disable-libxvid`
- Configuration does not include `--enable-gpl` or `--enable-libx264`
- `ffmpeg -encoders` contains `h264_mf` and does not contain `libx264`

The installer stages only `ffmpeg.exe`, `ffprobe.exe`, their co-located shared
DLLs, the upstream `LICENSE.txt`, and generated exact-binary evidence. It does
not bundle the executable carried by `imageio-ffmpeg`.

## Product encoder contract

The frozen PyWebView launcher sets:

```text
AIDRAMA_FFMPEG_H264_ENCODER=h264_mf
```

Final Assembly resolves the semantic output codec to this explicit encoder and
probes the actual binary with `ffmpeg -encoders` before render. An unavailable
`h264_mf` raises an explicit configuration error. There is no fallback to
`libx264` and no codec substitution.

## Build-time gates

`desktop.ffmpeg_distribution` rejects the payload if any of these are true:

- archive hash differs from the pinned SHA-256;
- `h264_mf` is absent from the actual encoder inventory;
- `libx264` is present in the inventory or configuration;
- `--enable-gpl` is present in configuration, or `ffmpeg -L` reports GPL;
- the archive lacks FFmpeg, FFprobe, or `LICENSE.txt`.

`build_windows_delivery.ps1` then invokes the physically packaged
`_internal/ffmpeg/ffmpeg.exe -encoders`, requires `h264_mf`, rejects
`libx264`, requires package-local FFprobe, and rejects a second FFmpeg binary.
The package retains `distribution-evidence.json`, plus captured
`-version`/`-buildconf`/`-L` output in `licenses/ffmpeg/`.
