from __future__ import annotations

import hashlib
import socket

import pytest

from aidrama_studio.services.model_runtime.codecs import CodecError
from aidrama_studio.services.model_runtime.contracts import (
    CapabilityKind,
    CapabilityRequest,
    DriverResponse,
    ProtocolFamily,
)
from aidrama_studio.services.model_runtime.drivers import (
    RequestResponseDriver,
    TransportError,
)
from aidrama_studio.services.model_runtime.mainland_codecs import (
    DashScopeWanI2VCodec,
    DashScopeZImageCodec,
)
from aidrama_studio.services.model_runtime.mainland_manifests import (
    build_mainland_manifests,
)
from aidrama_studio.services.model_runtime.mainland_runtime import (
    ContentAddressedArtifactSink,
    MainlandProviderRuntime,
)


MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"video" * 32


def _public_resolver(host, port, **_kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
    ]


class Response:
    def __init__(self, chunks=(), *, status=200):
        self._chunks = tuple(chunks)
        self.status_code = status
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        yield from self._chunks

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _image_manifest():
    return next(
        manifest
        for manifest in build_mainland_manifests(artifact_sink_available=True)
        if manifest.capability is CapabilityKind.IMAGE
    )


def _request(manifest, *, resolution="1024*1024", prompt_extend=None):
    parameters = {"resolution": resolution}
    if prompt_extend is not None:
        parameters["prompt_extend"] = prompt_extend
    return CapabilityRequest(
        request_id=f"request-{resolution}",
        capability=manifest.capability,
        protocol_family=manifest.protocol,
        provider_id=manifest.provider_id,
        model_id=manifest.model_id,
        manifest_id=manifest.id,
        manifest_hash=manifest.manifest_hash,
        codec_id=manifest.codec_id,
        prompt_or_text="A cinematic frame",
        provider_parameters=parameters,
    )


@pytest.mark.parametrize("resolution", ["1280*720", "720*1280"])
def test_zimage_contract_supports_landscape_and_portrait_without_n(resolution):
    manifest = _image_manifest()
    encoded = DashScopeZImageCodec().encode_request(
        _request(manifest, resolution=resolution), manifest
    )

    parameters = encoded.payload["parameters"]
    assert parameters == {"size": resolution}
    assert "n" not in parameters
    assert "prompt_extend" not in parameters


def test_zimage_prompt_extend_is_opt_in_and_dimensions_are_bounded():
    manifest = _image_manifest()
    encoded = DashScopeZImageCodec().encode_request(
        _request(manifest, prompt_extend=True), manifest
    )
    assert encoded.payload["parameters"]["prompt_extend"] is True

    for resolution in ("511*512", "512*2049"):
        with pytest.raises(CodecError, match="512 to 2048"):
            DashScopeZImageCodec().encode_request(
                _request(manifest, resolution=resolution), manifest
            )


def test_mainland_quality_profile_and_paid_create_retry_count_are_frozen():
    manifests = build_mainland_manifests()
    qwen = next(
        manifest
        for manifest in manifests
        if manifest.capability is CapabilityKind.LLM
        and manifest.provider_id == "alibaba_model_studio"
    )
    assert qwen.selection_policy["profile"] == "MAINLAND_QUALITY"

    runtime = MainlandProviderRuntime(credentials={})
    request_response_bindings = [
        runtime.binding_for(manifest.id)
        for manifest in runtime.manifests
        if manifest.protocol is ProtocolFamily.REQUEST_RESPONSE
    ]
    assert request_response_bindings
    assert all(
        isinstance(binding.driver, RequestResponseDriver)
        and binding.driver.max_retries == 0
        for binding in request_response_bindings
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.provider.example/result.mp4",
        "https://127.0.0.1/result.mp4",
        "https://10.0.0.1/result.mp4",
        "https://169.254.1.1/result.mp4",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_artifact_sink_rejects_non_public_destinations_before_network(tmp_path, url):
    session = Session()
    sink = ContentAddressedArtifactSink(
        tmp_path, session=session, resolver=_public_resolver
    )

    with pytest.raises(TransportError):
        sink.persist_remote(
            url,
            request_id="request-unsafe",
            role="generated_video",
            mime_type="video/mp4",
            safe_metadata={},
        )
    assert session.calls == []


def test_artifact_sink_rejects_any_private_dns_answer_before_network(tmp_path):
    def mixed_resolver(host, port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    session = Session()
    sink = ContentAddressedArtifactSink(
        tmp_path, session=session, resolver=mixed_resolver
    )
    with pytest.raises(TransportError, match="not public"):
        sink.persist_remote(
            "https://cdn.provider.example/result.mp4",
            request_id="request-dns",
            role="generated_video",
            mime_type="video/mp4",
            safe_metadata={},
        )
    assert session.calls == []


def test_artifact_sink_rejects_redirects_and_disables_redirect_following(tmp_path):
    response = Response(status=302)
    session = Session([response])
    sink = ContentAddressedArtifactSink(
        tmp_path, session=session, resolver=_public_resolver
    )

    with pytest.raises(TransportError, match="redirects are not allowed"):
        sink.persist_remote(
            "https://cdn.provider.example/result.mp4",
            request_id="request-redirect",
            role="generated_video",
            mime_type="video/mp4",
            safe_metadata={},
        )
    assert session.calls[0][1]["allow_redirects"] is False
    assert response.closed is True


def test_wan_dynamic_result_url_becomes_content_addressed_artifact(tmp_path):
    response = Response([MP4])
    session = Session([response])
    sink = ContentAddressedArtifactSink(
        tmp_path, session=session, resolver=_public_resolver
    )
    codec = DashScopeWanI2VCodec(artifact_sink=sink)
    request = CapabilityRequest(
        request_id="request-wan-result",
        capability=CapabilityKind.VIDEO,
        protocol_family=ProtocolFamily.ASYNC_TASK,
        provider_id="alibaba_model_studio",
        model_id="wan2.7-i2v-2026-04-25",
        codec_id=codec.codec_id,
    )
    result = codec.decode_result(
        DriverResponse(
            {
                "output": {
                    "task_status": "SUCCEEDED",
                    "task_id": "wan-task-1",
                    "video_url": (
                        "https://bucket.oss-cn-beijing.aliyuncs.com/result.mp4"
                    ),
                }
            }
        ),
        "wan-task-1",
        request,
    )

    expected_hash = hashlib.sha256(MP4).hexdigest()
    artifact = result.outputs[0]
    assert artifact.source_kind == "CONTENT_ADDRESSED_ARTIFACT"
    assert artifact.source_id == f"sha256:{expected_hash}"
    assert artifact.sha256 == expected_hash
    assert sink.path_for(artifact).read_bytes() == MP4
    assert "http" not in repr(artifact).lower()
    assert session.calls[0][1]["allow_redirects"] is False
    assert response.closed is True
