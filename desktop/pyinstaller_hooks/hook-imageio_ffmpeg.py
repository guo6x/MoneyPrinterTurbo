"""Keep imageio-ffmpeg importable without shipping its downloaded executable.

The frozen launcher always sets ``IMAGEIO_FFMPEG_EXE`` to the reviewed payload
at ``_internal/ffmpeg/ffmpeg.exe``.  The upstream PyInstaller hook would add
imageio-ffmpeg's own binary as package data, which would reintroduce the GPL
payload this packaging definition explicitly rejects.
"""

from __future__ import annotations

from PyInstaller.utils.hooks import is_module_satisfies


hiddenimports = ["imageio_ffmpeg.binaries"] if is_module_satisfies("imageio_ffmpeg >= 0.5.0") else []
