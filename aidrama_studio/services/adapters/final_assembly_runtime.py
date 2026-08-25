"""Media-only runtime seam for deterministic Final Assembly rendering.

The adapter receives a frozen manifest request and never consults production
services, latest retries, or the database.  The existing narrow MPT video
concat helper is reused behind this boundary; no MPT task pipeline is called.
"""

from __future__ import annotations

import re
import subprocess
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from aidrama_studio.domain import FinalAssemblyItem, FinalAssemblyManifest


class FinalAssemblyRuntimeError(RuntimeError):
    """Raised when source media or a render output is not usable."""


@dataclass(frozen=True, slots=True)
class FinalAssemblyRenderRequest:
    project_id: str
    assembly_id: str
    items: tuple[FinalAssemblyItem, ...]
    source_paths: tuple[Path, ...]
    expected_duration: float = 0.0
    output_profile: Mapping[str, object] | None = None

    @classmethod
    def from_manifest(
        cls,
        manifest: FinalAssemblyManifest,
        source_paths: tuple[Path, ...] | list[Path],
        *,
        expected_duration: float = 0.0,
        output_profile: Mapping[str, object] | None = None,
    ) -> "FinalAssemblyRenderRequest":
        ordered = tuple(sorted(manifest.items, key=lambda item: (item.order_index, item.id)))
        return cls(
            project_id=manifest.project_id,
            assembly_id=manifest.id,
            items=ordered,
            source_paths=tuple(Path(path) for path in source_paths),
            expected_duration=float(expected_duration or 0.0),
            output_profile=dict(output_profile or {}),
        )


class FinalAssemblyRuntimeAdapter:
    """Interface for a final media renderer."""

    name = "abstract"

    def validate_sources(self, request: FinalAssemblyRenderRequest) -> list[dict[str, object]]:
        raise NotImplementedError

    def render(self, request: FinalAssemblyRenderRequest, output_path: Path) -> None:
        raise NotImplementedError

    def probe_output(self, output_path: Path) -> dict[str, object]:
        raise NotImplementedError

    def cancel(self, render_reference: str | None = None) -> bool:
        raise NotImplementedError("selected final assembly media seam does not support cancellation")


class MPTFinalAssemblyAdapter(FinalAssemblyRuntimeAdapter):
    """Adapter around the existing project's narrow FFmpeg concat helper."""

    name = "mpt-media-concat"
    SUPPORTED_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv"})
    _DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
    _VIDEO_RE = re.compile(r"Stream #\S+.*?Video:\s*([^,\s]+).*?(\d{2,5})x(\d{2,5})")
    _AUDIO_RE = re.compile(r"Stream #\S+.*?Audio:", re.IGNORECASE)

    def __init__(self, *, project_root: Path | None = None, ffmpeg_binary: str | None = None, threads: int = 2):
        self.project_root = Path(project_root).resolve() if project_root is not None else None
        self.ffmpeg_binary = ffmpeg_binary
        self.threads = max(1, int(threads or 2))

    def validate_sources(self, request: FinalAssemblyRenderRequest) -> list[dict[str, object]]:
        if not request.project_id or not request.assembly_id:
            raise FinalAssemblyRuntimeError("render request 缺少 project/assembly identity")
        if len(request.items) == 0 or len(request.items) != len(request.source_paths):
            raise FinalAssemblyRuntimeError("render request source 数量与 manifest 不一致")
        orders = [item.order_index for item in request.items]
        if orders != sorted(orders) or len(set(orders)) != len(orders):
            raise FinalAssemblyRuntimeError("manifest item order_index 必须唯一且按 canonical order 排列")
        result: list[dict[str, object]] = []
        for item, source in zip(request.items, request.source_paths):
            path = Path(source)
            self._validate_path(path)
            if not path.exists() or not path.is_file():
                raise FinalAssemblyRuntimeError(f"source 文件不存在: {item.source_path}")
            if path.stat().st_size <= 0:
                raise FinalAssemblyRuntimeError(f"source 文件为空: {item.source_path}")
            if Path(item.source_path).suffix.lower() not in self.SUPPORTED_SUFFIXES:
                raise FinalAssemblyRuntimeError(f"source 视频格式不支持: {item.source_path}")
            if path.suffix.lower() not in self.SUPPORTED_SUFFIXES:
                raise FinalAssemblyRuntimeError(f"source 视频格式不支持: {path.name}")
            result.append({
                "order_index": item.order_index,
                "production_shot_id": item.production_shot_id,
                "production_execution_id": item.production_execution_id,
                "production_artifact_id": item.production_artifact_id,
                "source_relative_path": item.source_path.replace("\\", "/"),
            })
        return result

    def render(self, request: FinalAssemblyRenderRequest, output_path: Path) -> None:
        self.validate_sources(request)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # This is the only media-composition call in AIDrama.  It reuses the
        # existing MPT helper but does not invoke MPT's generation pipeline.
        from app.services.video import concat_video_clips_with_ffmpeg

        profile = dict(request.output_profile or {})
        if not profile:
            concat_video_clips_with_ffmpeg([str(path) for path in request.source_paths], str(output_path), self.threads, str(output_path.parent))
            return
        temporary = output_path.with_name(f".{output_path.stem}.concat.mp4")
        temporary.unlink(missing_ok=True)
        try:
            concat_video_clips_with_ffmpeg([str(path) for path in request.source_paths], str(temporary), self.threads, str(output_path.parent))
            binary = self.ffmpeg_binary or self._resolve_ffmpeg()
            if profile.get("delivery_width") and profile.get("delivery_height"):
                resolution = (
                    f"{int(profile['delivery_width'])}x{int(profile['delivery_height'])}"
                )
            else:
                resolution = str(profile.get("target_resolution") or "").lower()
            match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", resolution)
            if match is None:
                raise FinalAssemblyRuntimeError("OutputProfile target_resolution 无效")
            width, height = match.groups()
            fps = float(profile.get("target_fps") or profile.get("fps") or 30)
            if fps <= 0 or fps > 240:
                raise FinalAssemblyRuntimeError("OutputProfile fps 无效")
            codec = str(
                profile.get("target_video_codec")
                or profile.get("video_codec_target")
                or "h264"
            ).lower()
            video_codec = "libx265" if "265" in codec or "hevc" in codec else "libx264"
            command = [binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(temporary), "-map", "0:v:0", "-map", "0:a?", "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps:g}", "-c:v", video_codec, "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", str(int(profile.get("target_audio_sample_rate") or profile.get("audio_sample_rate") or 48000)), "-ac", str(int(profile.get("target_audio_channels") or profile.get("audio_channels") or 2)), str(output_path)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
            if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
                raise FinalAssemblyRuntimeError("Final Assembly OutputProfile normalization failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FinalAssemblyRuntimeError("Final Assembly media normalization unavailable") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def probe_output(self, output_path: Path) -> dict[str, object]:
        path = Path(output_path)
        self._validate_path(path, allow_nonexistent=False)
        if not path.is_file() or path.stat().st_size <= 0:
            raise FinalAssemblyRuntimeError("render output 不存在或为空")
        binary = self.ffmpeg_binary or self._resolve_ffmpeg()
        try:
            completed = subprocess.run(
                [binary, "-hide_banner", "-i", str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FinalAssemblyRuntimeError(f"无法 probe render output: {exc}") from exc
        text = f"{completed.stderr or ''}\n{completed.stdout or ''}"
        duration_match = self._DURATION_RE.search(text)
        video_match = self._VIDEO_RE.search(text)
        if duration_match is None or video_match is None:
            raise FinalAssemblyRuntimeError("render output probe 未发现有效 video stream/duration")
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        codec, width, height = video_match.groups()
        return {
            "duration_seconds": duration,
            "width": int(width),
            "height": int(height),
            "resolution": f"{width}x{height}",
            "codec": codec,
            "video_stream": True,
            "audio_stream": bool(self._AUDIO_RE.search(text)),
            "size_bytes": path.stat().st_size,
            "mime_type": "video/mp4" if path.suffix.lower() == ".mp4" else f"video/{path.suffix.lower().lstrip('.')}",
        }

    def _validate_path(self, path: Path, *, allow_nonexistent: bool = True) -> None:
        if self.project_root is None:
            return
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = (self.project_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        if self.project_root not in candidate.parents and candidate != self.project_root:
            raise FinalAssemblyRuntimeError("media path escapes project storage")
        if not allow_nonexistent and not candidate.exists():
            raise FinalAssemblyRuntimeError("media path 不存在")

    @staticmethod
    def _resolve_ffmpeg() -> str:
        from app.utils.utils import get_ffmpeg_binary

        return get_ffmpeg_binary()


__all__ = [
    "FinalAssemblyRuntimeError",
    "FinalAssemblyRenderRequest",
    "FinalAssemblyRuntimeAdapter",
    "MPTFinalAssemblyAdapter",
]
