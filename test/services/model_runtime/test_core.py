from __future__ import annotations

import json

import pytest

from aidrama_studio.services.model_runtime import (
    AsyncTaskDriver,
    CapabilityRequest,
    CreateAuthorizationError,
    JsonAsyncTaskCodec,
    JsonProviderCodec,
    JsonStreamCodec,
    MalformedProviderResult,
    ManifestValidationError,
    ModelManifest,
    ProtocolFamily,
    RequestResponseDriver,
    RuntimeOutcome,
    StreamDriver,
)


def _manifest(*, protocol: str = "REQUEST_RESPONSE", **kwargs) -> ModelManifest:
    kwargs.setdefault("configured", True)
    kwargs.setdefault("runtime_available", True)
    return ModelManifest(
        id=f"fake:{protocol.lower()}:v1",
        display_name="Fake model",
        provider_id="fake",
        capability="LLM",
        protocol=protocol,
        model_id="fake-model",
        codec_id="generic.json",
        **kwargs,
    )


def _request(manifest: ModelManifest) -> CapabilityRequest:
    return CapabilityRequest(
        request_id="request-1",
        capability=manifest.capability,
        protocol_family=manifest.protocol,
        provider_id=manifest.provider_id,
        model_id=manifest.model_id,
        manifest_id=manifest.id,
        manifest_hash=manifest.manifest_hash,
        codec_id=manifest.codec_id,
    )


def test_manifest_is_immutable_and_hash_serialization_is_stable() -> None:
    manifest = _manifest(
        duration={"minimum": 2, "maximum": 8, "discrete_values": [2, 4, 8]},
        supports={"structured_output": True},
    )
    assert manifest.protocol_family is ProtocolFamily.REQUEST_RESPONSE
    assert manifest.manifest_hash in manifest.serialize()
    assert json.loads(manifest.serialize())["capability"] == "LLM"
    with pytest.raises((AttributeError, TypeError)):
        manifest.model_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        manifest.selection_policy["x"] = 1  # type: ignore[index]


def test_manifest_rejects_secret_metadata_and_invalid_limits() -> None:
    with pytest.raises(ManifestValidationError):
        _manifest(pricing={"api_key": "do-not-store"})
    with pytest.raises(ManifestValidationError):
        _manifest(duration={"minimum": 10, "maximum": 2})


def test_request_response_driver_and_malformed_result_fail_closed() -> None:
    class Transport:
        calls = 0

        def send(self, encoded):
            self.calls += 1
            return {"outcome": "SUCCEEDED"}

    transport = Transport()
    manifest = _manifest()
    result = RequestResponseDriver(transport).invoke(_request(manifest), JsonProviderCodec(), manifest)
    assert result.outcome is RuntimeOutcome.SUCCEEDED
    assert transport.calls == 1

    class BadTransport:
        def send(self, encoded):
            return {"not_a_result": True}

    with pytest.raises(MalformedProviderResult):
        RequestResponseDriver(BadTransport()).invoke(_request(manifest), JsonProviderCodec(), manifest)


def test_async_create_identity_poll_and_reconcile_never_resubmit() -> None:
    class Transport:
        create_calls = 0
        poll_calls = 0

        def create(self, encoded):
            self.create_calls += 1
            return {"task_id": "remote-123"}

        def poll(self, reference):
            self.poll_calls += 1
            return {"status": "RUNNING", "task_id": reference}

        def fetch_result(self, reference):
            return {"outcome": "SUCCEEDED", "task_id": reference}

    manifest = _manifest(protocol="ASYNC_TASK")
    request = _request(manifest)
    transport = Transport()
    driver = AsyncTaskDriver(transport)
    codec = JsonAsyncTaskCodec()
    submission = driver.create(request, codec, manifest)
    assert submission.protocol_reference == "remote-123"
    assert transport.create_calls == 1
    assert driver.poll(submission.protocol_reference, codec).outcome is RuntimeOutcome.RUNNING
    assert driver.reconcile(submission.protocol_reference, codec).outcome is RuntimeOutcome.RUNNING
    assert transport.create_calls == 1
    assert transport.poll_calls == 2
    assert driver.collect(submission.protocol_reference, codec).succeeded


def test_async_create_authorization_blocks_transport_and_configuration_is_independent() -> None:
    class Transport:
        create_calls = 0

        def create(self, encoded):
            self.create_calls += 1
            return {"task_id": "should-not-happen"}

    manifest = _manifest(
        protocol="ASYNC_TASK",
        authorization={"create_is_paid": True, "requires_create_authorization": True},
        configured=True,
        verified=False,
        runtime_available=True,
        create_authorized=False,
    )
    assert manifest.configured is True
    assert manifest.verified is False
    assert manifest.runtime_available is True
    assert manifest.authorization_required is True
    with pytest.raises(CreateAuthorizationError):
        AsyncTaskDriver(Transport()).create(_request(manifest), JsonAsyncTaskCodec(), manifest)


def test_async_cancel_is_optional() -> None:
    class Transport:
        def create(self, encoded):
            return {"task_id": "remote"}

        def poll(self, reference):
            return {"status": "RUNNING"}

        def fetch_result(self, reference):
            return {"outcome": "SUCCEEDED"}

    manifest = _manifest(protocol="ASYNC_TASK")
    driver = AsyncTaskDriver(Transport())
    submission = driver.create(_request(manifest), JsonAsyncTaskCodec(), manifest)
    assert driver.cancel(submission.protocol_reference, JsonAsyncTaskCodec()) is False


def test_stream_driver_decodes_incremental_output() -> None:
    class Transport:
        def open(self, encoded):
            return ["a", "b", "c"]

    manifest = _manifest(protocol="STREAM")
    result = StreamDriver(Transport()).invoke(_request(manifest), JsonStreamCodec(), manifest)
    assert result.succeeded
    assert result.safe_metadata["chunk_count"] == 3
