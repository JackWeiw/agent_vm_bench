#!/usr/bin/env python3
"""AgentENV snapshot / lazy-load latency benchmark.

Standalone harness (depends only on the `e2b` SDK) that drives an AgentENV server
(E2B-compatible API) to collect the four metrics from the customer benchmark table:

  1. VM cold start          - Sandbox.create(template) + ready probe
  2. VM snapshot start      - beta_pause() -> connect() resume + ready probe
  3. 10-concurrent snapshot - k concurrent connect()+ready of paused sandboxes
  4. snapshot lazy-load     - per mode (seq/rand x read/write): populate 256MiB
                              -> pause -> resume -> latbench measure first/second

The lazy-load payload (`latbench`) is baked into the image. It MUST run inside the
sandbox: first-touch page-in only fires when the guest touches its own snapshotted
memory. See dockerfile_build/aenv_latency/README.md for the methodology.

Variance handling: the first sample of cold/snapshot is a warmup outlier (template
cache / host scheduler cold) and lazy-load had only one observation per mode, so
headlines swung widely. This harness discards `--warmup` leading samples, repeats
each lazy mode `--lazy-repeats` times, and reports the **median** (not mean) plus a
p99 noise ceiling so remaining jitter is visible. Raw samples are kept in report.json.

Usage (single arch):
  E2B_API_URL=http://127.0.0.1:8000 E2B_API_KEY=e2b_000... \
    python scripts/aenv_latency_bench.py --template arm=aenv-latency-arm

Compare x86 vs arm:
  python scripts/aenv_latency_bench.py \
      --template arm=aenv-latency-arm --template x86=aenv-latency-x86

Also measure a REAL cold boot (OCI image -> kernel boot, not a snapshot load) and
report it as 'VM冷启动(真boot)' alongside the template-snapshot 'VM冷启动' row. The
two are different things: the template create is a snapshot load (~100ms), the cold
boot is a real boot (seconds) and is the metric comparable to the customer's cold start.
Pass the same OCI ref your `aenv pull` used, per arch:
  python scripts/aenv_latency_bench.py \
      --template arm=aenv-latency-arm --template x86=aenv-latency-x86 \
      --cold-image arm=127.0.0.1:6000/ubuntu-aenv-latency-bench:24.04-linuxarm64 \
      --cold-image x86=127.0.0.1:6000/ubuntu-aenv-latency-bench:24.04-x86_64
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_API_KEY = "e2b_" + "0" * 40
MODES = ("seq_read", "seq_write", "rand_read", "rand_write")


def configure_env(api_url: str, api_key: str) -> None:
    os.environ["E2B_API_URL"] = api_url
    os.environ["E2B_SANDBOX_URL"] = api_url
    os.environ.setdefault("E2B_API_KEY", api_key)
    os.environ.setdefault("E2B_ACCESS_TOKEN", "dummy")


def result_fields(raw: Any) -> tuple[str, str, int | None]:
    stdout = str(getattr(raw, "stdout", "") or "")
    stderr = str(getattr(raw, "stderr", "") or "")
    code: int | None = None
    for name in ("exit_code", "return_code", "exit_status", "code"):
        value = getattr(raw, name, None)
        if isinstance(value, int):
            code = value
            break
    return stdout, stderr, code


async def run_cmd(sandbox: Any, cmd: str, timeout: float = 120) -> Any:
    """Run a short foreground command. Mirrors replay_agent's background+wait."""
    handle = await sandbox.commands.run(cmd=cmd, background=True, timeout=int(timeout), user="root")
    return await handle.wait()


async def ready_probe(sandbox: Any, attempts: int = 10, per_timeout: float = 10.0) -> float:
    """Run `true` until the command plane answers; return elapsed seconds."""
    started = time.perf_counter()
    last = "no attempts"
    for _ in range(attempts):
        try:
            raw = await run_cmd(sandbox, "true", per_timeout)
            _, _, code = result_fields(raw)
            if code in (None, 0):
                return time.perf_counter() - started
            last = f"exit={code}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.5)
    raise RuntimeError(f"sandbox command plane not ready: {last}")


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    f = int(k)
    if f + 1 < len(ordered):
        return ordered[f] + (ordered[f + 1] - ordered[f]) * (k - f)
    return ordered[-1]


def _drop_nan(vals: list[float]) -> list[float]:
    return [v for v in vals if v == v]  # filter NaN


def median_or_none(vals: list[float]) -> float | None:
    clean = _drop_nan(vals)
    return statistics.median(clean) if clean else None


@dataclass
class ArchResult:
    label: str
    # cold/snapshot hold ALL samples (including the warmup prefix); the warmup count
    # is kept separately so summary getters can slice [warmup:] and raw data survives.
    cold: list[dict] = field(default_factory=list)  # {create_ms, ready_ms}
    snapshot: list[dict] = field(default_factory=list)  # {resume_ms, ready_ms}
    concurrent: dict | None = None
    # lazy/control: mode -> {first_ms (median), second_ms (median), first_samples, second_samples}
    lazy: dict = field(default_factory=dict)
    control: dict = field(default_factory=dict)
    warmup: int = 0  # number of leading cold/snapshot samples discarded as warmup
    # Real cold boot from a raw OCI image (POST /sandboxes-cold -> for_create_fresh ->
    # start_fresh, a kernel boot, NOT a snapshot load from a template). launch_sandbox
    # waits for envd-ready before returning 201 (service.rs wait_for_ready), so create_ms
    # already includes boot+envd and ready_ms is 0 -> cold_total_ms == create_ms. This is
    # the metric comparable to the customer's "VM cold start"; the template create above
    # is a snapshot load, not a boot. Optional (--cold-image).
    cold_boot: list[dict] = field(default_factory=list)  # {create_ms, ready_ms}
    cold_boot_warmup: int = 0  # separate warmup; first cold pulls/converts OCI layers
    errors: list[str] = field(default_factory=list)


async def measure_cold(template: str, n: int, timeout: float) -> list[dict]:
    from e2b import AsyncSandbox

    out: list[dict] = []
    for _ in range(n):
        t0 = time.perf_counter()
        sb = await AsyncSandbox.create(template=template, timeout=timeout, request_timeout=timeout)
        create_ms = (time.perf_counter() - t0) * 1000.0
        try:
            ready_ms = await ready_probe(sb) * 1000.0
        except Exception:
            ready_ms = float("nan")
        out.append({"create_ms": create_ms, "ready_ms": ready_ms})
        try:
            await sb.kill()
        except Exception:
            pass
    return out


def _cold_http(method: str, url: str, api_key: str, body: dict | None, timeout: float) -> tuple[int, str]:
    """Blocking HTTP helper for the cold-boot path (run via asyncio.to_thread)."""
    import urllib.error
    import urllib.request

    data = None
    headers = {"X-API-KEY": api_key}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


async def measure_cold_boot(
    image: str,
    n: int,
    timeout: float,
    api_url: str,
    api_key: str,
    cpu: int = 2,
    mem_mb: int = 4096,
) -> list[dict]:
    """Real cold boot: POST /sandboxes-cold (image source -> for_create_fresh -> kernel boot).

    launch_sandbox waits for envd-ready before returning 201, so create_ms is the full
    cold-boot time (overlaybd convert + boot + envd) and ready_ms is 0. This is the
    customer-comparable "VM cold start"; the e2b SDK has no cold-boot entry, so the create
    call is raw HTTP (X-API-KEY header, same header the SDK sends). Cleanup is a raw DELETE.
    """
    endpoint = f"{api_url.rstrip('/')}/sandboxes-cold"
    out: list[dict] = []
    for i in range(n):
        body = {
            "image": image,
            "timeout": int(timeout),
            "autoPause": False,
            "cpuCount": cpu,
            "memoryMB": mem_mb,
        }
        # First cold may pull+convert OCI layers (tens of s); give the HTTP call more
        # headroom than the sandbox TTL so a slow first boot is measured, not aborted.
        http_timeout = timeout + 60.0
        t0 = time.perf_counter()
        try:
            status, text = await asyncio.to_thread(_cold_http, "POST", endpoint, api_key, body, http_timeout)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            out.append({"create_ms": float("nan"), "ready_ms": 0.0, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  cold-boot #{i} create FAILED after {elapsed_ms:.0f}ms: {exc}", flush=True)
            continue
        create_ms = (time.perf_counter() - t0) * 1000.0
        if status != 201:
            out.append({"create_ms": float("nan"), "ready_ms": 0.0, "error": f"HTTP {status}: {text[:200]}"})
            print(
                f"  cold-boot #{i} create HTTP {status} after {create_ms:.0f}ms: {text[:200]}",
                flush=True,
            )
            continue
        try:
            sandbox_id = json.loads(text)["sandboxID"]
        except Exception:  # noqa: BLE001
            out.append({"create_ms": create_ms, "ready_ms": 0.0, "error": f"no sandboxID: {text[:200]}"})
            continue
        out.append({"create_ms": create_ms, "ready_ms": 0.0})
        try:
            await asyncio.to_thread(
                _cold_http, "DELETE", f"{api_url.rstrip('/')}/sandboxes/{sandbox_id}", api_key, None, 60.0
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  cold-boot kill {sandbox_id} FAILED: {exc}", flush=True)
    return out


async def measure_snapshot(template: str, n: int, timeout: float) -> tuple[list[dict], Any | None]:
    """Pause one sandbox, then n times resume+ready+re-pause. Returns (samples, last_sb)."""
    from e2b import AsyncSandbox

    sb = await AsyncSandbox.create(template=template, timeout=timeout, request_timeout=timeout)
    await ready_probe(sb)
    await sb.beta_pause(request_timeout=timeout)
    out: list[dict] = []
    for _ in range(n):
        t0 = time.perf_counter()
        sb = await sb.connect(timeout=timeout, request_timeout=timeout)
        resume_ms = (time.perf_counter() - t0) * 1000.0
        ready_ms = await ready_probe(sb) * 1000.0
        out.append({"resume_ms": resume_ms, "ready_ms": ready_ms})
        await sb.beta_pause(request_timeout=timeout)
    try:
        await sb.kill()
    except Exception:
        pass
    return out, None


async def _resume_one(sb: Any, timeout: float) -> tuple[float, float, Any]:
    t0 = time.perf_counter()
    nsb = await sb.connect(timeout=timeout, request_timeout=timeout)
    resume_ms = (time.perf_counter() - t0) * 1000.0
    ready_ms = await ready_probe(nsb) * 1000.0
    return resume_ms, ready_ms, nsb


async def measure_concurrent(template: str, k: int, timeout: float) -> dict:
    from e2b import AsyncSandbox

    paused: list[Any] = []
    for _ in range(k):
        sb = await AsyncSandbox.create(template=template, timeout=timeout, request_timeout=timeout)
        await ready_probe(sb)
        await sb.beta_pause(request_timeout=timeout)
        paused.append(sb)

    t0 = time.perf_counter()
    raw = await asyncio.gather(*[_resume_one(sb, timeout) for sb in paused], return_exceptions=True)
    wall_ms = (time.perf_counter() - t0) * 1000.0

    per: list[dict] = []
    for _, r in enumerate(raw):
        if isinstance(r, Exception):
            per.append({"error": f"{type(r).__name__}: {r}"})
        else:
            resume_ms, ready_ms, nsb = r
            per.append({"resume_ms": resume_ms, "ready_ms": ready_ms, "total_ms": resume_ms + ready_ms})
            try:
                await nsb.kill()
            except Exception:
                pass
    resume_vals = [p["resume_ms"] for p in per if "resume_ms" in p]
    total_vals = [p["total_ms"] for p in per if "total_ms" in p]
    return {
        "k": k,
        "wall_ms": wall_ms,
        "per_instance": per,
        "resume_median_ms": statistics.median(resume_vals) if resume_vals else 0.0,
        "total_max_ms": max(total_vals) if total_vals else 0.0,
    }


def parse_measure(stdout: str, mode: str) -> tuple[float, float] | None:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(mode):
            parts = line.split()
            # mode first_ms second_ms pages mib
            if len(parts) >= 3:
                try:
                    return float(parts[1]), float(parts[2])
                except ValueError:
                    return None
    return None


def parse_stress(stdout: str) -> dict | None:
    """Parse a `STRESS <mode> iters=N total_ms=F per_iter_ms=F pages=N mib=N` line."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("STRESS"):
            out: dict = {}
            for tok in line.split()[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    out[k] = v
            if "mode" in out and "total_ms" in out:
                return out
    return None


async def measure_lazy(
    template: str,
    ws_mib: int,
    timeout: float,
    backing: str = "shm",
    repeats: int = 3,
    keep: bool = False,
) -> tuple[dict, list[dict], Any | None]:
    """Per mode: repeat `repeats` x (populate -> pause -> resume -> measure -> cleanup).

    Each repeat is a fresh lazy cycle (populate re-dirties the shm, pause re-snapshots,
    resume lazily restores), so the first-touch cost is genuinely re-measured every
    repeat. Report median first/second across repeats + the raw per-repeat samples.
    The 4*repeats resume timings also feed the snapshot-start row as a fallback.
    """
    from e2b import AsyncSandbox

    sb = await AsyncSandbox.create(template=template, timeout=timeout, request_timeout=timeout)
    await ready_probe(sb)
    lazy: dict = {}
    snap_samples: list[dict] = []
    latbench = "/usr/local/bin/latbench"
    for mode in MODES:
        firsts: list[float] = []
        seconds: list[float] = []
        last_err: str | None = None
        for _ in range(repeats):
            # populate while running -> pause -> resume (lazy state) -> measure
            raw = await run_cmd(sb, f"{latbench} populate {ws_mib} {backing}", timeout=180)
            _, stderr, code = result_fields(raw)
            if code not in (None, 0):
                last_err = f"populate exit={code} stderr={stderr!r}"
                break
            await sb.beta_pause(request_timeout=timeout)
            t0 = time.perf_counter()
            sb = await sb.connect(timeout=timeout, request_timeout=timeout)
            resume_ms = (time.perf_counter() - t0) * 1000.0
            ready_ms = await ready_probe(sb) * 1000.0
            snap_samples.append({"resume_ms": resume_ms, "ready_ms": ready_ms})

            raw = await run_cmd(sb, f"{latbench} measure {ws_mib} {mode} {backing}", timeout=180)
            stdout, stderr, code = result_fields(raw)
            await run_cmd(sb, f"{latbench} cleanup {backing}", timeout=30)
            if code not in (None, 0):
                last_err = f"measure exit={code} stderr={stderr!r}"
                continue
            parsed = parse_measure(stdout, mode)
            if parsed is None:
                last_err = f"unparseable stdout={stdout!r}"
                continue
            firsts.append(parsed[0])
            seconds.append(parsed[1])

        if firsts:
            lazy[mode] = {
                "first_ms": statistics.median(firsts),
                "second_ms": statistics.median(seconds),
                "first_samples": firsts,
                "second_samples": seconds,
            }
        else:
            lazy[mode] = {"error": last_err or "no successful repeats"}

    last_sb = sb
    if not keep:
        try:
            await sb.kill()
        except Exception:
            pass
        last_sb = None
    return lazy, snap_samples, last_sb


async def measure_control(template: str, ws_mib: int, timeout: float, backing: str = "shm", repeats: int = 3) -> dict:
    """Control: populate + measure with NO pause/resume, repeated.

    The working set is populated in a running sandbox and measured immediately,
    so pages are already resident: first touch should ~= second touch (both cheap
    minor faults). If that holds, the lazy-load delta in measure_lazy is proven
    to be caused by snapshot pause/resume, not by any inherent first-mmap penalty.
    """
    from e2b import AsyncSandbox

    sb = await AsyncSandbox.create(template=template, timeout=timeout, request_timeout=timeout)
    await ready_probe(sb)
    latbench = "/usr/local/bin/latbench"
    out: dict = {}
    for mode in MODES:
        firsts: list[float] = []
        seconds: list[float] = []
        for _ in range(repeats):
            raw = await run_cmd(sb, f"{latbench} populate {ws_mib} {backing}", timeout=180)
            _, _, code = result_fields(raw)
            if code not in (None, 0):
                continue
            raw = await run_cmd(sb, f"{latbench} measure {ws_mib} {mode} {backing}", timeout=180)
            stdout, _, code = result_fields(raw)
            await run_cmd(sb, f"{latbench} cleanup {backing}", timeout=30)
            if code not in (None, 0):
                continue
            parsed = parse_measure(stdout, mode)
            if parsed is None:
                continue
            firsts.append(parsed[0])
            seconds.append(parsed[1])
        if firsts:
            out[mode] = {
                "first_ms": statistics.median(firsts),
                "second_ms": statistics.median(seconds),
                "first_samples": firsts,
                "second_samples": seconds,
            }
        else:
            out[mode] = {"error": "no successful repeats"}
    try:
        await sb.kill()
    except Exception:
        pass
    return out


async def run_stress(label: str, template: str, args: argparse.Namespace) -> dict:
    """One-shot stress profile: create a sandbox, run `latbench stress` in the
    background so an external profiler (devkit_mem / ksys / perf) can be attached
    to the host VMM PID during the multi-second resident-traversal loop, collect
    the STRESS line, kill. This is the only way to drive latbench from outside the
    sandbox — there is no direct shell access, only the e2b command plane.

    Run e.g.:
      python scripts/aenv_latency_bench.py --template arm=lat --stress seq_read --stress-iters 2000
    """
    from e2b import AsyncSandbox

    timeout = int(args.sandbox_timeout)
    latbench = "/usr/local/bin/latbench"
    mib = args.working_set_mib
    mode = args.stress
    iters = args.stress_iters
    backing = args.backing

    print(
        f"\n=== [{label}] stress profile: {mode} {mib}MiB x{iters} iters backing={backing} ===",
        flush=True,
    )
    result: dict = {"label": label, "mode": mode, "iters": iters, "mib": mib, "backing": backing}
    sb = await AsyncSandbox.create(template=template, timeout=timeout, request_timeout=timeout)
    try:
        await ready_probe(sb)
        sb_id = getattr(sb, "sandbox_id", None) or getattr(sb, "id", None) or "<unknown>"
        print(
            f"[{label}] sandbox_id={sb_id}\n"
            f"[{label}] >>> attach devkit_mem / ksys / perf to the host VMM PID NOW <<<\n"
            f"[{label}]     host:  pgrep -af 'firecracker|aenv|vmm'  (find the VMM for this sandbox)\n"
            f"[{label}]            verify guest-RAM page size: "
            f"grep -E 'KernelPageSize|MMUPageSize' /proc/$FC_PID/smaps\n"
            f"[{label}]     see dockerfile_build/aenv_latency/README.md 'Profiling the resident path'",
            flush=True,
        )
        cmd = f"{latbench} stress {mib} {mode} {iters} {backing}"
        # seq full-page is bandwidth-bound (~tens of ms/iter); give the loop headroom.
        cmd_timeout = max(args.stress_timeout, iters // 20 + 60)
        raw = await run_cmd(sb, cmd, timeout=cmd_timeout)
        stdout, stderr, code = result_fields(raw)
        if code not in (None, 0):
            result["error"] = f"exit={code} stderr={stderr!r}"
            print(f"[{label}] stress FAILED: {result['error']}", flush=True)
            return result
        parsed = parse_stress(stdout)
        if parsed is None:
            result["error"] = f"unparseable stdout={stdout!r}"
            print(f"[{label}] stress FAILED: {result['error']}", flush=True)
            return result
        try:
            result["total_ms"] = float(parsed["total_ms"])
            result["per_iter_ms"] = float(parsed["per_iter_ms"])
        except (KeyError, ValueError):
            result["error"] = f"bad STRESS line={parsed!r}"
            return result
        print(
            f"[{label}] STRESS {result['mode']} iters={result['iters']} "
            f"total_ms={result.get('total_ms', float('nan')):.3f} "
            f"per_iter_ms={result.get('per_iter_ms', float('nan')):.6f}",
            flush=True,
        )
        return result
    finally:
        try:
            await sb.kill()
        except Exception:
            pass


async def run_suite(label: str, template: str, args: argparse.Namespace, cold_image: str | None = None) -> ArchResult:
    # AgentENV deserializes `timeout` as u32; the SDK forwards it verbatim, so cast
    # to int here once and every downstream create/connect/pause call is covered.
    timeout = int(args.sandbox_timeout)
    res = ArchResult(label=label, warmup=args.warmup)

    print(f"\n=== [{label}] template={template} ===", flush=True)

    # Sample counts are the KEPT counts; warmup samples are taken on top and discarded.
    cold_n = args.cold_samples + args.warmup
    snap_n = args.snapshot_samples + args.warmup
    print(
        f"[{label}] cold start ({args.cold_samples} kept + {args.warmup} warmup = {cold_n} runs)...",
        flush=True,
    )
    try:
        res.cold = await measure_cold(template, cold_n, timeout)
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"cold: {type(exc).__name__}: {exc}")
        print(f"[{label}] cold FAILED: {exc}", flush=True)

    if cold_image:
        cb_n = args.cold_boot_samples + args.cold_boot_warmup
        print(
            f"[{label}] real cold boot from OCI image ({args.cold_boot_samples} kept + "
            f"{args.cold_boot_warmup} warmup = {cb_n} runs, image={cold_image})...",
            flush=True,
        )
        res.cold_boot_warmup = args.cold_boot_warmup
        try:
            res.cold_boot = await measure_cold_boot(
                cold_image,
                cb_n,
                timeout,
                args.api_url,
                args.api_key,
                cpu=args.cold_cpu,
                mem_mb=args.cold_memory,
            )
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"cold_boot: {type(exc).__name__}: {exc}")
            print(f"[{label}] cold_boot FAILED: {exc}", flush=True)

    print(
        f"[{label}] snapshot start ({args.snapshot_samples} kept + {args.warmup} warmup = {snap_n} runs)...",
        flush=True,
    )
    try:
        samples, _ = await measure_snapshot(template, snap_n, timeout)
        res.snapshot = samples
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"snapshot: {type(exc).__name__}: {exc}")
        print(f"[{label}] snapshot FAILED: {exc}", flush=True)

    print(f"[{label}] concurrent snapshot start (k={args.concurrent})...", flush=True)
    try:
        res.concurrent = await measure_concurrent(template, args.concurrent, timeout)
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"concurrent: {type(exc).__name__}: {exc}")
        print(f"[{label}] concurrent FAILED: {exc}", flush=True)

    print(
        f"[{label}] lazy-load latency ({args.working_set_mib}MiB, backing={args.backing}, "
        f"{args.lazy_repeats} repeats/mode)...",
        flush=True,
    )
    try:
        lazy, snap, _ = await measure_lazy(
            template, args.working_set_mib, timeout, args.backing, repeats=args.lazy_repeats, keep=False
        )
        res.lazy = lazy
        if snap and not res.snapshot:
            res.snapshot = snap
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"lazy: {type(exc).__name__}: {exc}")
        print(f"[{label}] lazy FAILED: {exc}", flush=True)

    if args.control:
        print(
            f"[{label}] control (no pause/resume, {args.working_set_mib}MiB, " f"{args.lazy_repeats} repeats/mode)...",
            flush=True,
        )
        try:
            res.control = await measure_control(
                template, args.working_set_mib, timeout, args.backing, repeats=args.lazy_repeats
            )
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"control: {type(exc).__name__}: {exc}")
            print(f"[{label}] control FAILED: {exc}", flush=True)

    return res


def fmt_ms(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v != v:  # NaN
        return "n/a"
    return f"{v:.3f}"


def cold_total_ms(c: dict) -> float:
    r = c.get("ready_ms", float("nan"))
    return c["create_ms"] + (r if r == r else 0.0)


def snap_total_ms(s: dict) -> float:
    r = s.get("ready_ms", float("nan"))
    return s["resume_ms"] + (r if r == r else 0.0)


def _cold_kept_totals(r: ArchResult) -> list[float]:
    return [cold_total_ms(c) for c in r.cold[r.warmup :]]


def _cold_boot_kept_totals(r: ArchResult) -> list[float]:
    return [cold_total_ms(c) for c in r.cold_boot[r.cold_boot_warmup :]]


def _cold_boot_median(r: ArchResult) -> float | None:
    return median_or_none(_cold_boot_kept_totals(r))


def _cold_boot_p99(r: ArchResult) -> float | None:
    clean = _drop_nan(_cold_boot_kept_totals(r))
    return pct(clean, 0.99) if clean else None


def _snap_kept_totals(r: ArchResult) -> list[float]:
    return [snap_total_ms(s) for s in r.snapshot[r.warmup :]]


def _spread(vals: list[float]) -> str:
    clean = _drop_nan(vals)
    if not clean:
        return "n/a"
    return f"{min(clean):.3f}..{max(clean):.3f}"


def print_report(results: list[ArchResult]) -> None:
    print("\n" + "=" * 72)
    print("AgentENV latency benchmark — summary")
    print("=" * 72)

    for r in results:
        print(f"\n## {r.label}")
        if r.cold:
            totals = _cold_kept_totals(r)
            print(
                f"  VM cold start       median={fmt_ms(median_or_none(totals))}ms  "
                f"p99={fmt_ms(pct(_drop_nan(totals), 0.99))}ms  "
                f"spread={_spread(totals)}ms  (n={len(totals)}, +{r.warmup} warmup)"
            )
        else:
            print("  VM cold start       (no data)")
        if r.snapshot:
            totals = _snap_kept_totals(r)
            print(
                f"  VM snapshot start   median={fmt_ms(median_or_none(totals))}ms  "
                f"p99={fmt_ms(pct(_drop_nan(totals), 0.99))}ms  "
                f"spread={_spread(totals)}ms  (n={len(totals)}, +{r.warmup} warmup)"
            )
        else:
            print("  VM snapshot start   (no data)")
        if r.cold_boot:
            totals = _cold_boot_kept_totals(r)
            print(
                f"  VM cold start (boot) median={fmt_ms(median_or_none(totals))}ms  "
                f"p99={fmt_ms(pct(_drop_nan(totals), 0.99))}ms  "
                f"spread={_spread(totals)}ms  (n={len(totals)}, +{r.cold_boot_warmup} warmup)  "
                f"[POST /sandboxes-cold, kernel boot]"
            )
        else:
            print("  VM cold start (boot) (not measured; pass --cold-image LABEL=REF)")
        if r.concurrent:
            c = r.concurrent
            print(
                f"  {c['k']}-concurrent snap  wall={fmt_ms(c['wall_ms'])}ms  "
                f"resume_median={fmt_ms(c['resume_median_ms'])}ms  "
                f"total_max={fmt_ms(c['total_max_ms'])}ms"
            )
        else:
            print("  concurrent snapshot (no data)")
        print("  lazy-load latency (median first / second, ms; spread across repeats):")
        for mode in MODES:
            e = r.lazy.get(mode)
            if not e:
                print(f"    {mode:12s} (no data)")
            elif "error" in e:
                print(f"    {mode:12s} ERROR: {e['error']}")
            else:
                print(
                    f"    {mode:12s} first={fmt_ms(e['first_ms'])}ms "
                    f"[{_spread(e.get('first_samples', []))}]  "
                    f"second={fmt_ms(e['second_ms'])}ms"
                )
        if r.control:
            print("  control (no pause/resume, median first / second, ms):")
            for mode in MODES:
                e = r.control.get(mode)
                if not e:
                    print(f"    {mode:12s} (no data)")
                elif "error" in e:
                    print(f"    {mode:12s} ERROR: {e['error']}")
                else:
                    print(
                        f"    {mode:12s} first={fmt_ms(e['first_ms'])}ms "
                        f"[{_spread(e.get('first_samples', []))}]  "
                        f"second={fmt_ms(e['second_ms'])}ms"
                    )

    if len(results) >= 2:
        print("\n## x86 vs arm delta (snapshot start median)")
        for r in results:
            if r.snapshot:
                totals = _snap_kept_totals(r)
                print(f"  {r.label}: {fmt_ms(median_or_none(totals))}ms")

    errs = [f"[{r.label}] {e}" for r in results for e in r.errors]
    if errs:
        print("\n## errors")
        for e in errs:
            print(f"  {e}")


def to_jsonable(r: ArchResult) -> dict:
    return {
        "label": r.label,
        "warmup": r.warmup,
        "cold": r.cold,
        "snapshot": r.snapshot,
        "concurrent": r.concurrent,
        "lazy": r.lazy,
        "control": r.control,
        "cold_boot": r.cold_boot,
        "cold_boot_warmup": r.cold_boot_warmup,
        "errors": r.errors,
    }


def parse_templates(specs: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for spec in specs:
        if "=" in spec:
            label, alias = spec.split("=", 1)
        else:
            label, alias = spec, spec
        out.append((label.strip(), alias.strip()))
    return out


def _cell(v: float | None) -> str:
    """Format a metric value for table cells; blank for missing/NaN."""
    if v is None or v != v:  # None or NaN
        return ""
    return f"{v:.3f}"


def _cold_median(r: ArchResult) -> float | None:
    return median_or_none(_cold_kept_totals(r))


def _cold_p99(r: ArchResult) -> float | None:
    clean = _drop_nan(_cold_kept_totals(r))
    return pct(clean, 0.99) if clean else None


def _snap_median(r: ArchResult) -> float | None:
    return median_or_none(_snap_kept_totals(r))


def _snap_p99(r: ArchResult) -> float | None:
    clean = _drop_nan(_snap_kept_totals(r))
    return pct(clean, 0.99) if clean else None


def _conc_wall(r: ArchResult) -> float | None:
    return r.concurrent["wall_ms"] if r.concurrent else None


def _conc_total_max(r: ArchResult) -> float | None:
    return r.concurrent["total_max_ms"] if r.concurrent else None


def _lazy_value(mode: str, key: str):
    def getter(r: ArchResult) -> float | None:
        e = r.lazy.get(mode)
        if not e or "error" in e:
            return None
        return e[key]  # first_ms / second_ms are medians across repeats

    return getter


def _control_value(mode: str, key: str):
    def getter(r: ArchResult) -> float | None:
        e = r.control.get(mode)
        if not e or "error" in e:
            return None
        return e[key]

    return getter


# Fixed row order mirroring the customer benchmark table so runs from
# different machines paste-align row-by-row. Headline = median (robust to the
# warmup outlier + long tail); the p99 row right after each startup metric is a
# noise ceiling so jitter is visible in the paste-ready table. Each row =
# (维度, 指标, getter).
TABLE_ROWS: list[tuple[str, str, Any]] = [
    ("VM启动", "VM冷启动", _cold_median),
    ("VM启动", "VM冷启动-p99", _cold_p99),
    ("VM启动", "VM冷启动(真boot)", _cold_boot_median),
    ("VM启动", "VM冷启动(真boot)-p99", _cold_boot_p99),
    ("VM启动", "VM快照启动", _snap_median),
    ("VM启动", "VM快照启动-p99", _snap_p99),
    ("VM启动", "10并发快照启动-总耗时", _conc_wall),
    ("VM启动", "10并发快照启动-单实例最大", _conc_total_max),
    ("快照lazy load", "顺序遍历读-首读", _lazy_value("seq_read", "first_ms")),
    ("快照lazy load", "顺序遍历读-次读", _lazy_value("seq_read", "second_ms")),
    ("快照lazy load", "顺序遍历写-首写", _lazy_value("seq_write", "first_ms")),
    ("快照lazy load", "顺序遍历写-次写", _lazy_value("seq_write", "second_ms")),
    ("快照lazy load", "随机遍历读-首读", _lazy_value("rand_read", "first_ms")),
    ("快照lazy load", "随机遍历读-次读", _lazy_value("rand_read", "second_ms")),
    ("快照lazy load", "随机遍历写-首写", _lazy_value("rand_write", "first_ms")),
    ("快照lazy load", "随机遍历写-次写", _lazy_value("rand_write", "second_ms")),
]

# Control rows (populate+measure with NO pause/resume). First touch should
# ~second touch, proving the lazy-load delta above is specifically caused by
# snapshot pause/resume, not by any inherent first-mmap penalty. Emitted only
# when --control was run for at least one template.
CONTROL_ROWS: list[tuple[str, str, Any]] = [
    ("对照(无快照)", "顺序遍历读-首读", _control_value("seq_read", "first_ms")),
    ("对照(无快照)", "顺序遍历读-次读", _control_value("seq_read", "second_ms")),
    ("对照(无快照)", "顺序遍历写-首写", _control_value("seq_write", "first_ms")),
    ("对照(无快照)", "顺序遍历写-次写", _control_value("seq_write", "second_ms")),
    ("对照(无快照)", "随机遍历读-首读", _control_value("rand_read", "first_ms")),
    ("对照(无快照)", "随机遍历读-次读", _control_value("rand_read", "second_ms")),
    ("对照(无快照)", "随机遍历写-首写", _control_value("rand_write", "first_ms")),
    ("对照(无快照)", "随机遍历写-次写", _control_value("rand_write", "second_ms")),
]


def _all_rows(results: list[ArchResult]) -> list[tuple[str, str, Any]]:
    rows = list(TABLE_ROWS)
    if any(r.control for r in results):
        rows += CONTROL_ROWS
    return rows


def build_tsv(results: list[ArchResult]) -> str:
    labels = [r.label for r in results]
    lines = ["\t".join(["维度", "指标", *labels])]
    for dim, metric, getter in _all_rows(results):
        cells = [dim, metric] + [_cell(getter(r)) for r in results]
        lines.append("\t".join(cells))
    return "\n".join(lines) + "\n"


def build_markdown(results: list[ArchResult]) -> str:
    labels = [r.label for r in results]
    header = "| 维度 | 指标 | " + " | ".join(labels) + " |"
    sep = "|---|---|" + "|".join(["---"] * len(labels)) + "|"
    lines = [header, sep]
    for dim, metric, getter in _all_rows(results):
        cells = [dim, metric] + [_cell(getter(r)) for r in results]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    configure_env(args.api_url, args.api_key)
    templates = parse_templates(args.template)

    if args.stress:
        # Stress profile path: one `latbench stress` loop per template, skipping the
        # full cold/snapshot/lazy suite. Output is a small report.json only (no TSV —
        # stress is a single per_iter number, not a paste-ready comparison row).
        stress_results: list[dict] = []
        for label, alias in templates:
            res = await run_stress(label, alias, args)
            stress_results.append(res)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path(args.output_dir) / f"{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(
            json.dumps({"generated_at": stamp, "stress": stress_results}, indent=2),
            encoding="utf-8",
        )
        print(f"\nStress report written to {out_dir}/ (report.json)", flush=True)
        return 0

    results: list[ArchResult] = []
    cold_images = dict(parse_templates(args.cold_image or []))
    for label, alias in templates:
        res = await run_suite(label, alias, args, cold_image=cold_images.get(label))
        results.append(res)

    print_report(results)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.output_dir) / f"{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(
            {"generated_at": stamp, "results": [to_jsonable(r) for r in results]},
            indent=2,
        ),
        encoding="utf-8",
    )
    # Paste-ready tables: TSV for Excel/Sheets, Markdown for docs/issues.
    (out_dir / "report.tsv").write_text(build_tsv(results), encoding="utf-8")
    (out_dir / "report.md").write_text(build_markdown(results) + "\n", encoding="utf-8")

    print("\n" + build_markdown(results))
    print(f"\nReport written to {out_dir}/ (report.json, report.tsv, report.md)", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AgentENV snapshot/lazy-load latency benchmark")
    p.add_argument(
        "--template",
        action="append",
        required=True,
        metavar="LABEL=ALIAS",
        help="template to benchmark (repeat for x86 vs arm, e.g. arm=aenv-latency-arm)",
    )
    p.add_argument("--api-url", default=os.environ.get("E2B_API_URL", DEFAULT_API_URL))
    p.add_argument("--api-key", default=os.environ.get("E2B_API_KEY", DEFAULT_API_KEY))
    p.add_argument(
        "--cold-samples",
        type=int,
        default=10,
        help="kept cold-start samples (warmup is taken on top and discarded)",
    )
    p.add_argument(
        "--snapshot-samples",
        type=int,
        default=10,
        help="kept snapshot-start samples (warmup is taken on top and discarded)",
    )
    p.add_argument(
        "--cold-image",
        action="append",
        default=None,
        metavar="LABEL=REF",
        help="OCI image ref for a REAL cold boot (POST /sandboxes-cold, kernel boot — "
        "not a snapshot load). Repeat per arch with the same LABEL as --template, e.g. "
        "arm=127.0.0.1:6000/ubuntu-aenv-latency-bench:24.04-linuxarm64. When given, an "
        "extra 'VM冷启动(真boot)' row is measured alongside the template-snapshot 'VM冷启动' "
        "row; the two are not the same thing (boot vs snapshot load).",
    )
    p.add_argument(
        "--cold-boot-samples",
        type=int,
        default=10,
        help="kept real-cold-boot samples (cold-boot warmup is taken on top and discarded)",
    )
    p.add_argument(
        "--cold-boot-warmup",
        type=int,
        default=2,
        help="leading cold-boot samples to discard (the first cold pulls/converts OCI "
        "layers and is a tens-of-seconds outlier; 2 is safer than the snapshot warmup)",
    )
    p.add_argument(
        "--cold-cpu",
        type=int,
        default=2,
        help="cpuCount for the cold-boot sandbox (match your `aenv pull --cpu` so cold-boot "
        "and template-sandbox resources align; default 2)",
    )
    p.add_argument(
        "--cold-memory",
        type=int,
        default=4096,
        help="memoryMB for the cold-boot sandbox (match your `aenv pull --memory`; default 4096)",
    )
    p.add_argument("--concurrent", type=int, default=10, help="concurrent snapshot-start count")
    p.add_argument("--working-set-mib", type=int, default=256)
    p.add_argument(
        "--backing",
        choices=("shm", "file"),
        default="shm",
        help="latbench working-set backing store (shm default; file if shm does not survive resume)",
    )
    p.add_argument("--sandbox-timeout", type=float, default=300.0, help="per-sandbox lifecycle timeout (s)")
    p.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="leading cold/snapshot samples to discard as warmup (the first create/pause after a "
        "fresh template load runs cold and is an outlier; defaults to 1)",
    )
    p.add_argument(
        "--lazy-repeats",
        type=int,
        default=3,
        help="repeat each lazy-load mode's populate->pause->resume->measure cycle this many times; "
        "report median first/second (single-repeat lazy-load had no variance estimate)",
    )
    p.add_argument(
        "--control",
        action="store_true",
        help="also run a no-pause/resume control (populate+measure) to prove the lazy-load "
        "delta is snapshot-induced; first touch should ~= second touch",
    )
    p.add_argument(
        "--stress",
        choices=MODES,
        default=None,
        help="skip the suite and run a single `latbench stress` profile loop in a fresh "
        "sandbox (for attaching devkit_mem / ksys / perf to the host VMM and inspecting "
        "L2/L3 / EPT / TLB behavior over a multi-second resident-traversal window)",
    )
    p.add_argument(
        "--stress-iters",
        type=int,
        default=2000,
        help="iterations for the stress profile loop (bigger = longer sampling window; "
        "seq modes are bandwidth-bound so ~tens of ms/iter)",
    )
    p.add_argument(
        "--stress-timeout",
        type=int,
        default=600,
        help="floor for the stress-loop command timeout (s); auto-grows with iters",
    )
    p.add_argument("--output-dir", default="results/aenv_latency")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.working_set_mib <= 0:
        print("working-set-mib must be > 0", file=sys.stderr)
        sys.exit(2)
    if args.warmup < 0 or args.lazy_repeats < 1:
        print("warmup must be >= 0 and lazy-repeats must be >= 1", file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
