"""Tiny real media payloads for local orchestration/QC tests."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def mp4_bytes(*, source: str = "testsrc=size=160x120:rate=25:d=1", audio: bool = False) -> bytes:
    import imageio_ffmpeg

    binary = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "sample.mp4"
        command = [binary, "-y", "-f", "lavfi", "-i", source]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=1000:duration=1", "-shortest"]
        command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac"]
        command += [str(target)]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr[-1000:])
        return target.read_bytes()
