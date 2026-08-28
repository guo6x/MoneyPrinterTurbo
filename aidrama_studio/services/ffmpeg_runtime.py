"""FFmpeg runtime configuration kept below product output governance."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import subprocess


H264_ENCODER_ENV = "AIDRAMA_FFMPEG_H264_ENCODER"


class FFmpegEncoderConfigurationError(RuntimeError):
    """The configured encoder cannot satisfy the requested output codec."""


@dataclass(frozen=True, slots=True)
class VideoEncoderSelection:
    """Map a semantic output codec to one explicit FFmpeg implementation."""

    codec: str
    implementation: str

    @classmethod
    def resolve(
        cls, codec: str, *, implementation: str | None = None
    ) -> "VideoEncoderSelection":
        normalized = str(codec or "").strip().lower()
        if normalized in {"h264", "avc", "avc1"}:
            selected = implementation or os.environ.get(H264_ENCODER_ENV) or "libx264"
            semantic_codec = "h264"
        elif normalized in {"h265", "hevc", "hev1", "hvc1"}:
            selected = implementation or "libx265"
            semantic_codec = "h265"
        else:
            raise FFmpegEncoderConfigurationError(
                f"unsupported output video codec: {codec!r}"
            )
        selected = selected.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", selected):
            raise FFmpegEncoderConfigurationError("configured FFmpeg encoder name is invalid")
        return cls(codec=semantic_codec, implementation=selected)

    def require_available(self, ffmpeg_binary: str, *, timeout_seconds: int = 30) -> None:
        try:
            completed = subprocess.run(
                [ffmpeg_binary, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FFmpegEncoderConfigurationError(
                f"cannot inspect configured FFmpeg encoder {self.implementation!r}"
            ) from exc
        inventory = f"{completed.stdout}\n{completed.stderr}"
        encoder_line = re.compile(
            rf"(?m)^\s*[A-Z.]+\s+{re.escape(self.implementation)}(?:\s|$)"
        )
        if completed.returncode != 0 or encoder_line.search(inventory) is None:
            raise FFmpegEncoderConfigurationError(
                "configured FFmpeg encoder unavailable: "
                f"codec={self.codec}, implementation={self.implementation}; "
                "automatic fallback is disabled"
            )

    def output_args(self, *, preset: str = "veryfast") -> list[str]:
        args = ["-c:v", self.implementation]
        if self.implementation in {"libx264", "libx265"}:
            args += ["-preset", preset]
        return args + ["-pix_fmt", "yuv420p"]


__all__ = [
    "FFmpegEncoderConfigurationError",
    "H264_ENCODER_ENV",
    "VideoEncoderSelection",
]
