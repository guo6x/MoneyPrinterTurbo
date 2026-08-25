from __future__ import annotations

from pathlib import Path

from loguru import logger

from aidrama_studio.services import (
    configure_runtime_logging,
    sanitize_error,
    sanitize_persistent_metadata,
)


def test_error_redaction_removes_secrets_signed_query_and_private_paths():
    value = sanitize_error(
        r"Authorization: Bearer top-secret API_KEY=sk-abcdefghijklmnopqrstuvwxyz failed C:\Users\alice\project\x.py https://cdn.example.com/result.mp4?X-Amz-Signature=secret&token=bad"
    )
    assert "top-secret" not in value
    assert "sk-" not in value
    assert "alice" not in value
    assert "Signature" not in value
    assert "token=" not in value
    assert "https://cdn.example.com/result.mp4" in value


def test_runtime_log_is_rotating_redacted_and_outside_project(tmp_path: Path):
    target = configure_runtime_logging(tmp_path / "data")
    logger.error(r"token=super-secret C:\Users\alice\private.txt")
    logger.complete()
    text = target.read_text(encoding="utf-8")
    assert "super-secret" not in text
    assert "alice" not in text
    assert target.parent.name == "logs"


def test_embedded_signed_url_and_userinfo_are_redacted():
    value = sanitize_error(
        "download(url=https://user:pass@cdn.example/result.mp4?Policy=leak&X-Amz-Credential=also-leak)"
    )
    assert "user" not in value
    assert "pass" not in value
    assert "Policy" not in value
    assert "Credential" not in value
    assert "https://cdn.example/result.mp4" in value


def test_persistent_metadata_recursively_drops_secret_and_result_urls():
    safe = sanitize_persistent_metadata(
        {
            "provider": "SEEDANCE",
            "nested": {
                "Authorization": "Bearer secret",
                "video_url": "https://cdn.example/video.mp4?Signature=secret",
                "model": "model-1",
            },
            "trace": [r"failed at C:\Users\alice\private.txt"],
        }
    )
    text = repr(safe)
    assert "secret" not in text
    assert "video_url" not in text
    assert "alice" not in text
    assert safe["nested"]["model"] == "model-1"
