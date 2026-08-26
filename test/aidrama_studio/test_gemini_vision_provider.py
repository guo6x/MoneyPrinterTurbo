from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aidrama_studio.services import VisionAnalysisRequest, VisionMediaInput
from aidrama_studio.services.providers.gemini_vision import (
    DEFAULT_GEMINI_BASE_URL,
    DEFAULT_GEMINI_VISION_MODEL,
    GeminiHTTPTransport,
    GeminiVisionError,
    GeminiVisionProvider,
    GeminiVisionProviderConfig,
)


METRIC_NAMES = (
    "CHARACTER_CONSISTENCY",
    "SCENE_CONSISTENCY",
    "SHOT_COMPLIANCE",
    "VISUAL_DEFECTS",
    "ACTION_COMPLIANCE",
    "STYLE_CONSISTENCY",
    "CONTINUITY",
)


def _write(path: Path, value: bytes) -> tuple[Path, str]:
    path.write_bytes(value)
    return path.resolve(), hashlib.sha256(value).hexdigest()


def _request(tmp_path: Path) -> VisionAnalysisRequest:
    video_path, video_hash = _write(tmp_path / "shot.mp4", b"video-bytes")
    frame_path, frame_hash = _write(tmp_path / "frame.jpg", b"\xff\xd8\xffframe")
    first_path, first_hash = _write(tmp_path / "hero.png", b"\x89PNG\r\n\x1a\nhero")
    second_path, second_hash = _write(tmp_path / "room.webp", b"RIFF0000WEBProom")
    return VisionAnalysisRequest(
        project_id="project-1",
        execution_id="execution-1",
        artifact_id="artifact-1",
        video=VisionMediaInput(
            "VIDEO_ARTIFACT",
            "artifact-1",
            video_path,
            "video/mp4",
            video_hash,
            "GENERATED_SHOT",
        ),
        frames=(
            VisionMediaInput(
                "SAMPLED_FRAME",
                "manifest-1:0",
                frame_path,
                "image/jpeg",
                frame_hash,
                "FIRST",
                0.0,
            ),
        ),
        references=(
            VisionMediaInput(
                "REFERENCE_VERSION",
                "version-hero",
                first_path,
                "image/png",
                first_hash,
                "CHARACTER:hero",
            ),
            VisionMediaInput(
                "REFERENCE_VERSION",
                "version-room",
                second_path,
                "image/webp",
                second_hash,
                "LOCATION:room",
            ),
        ),
        frame_manifest_id="manifest-1",
        generation_brief_hash="a" * 64,
        creative_context={
            "shot": "Hero enters the room",
            "api_key": "must-not-leak",
        },
    )


def _structured_response(reference_ids=("version-hero", "version-room")):
    metrics = {
        name: {
            "score": 0.9,
            "status": "PASS",
            "summary": f"{name} checked",
            "evidence": ["sampled frame evidence"],
        }
        for name in METRIC_NAMES
    }
    return {
        "metrics": metrics,
        "reference_comparison": {
            "compared_reference_version_ids": list(reference_ids),
            "findings": [
                {
                    "reference_version_id": reference_id,
                    "status": "PASS",
                    "summary": "consistent",
                }
                for reference_id in reference_ids
            ],
        },
        "summary": "qualified AI analysis",
    }


class FakeTransport:
    def __init__(self, *, response=None, delete_failure: bool = False):
        self.uploads = []
        self.payloads = []
        self.deleted = []
        self.response = response or _structured_response()
        self.delete_failure = delete_failure

    def upload_file(self, path, *, mime_type, display_name):
        index = len(self.uploads) + 1
        self.uploads.append(
            {
                "path": Path(path),
                "mime_type": mime_type,
                "display_name": display_name,
            }
        )
        return {
            "file": {
                "name": f"files/file-{index}",
                "uri": f"https://generativelanguage.googleapis.com/v1beta/files/file-{index}",
                "mimeType": mime_type,
                "state": "ACTIVE",
            }
        }

    def get_file(self, name):
        raise AssertionError("ACTIVE upload must not be polled")

    def create_interaction(self, payload):
        self.payloads.append(dict(payload))
        text = self.response if isinstance(self.response, str) else json.dumps(self.response)
        return {
            "id": "interaction_123",
            "status": "completed",
            "steps": [
                {
                    "type": "model_output",
                    "content": [{"type": "text", "text": text}],
                }
            ],
            "usage": {"total_input_tokens": 123, "total_output_tokens": 45},
        }

    def delete_file(self, name):
        self.deleted.append(name)
        if self.delete_failure and len(self.deleted) == 1:
            raise GeminiVisionError("delete failed")


def _provider(transport, **overrides):
    return GeminiVisionProvider(
        GeminiVisionProviderConfig(
            api_key="test-key",
            allow_paid_live_tests=True,
            poll_interval_seconds=0.001,
            **overrides,
        ),
        transport=transport,
        sleep=lambda _: None,
    )


def test_current_official_model_is_pinned_and_live_requires_explicit_opt_in():
    provider = GeminiVisionProvider(
        env={"GEMINI_API_KEY": "configured", "AIDRAMA_ALLOW_PAID_LIVE_TESTS": ""}
    )

    assert DEFAULT_GEMINI_BASE_URL == "https://generativelanguage.googleapis.com/v1beta"
    assert DEFAULT_GEMINI_VISION_MODEL == "gemini-3.7-flash"
    assert provider.status.available is False
    assert provider.status.metadata["model"] == "gemini-3.7-flash"
    assert "authorization" in provider.status.reason


def test_gemini_interactions_contract_preserves_exact_inputs_and_deletes_remote_files(tmp_path):
    transport = FakeTransport()
    request = _request(tmp_path)
    analysis = _provider(transport).analyze(request=request)

    assert len(transport.uploads) == 4
    assert len(transport.payloads) == 1
    assert [item["mime_type"] for item in transport.uploads] == [
        "video/mp4",
        "image/jpeg",
        "image/png",
        "image/webp",
    ]
    payload = transport.payloads[0]
    assert payload["model"] == "gemini-3.7-flash"
    assert payload["store"] is False
    assert [item["type"] for item in payload["input"]] == [
        "text",
        "video",
        "image",
        "image",
        "image",
    ]
    prompt = payload["input"][0]["text"]
    assert "FROZEN_INPUTS_JSON_BEGIN" in prompt
    assert prompt.index("version-hero") < prompt.index("version-room")
    assert "must-not-leak" not in prompt
    assert str(tmp_path) not in str(payload)
    assert payload["response_format"]["mime_type"] == "application/json"
    assert set(payload["response_format"]["schema"]["properties"]["metrics"]["required"]) == set(METRIC_NAMES)
    assert transport.deleted == [
        "files/file-4",
        "files/file-3",
        "files/file-2",
        "files/file-1",
    ]
    assert analysis.metrics["SHOT_COMPLIANCE"]["status"] == "PASS"
    assert set(analysis.metrics) == set(METRIC_NAMES)
    assert len(analysis.metrics) == 7
    assert analysis.metadata["model"] == "gemini-3.7-flash"
    assert analysis.metadata["interaction_id"] == "interaction_123"
    assert analysis.metadata["remote_file_lifecycle"]["deleted_file_count"] == 4
    assert "uri" not in str(analysis.metadata).lower()
    assert "test-key" not in str(analysis.metadata)


def test_invalid_structured_response_still_deletes_every_remote_file(tmp_path):
    transport = FakeTransport(response="not-json")

    with pytest.raises(GeminiVisionError, match="invalid JSON") as error:
        _provider(transport).analyze(request=_request(tmp_path))

    assert len(transport.deleted) == 4
    lifecycle = error.value.safe_metadata["remote_file_lifecycle"]
    assert lifecycle["uploaded_file_count"] == 4
    assert lifecycle["deleted_file_count"] == 4
    assert lifecycle["delete_failure_count"] == 0
    assert lifecycle["fallback_retention"] == "NONE"
    assert "files/file" not in str(lifecycle)


def test_processing_failure_after_upload_still_deletes_known_remote_file(tmp_path):
    class ProcessingFailureTransport(FakeTransport):
        def upload_file(self, path, *, mime_type, display_name):
            value = super().upload_file(
                path, mime_type=mime_type, display_name=display_name
            )
            value["file"]["state"] = "FAILED"
            return value

    transport = ProcessingFailureTransport()

    with pytest.raises(GeminiVisionError, match="processing failed") as error:
        _provider(transport).analyze(request=_request(tmp_path))

    assert transport.deleted == ["files/file-1"]
    lifecycle = error.value.safe_metadata["remote_file_lifecycle"]
    assert lifecycle["uploaded_file_count"] == 1
    assert lifecycle["deleted_file_count"] == 1


def test_reference_provenance_mismatch_is_rejected(tmp_path):
    transport = FakeTransport(response=_structured_response(("version-room", "version-hero")))

    with pytest.raises(GeminiVisionError, match="provenance mismatched"):
        _provider(transport).analyze(request=_request(tmp_path))


def test_remote_delete_failure_is_truthfully_recorded_with_48_hour_fallback(tmp_path):
    transport = FakeTransport(delete_failure=True)

    analysis = _provider(transport).analyze(request=_request(tmp_path))

    lifecycle = analysis.metadata["remote_file_lifecycle"]
    assert lifecycle["delete_failure_count"] == 1
    assert lifecycle["fallback_retention"] == "AUTO_EXPIRES_WITHIN_48_HOURS"
    assert "files/file" not in str(lifecycle)


def test_media_hash_is_verified_before_any_upload(tmp_path):
    transport = FakeTransport()
    request = _request(tmp_path)
    request.video.path.write_bytes(b"tampered")

    with pytest.raises(GeminiVisionError, match="SHA-256 mismatch"):
        _provider(transport).analyze(request=request)

    assert transport.uploads == []


class FakeResponse:
    def __init__(self, status_code=200, *, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if len(self.calls) == 1:
            return FakeResponse(
                headers={
                    "X-Goog-Upload-URL": "https://generativelanguage.googleapis.com/upload/v1beta/files?upload_id=memory-only"
                }
            )
        return FakeResponse(
            payload={
                "file": {
                    "name": "files/uploaded-1",
                    "uri": "https://generativelanguage.googleapis.com/v1beta/files/uploaded-1",
                    "mimeType": "video/mp4",
                    "state": "ACTIVE",
                }
            }
        )


def test_raw_http_transport_uses_official_resumable_upload_endpoint(tmp_path):
    path, _ = _write(tmp_path / "shot.mp4", b"video")
    session = FakeSession()
    transport = GeminiHTTPTransport(
        GeminiVisionProviderConfig(api_key="test-key"), session=session
    )

    result = transport.upload_file(path, mime_type="video/mp4", display_name="shot.mp4")

    first_method, first_url, first_options = session.calls[0]
    assert first_method == "POST"
    assert first_url == "https://generativelanguage.googleapis.com/upload/v1beta/files"
    assert first_options["headers"]["X-Goog-Upload-Protocol"] == "resumable"
    assert session.calls[1][1].startswith(
        "https://generativelanguage.googleapis.com/upload/v1beta/files?"
    )
    assert result["file"]["name"] == "files/uploaded-1"
    assert "test-key" not in str(result)
