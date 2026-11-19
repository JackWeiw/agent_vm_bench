"""Tests for the vendored envd client's Connect wire handling.

These lock the review fixes: a stream cut before the end-of-stream frame is
truncation and raises (no phantom exit-0 success); the end-of-stream envelope
carries the RPC error under the ``error`` key only (metadata-only trailers are
a success); and a clean end without a wire-visible exit code is 0 (proto3
omits zero scalars).
"""

from __future__ import annotations

import io
import json

import pytest

from env_provider.sandboxsched.envd_client import (
    EnvdClient,
    EnvdError,
    _frame,
    _iter_connect_frames,
    _ld,
    _parse_trailers,
    _tag,
    _varint,
)


# -------------------------------------------------------------------- helpers
def _stream(*frames: bytes) -> io.BytesIO:
    return io.BytesIO(b"".join(frames))


def _stdout_event(text: bytes) -> bytes:
    # StartResponse.event(1) -> ProcessEvent.data(2) -> DataEvent.stdout(1)
    return _ld(1, _ld(2, _ld(1, text)))


def _exit_event(code: int) -> bytes:
    # StartResponse.event(1) -> ProcessEvent.end(3) -> EndEvent.exit_code(1, sint32 zigzag);
    # a zero exit code omits the scalar on the wire (proto3), so code=0 -> empty EndEvent.
    body = (_tag(1, 0) + _varint(code << 1)) if code else b""
    return _ld(1, _ld(3, body))


def _end_frame(*, error: dict | None = None, metadata: dict | None = None) -> bytes:
    meta: dict = {}
    if metadata:
        meta["metadata"] = metadata
    if error:
        meta["error"] = error
    payload = json.dumps(meta).encode()
    # end-of-stream (trailers) frame: flag bit 0x02, unlike data frames (0x00)
    return b"\x02" + len(payload).to_bytes(4, "big") + payload


def _client_with(data: bytes) -> EnvdClient:
    """An EnvdClient whose transport replays ``data`` (no network)."""
    client = EnvdClient("http://envd.test:49983")

    class _FakeResponse(io.BytesIO):
        def close(self):
            pass

    client._request = lambda *args, **kwargs: _FakeResponse(data)  # noqa: ARG005
    return client


# -------------------------------------------------------------------- frames
class TestIterConnectFrames:
    def test_clean_eof_at_boundary_ends_iteration(self):
        frames = list(_iter_connect_frames(_stream(_frame(b"x"))))
        assert frames == [(0x00, b"x")]

    def test_partial_header_raises(self):
        cut = _frame(b"payload")[:-2]
        with pytest.raises(EnvdError, match="truncated"):
            list(_iter_connect_frames(_stream(cut)))

    def test_short_payload_raises(self):
        full = _frame(b"payload")
        header, _ = full[:5], full[5:]
        with pytest.raises(EnvdError, match="payload cut"):
            list(_iter_connect_frames(_stream(header + b"pay")))


# -------------------------------------------------------------------- trailers
class TestParseTrailers:
    def test_empty_object_is_success(self):
        assert _parse_trailers(b"{}") is None

    def test_metadata_only_is_success(self):
        assert _parse_trailers(json.dumps({"metadata": {"a": "b"}}).encode()) is None

    def test_error_under_error_key_is_reported(self):
        body = json.dumps({"metadata": {}, "error": {"code": "X", "message": "boom"}}).encode()
        assert _parse_trailers(body) == "code=X message=boom"

    def test_unparsable_is_reported(self):
        assert _parse_trailers(b"not json") is not None


# -------------------------------------------------------------------- run()
class TestRun:
    def test_clean_stream_with_omitted_zero_exit_is_success(self):
        data = _frame(_stdout_event(b"hello") + _exit_event(0)) + _end_frame()
        result = _client_with(data).run("true")
        assert result.exit_code == 0
        assert result.stdout == b"hello"

    def test_explicit_exit_code_is_kept(self):
        data = _frame(_exit_event(137)) + _end_frame()
        result = _client_with(data).run("kill-me")
        assert result.exit_code == 137

    def test_metadata_only_trailers_still_clean(self):
        data = _frame(_stdout_event(b"ok")) + _end_frame(metadata={"server": "envd"})
        result = _client_with(data).run("true")
        assert result.exit_code == 0
        assert result.stdout == b"ok"

    def test_stream_without_end_frame_raises(self):
        # stdout frames only: the server cut the stream before the command ended
        data = _frame(_stdout_event(b"partial"))
        with pytest.raises(EnvdError, match="without an end-of-stream frame"):
            _client_with(data).run("killed")

    def test_rpc_error_in_envelope_raises(self):
        data = _frame(_stdout_event(b"x")) + _end_frame(error={"code": " PERMISSIONS", "message": "no"})
        with pytest.raises(EnvdError, match="code= PERMISSIONS"):
            _client_with(data).run("denied")
