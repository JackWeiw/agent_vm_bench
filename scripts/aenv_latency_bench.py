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

Usage (single arch):
  E2B_API_URL=http://127.0.0.1:8000 E2B_API_KEY=e2b_000... \
    python scripts/aenv_latency_bench.py --template arm=aenv-latency-arm

Compare x86 vs arm:
  python scripts/aenv_latency_bench.py \
      --template arm=aenv-latency-arm --template x86=aenv-latency-x86
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


@dataclass
class ArchResult:
    label: str
    cold: list[dict] = field(default_factory=list)  # {create_ms, ready_ms}
    snapshot: list[dict] = field(default_factory=list)  # {resume_ms, ready_ms}
    concurrent: dict | None = None
    lazy: dict = field(default_factory=dict)  # mode -> {first_ms, second_ms} or {error}
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
    for i, r in enumerate(raw):
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
        "resume_mean_ms": statistics.fmean(resume_vals) if resume_vals else 0.0,
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


async def measure_lazy(
    template: str,
    ws_mib: int,
    timeout: float,
    backing: str = "shm",
    keep: bool = False,
) -> tuple[dict, list[dict], Any | None]:
    from e2b import AsyncSandbox

    sb = await AsyncSandbox.create(template=template, timeout=timeout, request_timeout=timeout)
    await ready_probe(sb)
    lazy: dict = {}
    snap_samples: list[dict] = []
    latbench = "/usr/local/bin/latbench"
    for mode in MODES:
        # populate while running -> pause -> resume (lazy state) -> measure
        raw = await run_cmd(sb, f"{latbench} populate {ws_mib} {backing}", timeout=180)
        _, stderr, code = result_fields(raw)
        if code not in (None, 0):
            lazy[mode] = {"error": f"populate exit={code} stderr={stderr!r}"}
            continue
        await sb.beta_pause(request_timeout=timeout)
        t0 = time.perf_counter()
        sb = await sb.connect(timeout=timeout, request_timeout=timeout)
        resume_ms = (time.perf_counter() - t0) * 1000.0
        ready_ms = await ready_probe(sb) * 1000.0
        snap_samples.append({"resume_ms": resume_ms, "ready_ms": ready_ms})

        raw = await run_cmd(sb, f"{latbench} measure {ws_mib} {mode} {backing}", timeout=180)
        stdout, stderr, code = result_fields(raw)
        if code not in (None, 0):
            lazy[mode] = {"error": f"measure exit={code} stderr={stderr!r}"}
            await run_cmd(sb, f"{latbench} cleanup {backing}", timeout=30)
            continue
        parsed = parse_measure(stdout, mode)
        if parsed is None:
            lazy[mode] = {"error": f"unparseable stdout={stdout!r}"}
        else:
            lazy[mode] = {"first_ms": parsed[0], "second_ms": parsed[1]}
        await run_cmd(sb, f"{latbench} cleanup {backing}", timeout=30)

    last_sb = sb
    if not keep:
        try:
            await sb.kill()
        except Exception:
            pass
        last_sb = None
    return lazy, snap_samples, last_sb


async def run_suite(label: str, template: str, args: argparse.Namespace) -> ArchResult:
    # AgentENV deserializes `timeout` as u32; the SDK forwards it verbatim, so cast
    # to int here once and every downstream create/connect/pause call is covered.
    timeout = int(args.sandbox_timeout)
    res = ArchResult(label=label)

    print(f"\n=== [{label}] template={template} ===", flush=True)

    print(f"[{label}] cold start ({args.cold_samples} samples)...", flush=True)
    try:
        res.cold = await measure_cold(template, args.cold_samples, timeout)
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"cold: {type(exc).__name__}: {exc}")
        print(f"[{label}] cold FAILED: {exc}", flush=True)

    print(f"[{label}] snapshot start ({args.snapshot_samples} samples)...", flush=True)
    try:
        samples, _ = await measure_snapshot(template, args.snapshot_samples, timeout)
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

    print(f"[{label}] lazy-load latency ({args.working_set_mib}MiB, backing={args.backing})...", flush=True)
    try:
        lazy, snap, _ = await measure_lazy(template, args.working_set_mib, timeout, args.backing, keep=False)
        res.lazy = lazy
        if snap and not res.snapshot:
            res.snapshot = snap
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"lazy: {type(exc).__name__}: {exc}")
        print(f"[{label}] lazy FAILED: {exc}", flush=True)

    return res


def fmt_ms(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v != v:  # NaN
        return "n/a"
    return f"{v:.1f}"


def cold_total_ms(c: dict) -> float:
    r = c.get("ready_ms", float("nan"))
    return c["create_ms"] + (r if r == r else 0.0)


def snap_total_ms(s: dict) -> float:
    r = s.get("ready_ms", float("nan"))
    return s["resume_ms"] + (r if r == r else 0.0)


def print_report(results: list[ArchResult]) -> None:
    print("\n" + "=" * 72)
    print("AgentENV latency benchmark — summary")
    print("=" * 72)

    for r in results:
        print(f"\n## {r.label}")
        if r.cold:
            totals = [cold_total_ms(c) for c in r.cold]
            print(
                f"  VM cold start       mean={fmt_ms(statistics.fmean(totals))}ms  "
                f"p50={fmt_ms(pct(totals, 0.5))}ms  p99={fmt_ms(pct(totals, 0.99))}ms  "
                f"(n={len(totals)})"
            )
        else:
            print("  VM cold start       (no data)")
        if r.snapshot:
            totals = [snap_total_ms(s) for s in r.snapshot]
            print(
                f"  VM snapshot start   mean={fmt_ms(statistics.fmean(totals))}ms  "
                f"p50={fmt_ms(pct(totals, 0.5))}ms  p99={fmt_ms(pct(totals, 0.99))}ms  "
                f"(n={len(totals)})"
            )
        else:
            print("  VM snapshot start   (no data)")
        if r.concurrent:
            c = r.concurrent
            print(
                f"  {c['k']}-concurrent snap  wall={fmt_ms(c['wall_ms'])}ms  "
                f"resume_mean={fmt_ms(c['resume_mean_ms'])}ms  "
                f"total_max={fmt_ms(c['total_max_ms'])}ms"
            )
        else:
            print("  concurrent snapshot (no data)")
        print("  lazy-load latency (first / second, ms):")
        for mode in MODES:
            e = r.lazy.get(mode)
            if not e:
                print(f"    {mode:12s} (no data)")
            elif "error" in e:
                print(f"    {mode:12s} ERROR: {e['error']}")
            else:
                print(f"    {mode:12s} first={fmt_ms(e['first_ms'])}ms  " f"second={fmt_ms(e['second_ms'])}ms")

    if len(results) >= 2:
        print("\n## x86 vs arm delta (snapshot start mean)")
        for r in results:
            if r.snapshot:
                totals = [snap_total_ms(s) for s in r.snapshot]
                print(f"  {r.label}: {fmt_ms(statistics.fmean(totals))}ms")

    errs = [f"[{r.label}] {e}" for r in results for e in r.errors]
    if errs:
        print("\n## errors")
        for e in errs:
            print(f"  {e}")


def to_jsonable(r: ArchResult) -> dict:
    return {
        "label": r.label,
        "cold": r.cold,
        "snapshot": r.snapshot,
        "concurrent": r.concurrent,
        "lazy": r.lazy,
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
    return f"{v:.1f}"


def _cold_mean(r: ArchResult) -> float | None:
    if not r.cold:
        return None
    return statistics.fmean(cold_total_ms(c) for c in r.cold)


def _snap_mean(r: ArchResult) -> float | None:
    if not r.snapshot:
        return None
    return statistics.fmean(snap_total_ms(s) for s in r.snapshot)


def _conc_wall(r: ArchResult) -> float | None:
    return r.concurrent["wall_ms"] if r.concurrent else None


def _lazy_value(mode: str, key: str):
    def getter(r: ArchResult) -> float | None:
        e = r.lazy.get(mode)
        if not e or "error" in e:
            return None
        return e[key]

    return getter


# Fixed row order mirroring the customer benchmark table so runs from
# different machines paste-align row-by-row.
TABLE_ROWS: list[tuple[str, str]] = [
    ("VM启动", "VM冷启动"),
    ("VM启动", "VM快照启动"),
    ("VM启动", "10并发快照启动"),
    ("快照lazy load", "顺序遍历读-首读"),
    ("快照lazy load", "顺序遍历读-次读"),
    ("快照lazy load", "顺序遍历写-首写"),
    ("快照lazy load", "顺序遍历写-次写"),
    ("快照lazy load", "随机遍历读-首读"),
    ("快照lazy load", "随机遍历读-次读"),
    ("快照lazy load", "随机遍历写-首写"),
    ("快照lazy load", "随机遍历写-次写"),
]


def _row_getter(metric: str):
    if metric == "VM冷启动":
        return _cold_mean
    if metric == "VM快照启动":
        return _snap_mean
    if metric == "10并发快照启动":
        return _conc_wall
    mode_map = {
        "顺序遍历读-首读": ("seq_read", "first_ms"),
        "顺序遍历读-次读": ("seq_read", "second_ms"),
        "顺序遍历写-首写": ("seq_write", "first_ms"),
        "顺序遍历写-次写": ("seq_write", "second_ms"),
        "随机遍历读-首读": ("rand_read", "first_ms"),
        "随机遍历读-次读": ("rand_read", "second_ms"),
        "随机遍历写-首写": ("rand_write", "first_ms"),
        "随机遍历写-次写": ("rand_write", "second_ms"),
    }
    mode, key = mode_map[metric]
    return _lazy_value(mode, key)


def build_tsv(results: list[ArchResult]) -> str:
    labels = [r.label for r in results]
    lines = ["\t".join(["维度", "指标", *labels])]
    for dim, metric in TABLE_ROWS:
        getter = _row_getter(metric)
        cells = [dim, metric] + [_cell(getter(r)) for r in results]
        lines.append("\t".join(cells))
    return "\n".join(lines) + "\n"


def build_markdown(results: list[ArchResult]) -> str:
    labels = [r.label for r in results]
    header = "| 维度 | 指标 | " + " | ".join(labels) + " |"
    sep = "|---|---|" + "|".join(["---"] * len(labels)) + "|"
    lines = [header, sep]
    for dim, metric in TABLE_ROWS:
        getter = _row_getter(metric)
        cells = [dim, metric] + [_cell(getter(r)) for r in results]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    configure_env(args.api_url, args.api_key)
    templates = parse_templates(args.template)
    results: list[ArchResult] = []
    for label, alias in templates:
        res = await run_suite(label, alias, args)
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
    p.add_argument("--cold-samples", type=int, default=5)
    p.add_argument("--snapshot-samples", type=int, default=5)
    p.add_argument("--concurrent", type=int, default=10, help="concurrent snapshot-start count")
    p.add_argument("--working-set-mib", type=int, default=256)
    p.add_argument(
        "--backing",
        choices=("shm", "file"),
        default="shm",
        help="latbench working-set backing store (shm default; file if shm does not survive resume)",
    )
    p.add_argument("--sandbox-timeout", type=float, default=300.0, help="per-sandbox lifecycle timeout (s)")
    p.add_argument("--output-dir", default="results/aenv_latency")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.working_set_mib <= 0:
        print("working-set-mib must be > 0", file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
