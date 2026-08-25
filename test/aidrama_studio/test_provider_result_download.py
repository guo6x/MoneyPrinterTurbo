from __future__ import annotations

import io
import socket

import pytest
import requests

from aidrama_studio.services.provider_result_download import (
    ProviderResultDownloadError,
    ProviderResultDownloader,
    ProviderResultPolicy,
    validate_mp4_prefix,
)


MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"video" * 32


def _public_resolver(host, port, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class Response:
    def __init__(self, chunks=(), *, status=200, headers=None):
        self._chunks = chunks
        self.status_code = status
        self.headers = dict(headers or {})
        self.closed = False

    @property
    def content(self):
        raise AssertionError("streaming downloader must not access response.content")

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        yield from self._chunks

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _downloader(session, *, max_bytes=1024, resolver=_public_resolver):
    return ProviderResultDownloader(
        ProviderResultPolicy(("cdn.provider.example",), max_bytes),
        session=session,
        resolver=resolver,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.provider.example/video.mp4",
        "https://user:pass@cdn.provider.example/video.mp4",
        "https://cdn.provider.example:443/video.mp4",
        "https://evil.cdn.provider.example/video.mp4",
        "https://cdn.provider.example\\@127.0.0.1/video.mp4",
    ],
)
def test_result_url_rejects_unsafe_authority_before_network(url):
    session = Session([])
    with pytest.raises(ProviderResultDownloadError):
        _downloader(session).source(url)
    assert session.calls == []


def test_result_url_rejects_any_non_public_dns_answer():
    def resolver(host, port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    source = _downloader(Session([]), resolver=resolver).source(
        "https://cdn.provider.example/video.mp4"
    )
    with pytest.raises(ProviderResultDownloadError, match="non-public"):
        source.write_to(io.BytesIO())


def test_result_download_streams_valid_mp4_and_never_materializes_content():
    response = Response(
        [MP4[:9], MP4[9:]],
        headers={"Content-Type": "video/mp4", "Content-Length": str(len(MP4))},
    )
    session = Session([response])
    source = _downloader(session).source(
        "https://cdn.provider.example/video.mp4?X-Amz-Signature=secret",
        prefix_validator=validate_mp4_prefix,
    )
    sink = io.BytesIO()
    source.write_to(sink)

    assert sink.getvalue() == MP4
    assert response.closed is True
    assert session.calls[0][1]["allow_redirects"] is False
    assert "Authorization" not in session.calls[0][1]["headers"]


def test_result_download_revalidates_redirect_and_rejects_new_host():
    redirect = Response(
        status=302,
        headers={"Location": "https://untrusted.example/private.mp4"},
    )
    session = Session([redirect])
    source = _downloader(session).source("https://cdn.provider.example/video.mp4")
    with pytest.raises(ProviderResultDownloadError, match="allowlisted"):
        source.write_to(io.BytesIO())
    assert redirect.closed is True
    assert len(session.calls) == 1


def test_result_download_enforces_actual_size_and_closes_interrupted_response():
    oversized = Response([b"a" * 8, b"b" * 8])
    source = _downloader(Session([oversized]), max_bytes=12).source(
        "https://cdn.provider.example/video.mp4"
    )
    with pytest.raises(ProviderResultDownloadError, match="limit"):
        source.write_to(io.BytesIO())
    assert oversized.closed is True

    class Interrupted(Response):
        def iter_content(self, chunk_size):
            yield b"partial"
            raise requests.ConnectionError("url=https://cdn.provider.example/?token=secret")

    interrupted = Interrupted()
    source = _downloader(Session([interrupted])).source(
        "https://cdn.provider.example/video.mp4"
    )
    with pytest.raises(ProviderResultDownloadError, match="transport failed") as error:
        source.write_to(io.BytesIO())
    assert "secret" not in str(error.value)
    assert interrupted.closed is True


def test_result_download_rejects_header_oversize_and_fake_media():
    oversized = Response(
        [MP4],
        headers={"Content-Length": "2048", "Content-Type": "video/mp4"},
    )
    source = _downloader(Session([oversized]), max_bytes=1024).source(
        "https://cdn.provider.example/video.mp4"
    )
    with pytest.raises(ProviderResultDownloadError, match="limit"):
        source.write_to(io.BytesIO())

    html = Response([b"<html>not video</html>"], headers={"Content-Type": "video/mp4"})
    source = _downloader(Session([html])).source(
        "https://cdn.provider.example/video.mp4",
        prefix_validator=validate_mp4_prefix,
    )
    with pytest.raises(ProviderResultDownloadError, match="MP4"):
        source.write_to(io.BytesIO())
