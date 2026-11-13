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

"""Client for E2B (envd) sandboxes: OpenSandbox control plane + raw envd data plane.

Why this module exists
----------------------
A sandbox running under the flux runtime in E2B mode exposes **envd** on port 49983.
envd speaks Connect RPC, which is *not* the REST contract the OpenSandbox SDK's
``commands`` / ``files`` APIs are built against (``/command``, ``/files/upload``,
``/files/download``). Those paths do not exist on envd.

OpenSandbox therefore provides two things for such a sandbox:

1. **Control plane** -- create / inspect / destroy, and endpoint resolution
   (delegated to the flux-sandbox controller over gRPC).
2. **Generic forwarding** -- ``/sandboxes/{id}/proxy/{port}``, a protocol-agnostic
   L7 reverse proxy.

What it does *not* provide is protocol adaptation. This module fills that gap:

* lifecycle  -> OpenSandbox SDK (``SandboxSync``), the only part needing an API key
* exec/files -> envd wire protocol, implemented here with the standard library only

envd surface used (see ``packages/envd/spec/process/process.proto`` upstream):

    POST /process.Process/Start   run a command (server-streaming Connect RPC)
    POST /files?path=...          upload (multipart; form field name must be "file")
    GET  /files?path=...          download
    GET  /health                  204 when healthy

Reaching envd
-------------
Two modes, selected by ``use_server_proxy``:

``proxy`` (default, recommended)
    ``http://<server>/sandboxes/<id>/proxy/49983`` -- the OpenSandbox server
    forwards to the sandbox. No CNI-network reachability required on the client,
    and the proxy path needs no API key.

``direct``
    ``http://<CNI-IP>:49983`` -- the client must be able to reach the sandbox
    network (typically from a cluster node).

Known limitations
-----------------
* The proxy strips the ``Authorization`` header, so Basic Auth cannot be used to
  select the executing user in ``proxy`` mode. envd's Basic Auth only picks the OS
  user anyway (the password is never checked -- see
  ``packages/envd/internal/permissions/authenticate.go``). To run as a specific
  user, use ``sudo -u <user>`` inside the command, switch to ``direct`` mode, or
  pass ``user=`` for file operations (which travels as the ``username`` query
  parameter and is unaffected by header filtering).
* If envd is configured with an access token, pass it via ``access_token``.
  ``X-Access-Token`` is not stripped by the proxy.
* The OpenSandbox server applies a 180s timeout to its upstream request. Commands
  that produce no output for longer than that may time out; long-running commands
  that keep emitting output are fine.
* The server answers some runtime rejections (bad template name, failed template
  build, template/group resource mismatch) with a bare ``500 Internal Server
  Error``, and the SDK then surfaces a misleading JSON parse error. :func:`create_sandbox`
  works around that by capturing the raw HTTP response and explaining the likely
  E2B-specific causes; the authoritative error is in the server log.
* A sandbox is not usable the moment it is created. ``status.endpoints`` stays empty
  until it is published, and envd boots after that. Use :func:`wait_for_envd`; if you
  drive the SDK yourself, expect a 404 (``KUBERNETES::POD_IP_NOT_AVAILABLE``) during
  that window.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional
from collections.abc import Callable, Iterator

#: envd's fixed listening port. In E2B mode flux-sandbox writes
#: ``<CNI-IP>:49983`` into the Sandbox CR's ``status.endpoints``, so resolving
#: this port hits the endpoint exactly.
ENVD_PORT = 49983

#: Server error code returned by endpoint resolution while the sandbox has not
#: published ``status.endpoints`` yet. It is the reliable "still starting" signal:
#: the same 404 is used for a missing sandbox, but with a different code.
ENDPOINT_NOT_READY_CODE = "KUBERNETES::POD_IP_NOT_AVAILABLE"


class FluxSandboxError(RuntimeError):
    """Base class for every error this module raises.

    Callers that just want "something went wrong" can catch this one type instead
    of tracking which layer failed.
    """


class EnvdError(FluxSandboxError):
    """envd returned an error (non-2xx HTTP, or a Connect RPC-level failure)."""


class SandboxNotReadyError(EnvdError):
    """The sandbox is not ready yet and its endpoint is still unavailable."""


class ControlPlaneError(FluxSandboxError):
    """An OpenSandbox control-plane call failed: create, destroy, or endpoint lookup.

    Wraps whatever the SDK raised (``SandboxException`` and friends) so that a
    misconfigured sandbox group, a bad template name, or an unreachable server
    surfaces as a readable message rather than a traceback.

    ``code`` carries the server's error code when the SDK exposed one, which lets
    callers tell a transient "not published yet" apart from a hard failure.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


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
    """
    while True:
        header = _read_exact(stream, 5)
        if len(header) < 5:
            return
        flags = header[0]
        length = int.from_bytes(header[1:5], "big")
        payload = _read_exact(stream, length)
        if len(payload) < length:
            return
        yield flags, payload


def _parse_trailers(payload: bytes) -> str | None:
    """Return an error description from a trailers frame, or None on success (``{}``)."""
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        meta = json.loads(text)
    except ValueError:
        return f"unparsable trailers: {text}"
    if not meta:
        return None
    return f"code={meta.get('code', '?')} message={meta.get('message', '')}"


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

    ``base_url`` is either ``http://<CNI-IP>:49983`` (direct) or
    ``http://<server>/sandboxes/<id>/proxy/49983`` (proxy).
    """

    base_url: str
    access_token: str | None = None
    user: str | None = None          # only used for /files' username query param
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

    def _files_query(self, path: str, user: str | None = None) -> str:
        params = {"path": path}
        # /files' username parameter sets the file owner and resolves relative
        # paths. It travels as a query parameter, so unlike Basic Auth it is not
        # affected by the proxy's header filtering -- the only way to pick a user
        # for file operations in proxy mode.
        effective_user = user or self.user
        if effective_user:
            params["username"] = effective_user
        return urllib.parse.urlencode(params)

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

        envd only sends the ``EndEvent`` (exit code) when the command **fails**;
        on success the stream just ends after the data frames. A stream that ended
        cleanly without an ``EndEvent`` therefore means exit code 0.
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
        try:
            for flags, payload in _iter_connect_frames(resp):
                if flags & 0x01:
                    raise EnvdError("received a compressed frame (flags=0x01)")
                if flags & 0x02:
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
            exit_code = 0  # envd omits EndEvent on success: clean end == exit 0
        return RunResult(exit_code, b"".join(stdout), b"".join(stderr))

    # -- file transfer -----------------------------------------------------

    def upload(self, local_path: str, remote_path: str, *, user: str | None = None) -> list:
        """Upload a local file to the sandbox.

        envd's upload is multipart and the form field name **must** be ``file``;
        envd silently skips parts with any other name. The destination comes from
        the ``path`` query parameter.

        The body must carry a ``Content-Length`` (passing bytes makes urllib set
        it): the OpenSandbox proxy does not support chunked multipart.
        """
        with open(local_path, "rb") as fh:
            content = fh.read()

        boundary = uuid.uuid4().hex
        filename = os.path.basename(local_path)
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body = head + content + f"\r\n--{boundary}--\r\n".encode()

        resp = self._request(
            "POST",
            "/files?" + self._files_query(remote_path, user),
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=max(self.timeout, 60.0 + len(content) / 65536.0),
        )
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except ValueError:
            return [{"raw": raw}]

    def download(self, remote_path: str, local_path: str, *, user: str | None = None) -> int:
        """Download a file from the sandbox; returns the number of bytes written.

        ``Accept-Encoding: identity`` is sent explicitly so envd returns the body
        uncompressed.
        """
        resp = self._request(
            "GET",
            "/files?" + self._files_query(remote_path, user),
            timeout=max(self.timeout, 300.0),
        )
        data = resp.read()
        with open(local_path, "wb") as fh:
            fh.write(data)
        return len(data)


# ============================================================================
# OpenSandbox control plane -- the only part that needs the opensandbox SDK
# ============================================================================

def _load_sdk():
    try:
        from opensandbox.config.connection_sync import ConnectionConfigSync
        from opensandbox.sync.sandbox import SandboxSync
    except ImportError as exc:
        raise ControlPlaneError(
            "lifecycle operations need the opensandbox SDK: uv pip install opensandbox\n"
            "To drive an existing sandbox without it, pass an explicit --endpoint instead."
        ) from exc
    return ConnectionConfigSync, SandboxSync


@contextmanager
def _control_plane(action: str):
    """Turn whatever the SDK raises into a ControlPlaneError with the action named.

    Without this the SDK's own exception escapes to the caller as a traceback, which
    is a poor first experience for the most likely failures (wrong sandbox group,
    bad template name, unreachable server, wrong API key).

    `_control_plane` is also the single place that silences the SDK's own error
    logging: on failure the adapter calls ``logger.error(..., exc_info=...)`` and
    dumps a full traceback to stderr, drowning out the concise message this
    wrapper produces.
    """
    sdk_adapter_logger = logging.getLogger("opensandbox.sync.adapters.sandboxes_adapter")
    was_disabled = sdk_adapter_logger.disabled
    sdk_adapter_logger.disabled = True
    try:
        try:
            yield
        except ControlPlaneError:
            raise
        except Exception as exc:
            # SandboxApiException carries status_code plus error.code (a SandboxError).
            code = getattr(getattr(exc, "error", None), "code", None)
            message = f"{action} failed: {exc}"
            if str(exc).startswith("Network connectivity error"):
                # The SDK converts every httpx failure (connection refused, DNS miss,
                # timeout) into this message; add where the client was actually aiming.
                protocol = os.environ.get("SANDBOX_PROTOCOL", "http")
                domain = os.environ.get("SANDBOX_DOMAIN", "127.0.0.1:8080")
                message = (
                    f"{action} failed: cannot reach the OpenSandbox server at "
                    f"{protocol}://{domain} ({exc}). Check that SANDBOX_DOMAIN / "
                    f"SANDBOX_PROTOCOL are right and the server is up (the example "
                    f"prints the sandbox address as {protocol}://<server>/)."
                )
            raise ControlPlaneError(
                message,
                code=str(code) if code else None,
                status_code=getattr(exc, "status_code", None),
            ) from exc
    finally:
        sdk_adapter_logger.disabled = was_disabled


def _capture_http_response(module, captured: dict):
    """Record the raw HTTP status/body of a failed (>=400) request for later use.

    The generated OpenAPI client assumes every error body is JSON. When the server
    answers with plain text -- e.g. an unhandled 500 that returns literally
    ``Internal Server Error`` -- ``response.json()`` throws a ``JSONDecodeError``
    that the SDK surfaces as ``InvalidArgumentException: Expecting value: ...``,
    hiding the real cause (in E2B mode: a bad template name, a template build that
    failed, or a template/group resource mismatch). This wrapper stores the response
    before the SDK's error path throws, so :func:`create_sandbox` can report
    ``HTTP <status> (<body>)`` and suggest what to check.

    Success paths are untouched; a ``None`` module (SDK layout change) is a no-op.
    """
    @contextmanager
    def _wrap():
        original = getattr(module, "_parse_response", None) if module is not None else None
        if original is None:
            yield
            return

        def _hooked(*, client, response):
            try:
                return original(client=client, response=response)
            except ValueError:
                if response.status_code >= 400:
                    captured["status_code"] = response.status_code
                    captured["body"] = response.text
                raise

        module._parse_response = _hooked
        try:
            yield
        finally:
            module._parse_response = original

    return _wrap()


def _explain_create_failure(
    exc: ControlPlaneError,
    image: str,
    group: str,
    captured: dict,
) -> ControlPlaneError:
    """Return a ControlPlaneError that names the likely cause of a rejected create.

    With the structured server error in place most rejections surface the real
    cause through ``_control_plane`` (e.g. ``template resource drift: ...``). This
    fallback only runs when the body still could not be parsed as JSON -- an
    infrastructure proxy, or an exception type the server does not convert -- so
    it points at the two things that reject an E2B-mode create and where the
    authoritative detail lives.
    """
    status = captured.get("status_code")
    body = captured.get("body")
    http = f" (HTTP {status}: {body})" if status is not None else ""
    return ControlPlaneError(
        f"create sandbox with template {image!r} in group {group!r} was rejected "
        f"by the server{http}.\n"
        "The response body was not a parseable JSON error, so check the OpenSandbox "
        "server log for the exact scheduler message. Typical E2B-mode rejections "
        "are: SANDBOX_IMAGE is not a usable template (wrong name, or the build "
        "failed), or the template's resources do not match E2B_SANDBOX_GROUP "
        "(e.g. a huge-pages template needs a group whose per-slot resources "
        "declare matching hugepages-2Mi).",
        code=exc.code,
        status_code=status or exc.status_code,
    )


def connection_config(use_server_proxy: bool):
    """Build a sync SDK connection config from the environment."""
    ConnectionConfigSync, _ = _load_sdk()
    return ConnectionConfigSync(
        domain=os.environ.get("SANDBOX_DOMAIN", "127.0.0.1:8080"),
        api_key=os.environ.get("SANDBOX_API_KEY") or None,
        protocol=os.environ.get("SANDBOX_PROTOCOL", "http"),
        use_server_proxy=use_server_proxy,
    )


def create_sandbox(
    image: str,
    group: str,
    timeout_minutes: int = 30,
    use_server_proxy: bool = True,
    cpu: str = "1",
    memory: str = "512Mi",
) -> str:
    """Create an E2B sandbox and return its id.

    Two E2B-specific details:

    * ``extensions={"sandboxGroup": ...}`` is required -- it decides which sandbox
      group the request is routed to; without it the request will not land on an
      E2B sandbox group.
    * ``skip_health_check=True`` -- the SDK's health check targets the execd
      contract port. In E2B mode it times out while envd is still starting and
      then **deletes the sandbox it just created**.

    Because of that second point this function returns as soon as the sandbox is
    accepted, not when it is usable. Follow it with :func:`wait_for_envd` before
    talking to the sandbox.

    Most server rejections (bad template, failed template build, template/group
    resource mismatch) now come back as a structured JSON error whose message is
    surfaced directly. This function still captures the raw HTTP response as a
    fallback for the rare case the body is not parseable JSON
    (see :func:`_explain_create_failure`).
    """
    from datetime import timedelta

    _, SandboxSync = _load_sdk()
    captured: dict = {}
    try:
        try:
            from opensandbox.api.lifecycle.api.sandboxes import post_sandboxes as _post
        except ImportError:
            _post = None
        with _capture_http_response(_post, captured):
            with _control_plane(f"create sandbox (image={image}, group={group})"):
                sandbox = SandboxSync.create(
                    image=image,
                    resource={"cpu": cpu, "memory": memory},
                    timeout=timedelta(minutes=timeout_minutes),
                    extensions={"sandboxGroup": group},
                    connection_config=connection_config(use_server_proxy),
                    skip_health_check=True,
                )
    except ControlPlaneError as exc:
        if captured.get("status_code") is not None:
            raise _explain_create_failure(exc, image, group, captured) from exc
        raise

    sandbox_id = sandbox.id
    try:
        sandbox.close()
    except Exception:
        pass
    return sandbox_id


def kill_sandbox(sandbox_id: str, use_server_proxy: bool = True) -> None:
    """Terminate a sandbox."""
    _, SandboxSync = _load_sdk()
    with _control_plane(f"destroy sandbox {sandbox_id}"):
        sandbox = SandboxSync.connect(
            sandbox_id,
            connection_config=connection_config(use_server_proxy),
            skip_health_check=True,
        )
        try:
            sandbox.kill()
        finally:
            try:
                sandbox.close()
            except Exception:
                pass


def resolve_envd_base_url(sandbox_id: str, use_server_proxy: bool = True) -> str:
    """Resolve envd's base URL for a sandbox.

    ``use_server_proxy=True`` yields ``<server>/sandboxes/<id>/proxy/49983`` (the
    SDK returns it without a scheme); ``False`` yields ``<CNI-IP>:49983``.

    In E2B mode flux-sandbox reports port 49983, so this hits the endpoint exactly.
    Requesting the execd contract port (44772) instead would also work -- the
    server falls back to the same envd address -- but there is no reason to.

    Raises :class:`SandboxNotReadyError` while the sandbox is still starting, since
    ``status.endpoints`` stays empty until it reports one.
    """
    _, SandboxSync = _load_sdk()
    with _control_plane(f"resolve endpoint for sandbox {sandbox_id}"):
        sandbox = SandboxSync.connect(
            sandbox_id,
            connection_config=connection_config(use_server_proxy),
            skip_health_check=True,
        )
        try:
            endpoint = sandbox.get_endpoint(ENVD_PORT)
        finally:
            try:
                sandbox.close()
            except Exception:
                pass

    if not endpoint or not endpoint.endpoint:
        raise SandboxNotReadyError(
            f"sandbox {sandbox_id} has no endpoint yet; it may still be starting"
        )
    return endpoint.endpoint


def wait_for_envd(
    sandbox_id: str,
    use_server_proxy: bool = True,
    timeout_seconds: float = 60.0,
    poll_seconds: float = 1.0,
    access_token: str | None = None,
) -> EnvdClient:
    """Return a client for the sandbox once envd answers, waiting for it to boot.

    A freshly created sandbox is not usable yet: endpoint resolution fails until it
    publishes ``status.endpoints``, and envd may still be booting after that.
    Because :func:`create_sandbox` deliberately skips the SDK health check, nothing
    else waits -- so a create immediately followed by exec or upload fails
    intermittently unless you call this.

    Only the transient conditions are retried: the server's
    ``POD_IP_NOT_AVAILABLE`` code and an unreachable envd. Anything else (sandbox
    missing, auth failure, bad request) propagates immediately rather than
    stalling for the whole timeout.

    ``timeout_seconds=0`` probes once and raises instead of waiting.

    Raises :class:`SandboxNotReadyError` if envd does not answer in time.
    """
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    last_error: Exception | None = None

    while True:
        try:
            client = EnvdClient(
                resolve_envd_base_url(sandbox_id, use_server_proxy=use_server_proxy),
                access_token=access_token,
            )
            # Short per-probe timeout so one hung connection cannot eat the budget.
            if client.health(timeout=min(5.0, max(timeout_seconds, 1.0))):
                return client
            last_error = EnvdError(f"envd at {client.base_url} answered but is not healthy")
        except EnvdError as exc:
            # SandboxNotReadyError, or envd not accepting connections yet.
            last_error = exc
        except ControlPlaneError as exc:
            if exc.code != ENDPOINT_NOT_READY_CODE:
                raise
            last_error = exc

        if time.monotonic() >= deadline:
            raise SandboxNotReadyError(
                f"envd for sandbox {sandbox_id} was not ready within "
                f"{timeout_seconds:.0f}s; last error: {last_error}"
            )
        time.sleep(poll_seconds)
