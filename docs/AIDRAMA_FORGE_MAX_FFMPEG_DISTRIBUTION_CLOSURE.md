# AIDrama Forge MAX FFmpeg distribution closure

Audited packaging head: `5a48f3a846210bf620ffcc476cc4eb86c4f6778a`.

## Exact binary

The packaging environment resolves:

`imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe`

from `imageio-ffmpeg==0.6.0`, the locked Windows AMD64 wheel:

`https://files.pythonhosted.org/packages/2c/c6/fa760e12a2483469e2bf5058c5faff664acf66cadb4df2ad6205b016a73d/imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl`

Observed wheel SHA-256: `02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a`.
The wheel payload and the installed payload both hash to
`2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`.

`ffmpeg -version` identifies Gyan FFmpeg 7.1 essentials; `ffmpeg -buildconf`
reports `--enable-gpl --enable-version3 --enable-static`, plus `libx264`,
`libx265`, `libxvid`, and `librubberband`. `ffmpeg -L` reports GNU GPL
version 3 or later. No `--enable-nonfree` option was observed.

The exact option/library evidence is retained in
`licenses/ffmpeg/GPL_COMPONENT_AUDIT.txt`; upstream FFmpeg COPYING texts and
the GPL-enabled external-library COPYING texts and corresponding-source
checklist are retained beside it.

## Product encoder path

`aidrama_studio/services/adapters/final_assembly_runtime.py` maps the delivery
profile's H.264 target to `libx264` and invokes it for each normalized shot and
the final delivery-clock encode. The acceptance output profile also names
`libx264`. A clean local smoke using this exact binary successfully encoded and
decoded an MP4 with `libx264`, AAC, yuv420p, and MP4 fast-start.

Therefore the current product path requires a GPL-only encoder: replacing the
binary with an LGPL build would change the required encoder/runtime behavior.
Hardware H.264 encoders in the current binary are machine/driver dependent and
are not an equivalent clean-machine replacement.

## Redistribution boundary

The build emits `THIRD_PARTY_NOTICES.txt`, dependency license materials under
`licenses/python` and `licenses/build-tools`, exact FFmpeg `-version`/`-L`
records, FFmpeg `COPYING.GPLv3`/`COPYING.LGPLv3`/`LICENSE.md`, and the
`CORRESPONDING_SOURCE_OFFER.txt` checklist. The release owner must still
approve the exact GPL binary, publish complete corresponding source (including
enabled external libraries), or provide an applicable written source offer.

Decision for this audit: `LEGAL_REVIEW_REQUIRED`.
