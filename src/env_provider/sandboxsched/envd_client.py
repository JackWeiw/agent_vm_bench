# Copyright 2026 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# NOTE: vendored from opensandbox examples/flux-sandbox-e2b/envd_client.py with
# local correctness fixes -- no longer byte-identical to upstream. Local deltas:
# _iter_connect_frames (mid-frame truncation raises instead of ending silently),
# _parse_trailers (end-of-stream error parsed under the "error" key only), run()
# (a stream with neither exit code nor end-of-stream frame raises instead of
# reporting a phantom success), and EnvdTruncatedError (truncation is a distinct
# error type so callers can tell it apart from an RPC failure). Only the DATA
# plane is vendored (exec + health, standard library only); the control plane is
# the async opensandbox SDK, driven by manager.py.

"""envd data-plane client for E2B-mode sandboxes (standard library only).

Why this module exists
----------------------
A sandbox running under the flux runtime in E2B mode exposes **envd** on port
49983. envd speaks Connect RPC, which is *not* the REST contract the OpenSandbox
SDK's ``commands`` / ``files`` APIs are built against (``/command``,
``/files/upload``, ``/files/download``). Those paths do not exist on envd, and
the SDK has no Connect client -- so command execution goes through this module.

Scope: the DATA plane only -- ``run`` (POST /process.Process/Start, streaming),
``health`` (GET /health -> 204). Create / connect / pause / resume / kill /
list / endpoint resolution are the CONTROL plane and live in :mod:`manager`
through the async ``opensandbox`` SDK.

Reaching envd
-------------
Selected by ``use_server_proxy`` (the manager resolves the URL once per sandbox
through the control plane and hands this client the base URL):

``proxy``
    ``http://<server>/sandboxes/<id>/proxy/{port}`` -- the OpenSandbox server
    forwards to the sandbox. No CNI-network reachability required on the client,
    and the proxy path needs no API key.

``direct``
    ``http://<CNI-IP>:{port}`` -- the client must be able to reach the sandbox
    network (typically from a cluster node).

Known limitations
-----------------
* The proxy strips the ``Authorization`` header, so Basic Auth cannot be used to
  select the executing user in ``proxy`` mode. envd's Basic Auth only picks the OS
  user anyway (the password is never checked -- see
  ``packages/envd/internal/permissions/authenticate.go``). To run as a specific
  user in proxy mode, wrap the command with ``sudo -u <user>`` inside the
  command string.
* If envd is configured with an access token, pass it via ``access_token``.
  ``X-Access-Token`` is not stripped by the proxy.
* The OpenSandbox server applies a 180s timeout to its upstream request. Commands
  that produce no output for longer than that may have their stream cut mid-flight;
  :meth:`EnvdClient.run` surfaces that as :class:`EnvdTruncatedError` rather than a
  phantom success. Long-running commands that keep emitting output are fine.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from collections.abc import Callable, Iterator

#: envd's fixed listening port. In E2B mode flux-sandbox writes
#: ``<CNI-IP>:49983`` into the Sandbox CR's ``status.endpoints``, so resolving
#: this port hits the endpoint exactly. The manager's ``envd_port`` config knob
#: defaults to this value.
ENVD_PORT = 49983


class FluxSandboxError(RuntimeError):
    """Base class for every error this module raises.

    Callers that just want "something went wrong" can catch this one type instead
    of tracking which layer failed.
    """


class EnvdError(FluxSandboxError):
    """envd returned an error (non-2xx HTTP, or a Connect RPC-level failure)."""


class EnvdTruncatedError(EnvdError):
    """The exec stream was cut before the command finished.

    connect-go ALWAYS emits the end-of-stream frame on a clean finish, so a
    stream ending without it (partial frame header/payload, or no trailers at
    all) is a detectable truncation: a signal-killed command, or the server's
    ~180s upstream timeout dropping the connection. Distinct from
    :class:`EnvdError` so callers can report truncation as its own failure mode
    (manager.py maps it to its own exit code) instead of conflating it with a
    command timeout or an RPC rejection.
    """


# ============================================================================
# protobuf wire format -- encoding
# ============================================================================

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field_num: int, wire_type: int) -> bytes:
    return _varint(field_num << 3 | wire_type)


def _ld(field_num: int, payload: bytes) -> bytes:
    """Length-delimited field (wire type 2)."""
    return _tag(field_num, 2) + _varint(len(payload)) + payload


def _str(field_num: int, value: str) -> bytes:
    return _ld(field_num, value.encode("utf-8"))


def _bool(field_num: int, value: bool) -> bytes:
    return _tag(field_num, 0) + _varint(1 if value else 0)


# ============================================================================
# protobuf wire format -- decoding
# ============================================================================

def _read_varint(buf: bytes, pos: int):
    """Return (value, bytes_consumed); (None, 0) when out of bounds."""
    value, shift = 0, 0
    for i in range(pos, min(pos + 10, len(buf))):
        value |= (buf[i] & 0x7F) << shift
        shift += 7
        if not buf[i] & 0x80:
            return value, i - pos + 1
    return None, 0


def _iter_fields(buf: bytes) -> Iterator[tuple]:
    """Iterate protobuf fields, yielding ``(field_num, wire_type, value)``.

    Wire type 0 yields an int, wire type 2 yields bytes. Other wire types stop
    iteration -- envd's responses do not use them.
    """
    pos = 0
    while pos < len(buf):
        tag, consumed = _read_varint(buf, pos)
        if not consumed:
            return
        pos += consumed
        field_num, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, consumed = _read_varint(buf, pos)
            if not consumed:
                return
            pos += consumed
            yield field_num, wire_type, value
        elif wire_type == 2:
            length, consumed = _read_varint(buf, pos)
            if not consumed or pos + consumed + length > len(buf):
                return
            pos += consumed
            yield field_num, wire_type, buf[pos:pos + length]
            pos += length
        else:
            return


# ============================================================================
# Connect protocol -- request framing and response stream parsing
# ============================================================================

def build_start_request(
    command: str,
    envs: dict | None = None,
    cwd: str | None = None,
    stdin: bool = False,
) -> bytes:
    """Build a framed ``process.Process/Start`` request body.

    Maps to the upstream proto::

        StartRequest {
          ProcessConfig process = 1;   // cmd=1, args=2, envs=3 (map), cwd=4
          PTY pty = 2;                 // unused: non-PTY keeps stdout/stderr split
          string tag = 3;
          optional bool stdin = 4;     // defaults to true; false avoids stdin waits
        }
    """
    process = _str(1, "/bin/bash") + _str(2, "-c") + _str(2, command)
    for key, value in (envs or {}).items():
        # map<string,string> encodes as repeated entry{key=1, value=2}
        process += _ld(3, _str(1, key) + _str(2, value))
    if cwd:
        process += _str(4, cwd)

    return _frame(_ld(1, process) + _bool(4, stdin))


def _frame(payload: bytes) -> bytes:
    """Connect frame header: 1 byte flags (0 = uncompressed) + 4 byte big-endian length."""
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def _read_exact(stream, size: int) -> bytes:
    """Read exactly ``size`` bytes, returning early on EOF."""
    chunks, remaining = [], size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _iter_connect_frames(stream) -> Iterator[tuple]:
    """Parse a Connect response stream, yielding ``(flags, payload)``.

    ``flags`` 0x00 is a protobuf message frame, 0x02 the trailing trailers frame.
    A clean EOF at a frame boundary ends the iteration; a stream cut mid-frame
    (partial header or payload -- a killed process, or the server's ~180s
    upstream timeout dropping the connection) raises :class:`EnvdTruncatedError`
    instead of ending silently, so a truncated exec is never mistaken for a
    finished one.
    """
    while True:
        header = _read_exact(stream, 5)
        if not header:
            return  # clean EOF at a frame boundary
        if len(header) < 5:
            raise EnvdTruncatedError("connect stream truncated: partial frame header")
        flags = header[0]
        length = int.from_bytes(header[1:5], "big")
        payload = _read_exact(stream, length)
        if len(payload) < length:
            raise EnvdTruncatedError(
                f"connect stream truncated: payload cut at {len(payload)}/{length} bytes"
            )
        yield flags, payload


def _parse_trailers(payload: bytes) -> str | None:
    """Return an error description from a trailers frame, or None on success.

    connect-go wraps the end-of-stream envelope as ``{"metadata": ...,
    "error": {"code": ..., "message": ...}}`` -- the error lives under the
    ``error`` key only. A frame carrying metadata but no error is a success
    (misreporting it fails clean streams); envd's empty ``{}`` end frame also
    passes. A genuine RPC rejection's code/message ride inside ``error``.
    """
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        meta = json.loads(text)
    except ValueError:
        return f"unparsable trailers: {text}"
    if not meta:
        return None
    error = meta.get("error") if isinstance(meta, dict) else None
    if not error:
        return None  # metadata-only end-of-stream: not a failure
    if isinstance(error, dict):
        return f"code={error.get('code', '?')} message={error.get('message', '')}"
    return f"code={error}"


def _iter_process_events(payload: bytes) -> Iterator[tuple]:
    """Yield ``('stdout'|'stderr'|'pty'|'exit', value)`` from a ``StartResponse``."""
    for field_num, wire_type, value in _iter_fields(payload):
        if field_num == 1 and wire_type == 2:  # StartResponse.event
            yield from _iter_event(value)


def _iter_event(event: bytes) -> Iterator[tuple]:
    """``ProcessEvent`` oneof: start=1, data=2, end=3, keepalive=4."""
    for field_num, wire_type, value in _iter_fields(event):
        if wire_type != 2:
            continue
        if field_num == 2:  # DataEvent: stdout=1, stderr=2, pty=3
            for out_field, out_wire, out_value in _iter_fields(value):
                if out_wire != 2:
                    continue
                if out_field == 1:
                    yield "stdout", out_value
                elif out_field == 2:
                    yield "stderr", out_value
                elif out_field == 3:
                    yield "pty", out_value
        elif field_num == 3:  # EndEvent: exit_code=1 sint32 (zigzag)
            for end_field, end_wire, end_value in _iter_fields(value):
                if end_field == 1 and end_wire == 0:
                    yield "exit", (end_value >> 1) ^ -(end_value & 1)


# ============================================================================
# envd client
# ============================================================================

@dataclass
class RunResult:
    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class EnvdClient:
    """Talk to envd directly, using its native protocol.

    ``base_url`` is either ``http://<CNI-IP>:<port>`` (direct) or
    ``http://<server>/sandboxes/<id>/proxy/<port>`` (proxy), resolved by the
    manager through the control plane.
    """

    base_url: str
    access_token: str | None = None
    user: str | None = None         # consumed by manager._UserEnvdClient's Basic auth
    timeout: float = 60.0

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = "http://" + self.base_url

    # -- request plumbing --------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ):
        merged = {"Accept-Encoding": "identity"}
        if self.access_token:
            merged["X-Access-Token"] = self.access_token
        merged.update(headers or {})

        req = urllib.request.Request(
            self.base_url + path, data=data, headers=merged, method=method
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise EnvdError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise EnvdError(f"cannot reach envd ({self.base_url}): {exc.reason}") from exc

    # -- health ------------------------------------------------------------

    def health(self, timeout: float | None = None) -> bool:
        """``GET /health``; envd answers 204 when healthy.

        ``timeout`` overrides the client default, which matters when polling a
        sandbox that is still booting: a hung connection should not consume the
        whole readiness budget.
        """
        return self._request("GET", "/health", timeout=timeout).status == 204

    # -- command execution -------------------------------------------------

    def run(
        self,
        command: str,
        *,
        envs: dict | None = None,
        cwd: str | None = None,
        on_stdout: Callable[[bytes], None] | None = None,
        on_stderr: Callable[[bytes], None] | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        """Run a shell command in the sandbox, streaming its output.

        ``Start`` is a server-streaming RPC: one ``ProcessEvent`` per frame. Frames
        are consumed as they arrive, so long-running commands report output live.

        Every clean stream ends with the end-of-stream (trailers) frame; an
        ``EndEvent`` carrying a zero exit code omits the scalar on the wire
        (proto3), so "trailers seen, no exit event" means exit 0. A stream that
        ends with neither an exit code nor the trailers frame was cut before the
        command finished (killed process, the server's ~180s upstream timeout)
        and raises :class:`EnvdTruncatedError` instead of reporting a phantom
        success.
        """
        body = build_start_request(command, envs=envs, cwd=cwd)
        resp = self._request(
            "POST",
            "/process.Process/Start",
            data=body,
            headers={"Content-Type": "application/connect+proto"},
            timeout=timeout or self.timeout,
        )

        stdout, stderr = [], []
        exit_code = None
        saw_trailers = False
        try:
            for flags, payload in _iter_connect_frames(resp):
                if flags & 0x01:
                    raise EnvdError("received a compressed frame (flags=0x01)")
                if flags & 0x02:
                    saw_trailers = True
                    error = _parse_trailers(payload)
                    if error:
                        raise EnvdError(f"Connect RPC failed: {error}")
                    continue
                for kind, value in _iter_process_events(payload):
                    if kind == "stdout":
                        stdout.append(value)
                        if on_stdout:
                            on_stdout(value)
                    elif kind in ("stderr", "pty"):
                        stderr.append(value)
                        if on_stderr:
                            on_stderr(value)
                    elif kind == "exit":
                        exit_code = value
        finally:
            resp.close()

        if exit_code is None:
            if not saw_trailers:
                raise EnvdTruncatedError(
                    "connect stream ended without an end-of-stream frame: exec "
                    f"truncated ({len(b''.join(stdout))} stdout / "
                    f"{len(b''.join(stderr))} stderr bytes captured)"
                )
            exit_code = 0  # clean end: proto3 omits a zero exit code on the wire
        return RunResult(exit_code, b"".join(stdout), b"".join(stderr))

