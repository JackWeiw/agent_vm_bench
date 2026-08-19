#!/usr/bin/env python3
"""Standalone REAL cold-boot benchmark for AgentENV (curl-based).

POST /sandboxes-cold (OCI image -> for_create_fresh -> start_fresh, a kernel boot,
NOT a snapshot load) N times per image and time the round-trip. Uses curl via
subprocess because that is the exact path proven to return 201 in ~0.6s; the
in-guest envd-ready is folded into create_ms (launch_sandbox wait_for_ready runs
before the 201), so create_ms IS the customer-comparable "VM cold start".

Compare x86 vs arm:
  python scripts/aenv_cold_boot_bench.py \
      --image x86=127.0.0.1:6000/ubuntu-aenv-latency-bench:24.04-x86_64 \
      --image arm=127.0.0.1:6000/ubuntu-aenv-latency-bench:24.04-linuxarm64

Single arch:
  E2B_API_URL=http://127.0.0.1:8000 E2B_API_KEY=e2b_000000 \
    python scripts/aenv_cold_boot_bench.py --image x86=127.0.0.1:6000/...:24.04-x86_64
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_API_KEY = "e2b_" + "0" * 40


def parse_images(specs: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for spec in specs:
        if "=" in spec:
            label, ref = spec.split("=", 1)
        else:
            label, ref = spec, spec
        out.append((label.strip(), ref.strip()))
    return out


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    f = int(k)
    if f + 1 < len(ordered):
        return ordered[f] + (ordered[f + 1] - ordered[f]) * (k - f)
    return ordered[-1]


def _curl(args: list[str], timeout: int) -> tuple[int, str, str, str]:
    """Run curl, return (http_code, time_total_s, body_text, stderr)."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 0, "0.0", "", "curl subprocess timed out"
    # -w output goes to stdout, body goes to -o file
    wout = proc.stdout.strip()
    body = ""
    body_path = next((a for i, a in enumerate(args) if a == "-o" and i + 1 < len(args)), None)
    if body_path and os.path.exists(body_path):
        try:
            with open(body_path, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            body = ""
    # parse "<code> <time_total>"
    code = 0
    total = 0.0
    parts = wout.split()
    if len(parts) >= 2:
        try:
            code = int(parts[0])
            total = float(parts[1])
        except ValueError:
            pass
    elif len(parts) == 1:
        try:
            code = int(parts[0])
        except ValueError:
            pass
    return code, total, body, proc.stderr.strip()


def cold_create(api_url: str, api_key: str, image: str, cpu: int, mem: int, ttl: int) -> tuple[int, float, str, str]:
    """POST /sandboxes-cold via curl. Returns (http_code, total_s, sandbox_id_or_empty, error)."""
    body = json.dumps({"image": image, "timeout": ttl, "autoPause": False, "cpuCount": cpu, "memoryMB": mem})
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as bf:
        body_path = bf.name
    # -o body file, -w "code time_total"
    args = [
        "curl",
        "-sS",
        "-X",
        "POST",
        f"{api_url.rstrip('/')}/sandboxes-cold",
        "-H",
        f"X-API-KEY: {api_key}",
        "-H",
        "Content-Type: application/json",
        "--max-time",
        str(ttl + 60),
        "-o",
        body_path,
        "-w",
        "%{http_code} %{time_total}",
        "-d",
        body,
    ]
    code, total, body_text, err = _curl(args, ttl + 90)
    try:
        os.unlink(body_path)
    except OSError:
        pass
    sandbox_id = ""
    if body_text:
        try:
            sandbox_id = json.loads(body_text).get("sandboxID", "")
        except Exception:  # noqa: BLE001
            err = err or body_text[:200]
    return code, total, sandbox_id, err


def cold_kill(api_url: str, api_key: str, sandbox_id: str) -> int:
    args = [
        "curl",
        "-sS",
        "-X",
        "DELETE",
        f"{api_url.rstrip('/')}/sandboxes/{sandbox_id}",
        "-H",
        f"X-API-KEY: {api_key}",
        "-w",
        "%{http_code}",
        "-o",
        "/dev/null",
    ]
    code, _, _, _ = _curl(args, 60)
    return code


@dataclass
class ImageResult:
    label: str
    image: str
    samples: list[float] = field(default_factory=list)  # total_s * 1000 (ms), kept only
    errors: list[str] = field(default_factory=list)
    warmup: int = 0


def measure(label: str, image: str, args: argparse.Namespace) -> ImageResult:
    ttl = int(args.timeout)
    total_runs = args.samples + args.warmup
    res = ImageResult(label=label, image=image, warmup=args.warmup)
    print(f"\n=== [{label}] real cold boot from OCI image ({args.samples} kept + {args.warmup} warmup) ===")
    print(f"    image={image}  cpu={args.cpu}  memory={args.memory}MB", flush=True)
    for i in range(total_runs):
        if args.gap > 0 and i > 0:
            time.sleep(args.gap)
        t0 = time.perf_counter()
        code, total, sid, err = cold_create(args.api_url, args.api_key, image, args.cpu, args.memory, ttl)
        wall_ms = (time.perf_counter() - t0) * 1000.0
        if code != 201:
            res.errors.append(f"#{i} HTTP {code} after {total * 1000.0:.0f}ms: {err[:120]}")
            print(f"  [{label}] #{i} FAILED HTTP {code} after {total * 1000.0:.0f}ms: {err[:120]}", flush=True)
            continue
        if i >= args.warmup:
            res.samples.append(total * 1000.0)
        print(
            f"  [{label}] #{i} 201 in {total * 1000.0:.1f}ms  (wall {wall_ms:.1f}ms) sid={sid[:8]}",
            flush=True,
        )
        if sid:
            kcode = cold_kill(args.api_url, args.api_key, sid)
            if kcode != 204:
                print(
                    f"  [{label}] kill {sid[:8]} -> HTTP {kcode} (leftover; autoPause=false TTL will reap it)",
                    flush=True,
                )
    return res


def median(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


def fmt(v: float | None) -> str:
    if v is None or v != v:
        return "n/a"
    return f"{v:.3f}"


def build_tsv(results: list[ImageResult]) -> str:
    labels = [r.label for r in results]
    lines = ["\t".join(["维度", "指标", *labels])]
    med = [fmt(median(r.samples)) for r in results]
    p99 = [fmt(pct(r.samples, 0.99) if r.samples else None) for r in results]
    lines.append("\t".join(["VM启动", "VM冷启动(真boot)", *med]))
    lines.append("\t".join(["VM启动", "VM冷启动(真boot)-p99", *p99]))
    return "\n".join(lines) + "\n"


def build_markdown(results: list[ImageResult]) -> str:
    labels = [r.label for r in results]
    header = "| 维度 | 指标 | " + " | ".join(labels) + " |"
    sep = "|---|---|" + "|".join(["---"] * len(labels)) + "|"
    lines = [header, sep]
    cells = ["VM启动", "VM冷启动(真boot)"] + [fmt(median(r.samples)) for r in results]
    lines.append("| " + " | ".join(cells) + " |")
    cells = ["VM启动", "VM冷启动(真boot)-p99"] + [fmt(pct(r.samples, 0.99) if r.samples else None) for r in results]
    lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def print_report(results: list[ImageResult]) -> None:
    print("\n" + "=" * 60)
    print("AgentENV real cold-boot — summary (median / p99 / spread)")
    print("=" * 60)
    for r in results:
        if r.samples:
            spread = f"{min(r.samples):.3f}..{max(r.samples):.3f}"
            print(
                f"  {r.label:<6} median={fmt(median(r.samples))}ms  p99={fmt(pct(r.samples, 0.99))}ms  "
                f"spread={spread}ms  (n={len(r.samples)}, +{r.warmup} warmup)"
            )
        else:
            print(f"  {r.label:<6} (no successful samples; {len(r.errors)} failures)")
        for e in r.errors[:5]:
            print(f"        err: {e}")
    print("\n" + build_markdown(results))


def main() -> int:
    p = argparse.ArgumentParser(description="AgentENV real cold-boot benchmark (curl-based)")
    p.add_argument(
        "--image", action="append", required=True, metavar="LABEL=REF", help="OCI image ref (repeat for x86 vs arm)"
    )
    p.add_argument("--api-url", default=os.environ.get("E2B_API_URL", DEFAULT_API_URL))
    p.add_argument("--api-key", default=os.environ.get("E2B_API_KEY", DEFAULT_API_KEY))
    p.add_argument("--samples", type=int, default=10, help="kept samples")
    p.add_argument(
        "--warmup", type=int, default=2, help="leading samples to discard (first cold pulls/converts OCI layers)"
    )
    p.add_argument("--cpu", type=int, default=2, help="cpuCount (match your `aenv pull --cpu`)")
    p.add_argument("--memory", type=int, default=4096, help="memoryMB (match your `aenv pull --memory`)")
    p.add_argument("--timeout", type=float, default=300.0, help="per-sandbox lifecycle TTL (s)")
    p.add_argument("--gap", type=float, default=1.0, help="seconds to sleep between cold boots")
    p.add_argument("--output-dir", default="results/aenv_cold_boot")
    args = p.parse_args()

    images = parse_images(args.image)
    results: list[ImageResult] = []
    for label, ref in images:
        results.append(measure(label, ref, args))

    print_report(results)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.output_dir) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(
            {
                "generated_at": stamp,
                "results": [
                    {
                        "label": r.label,
                        "image": r.image,
                        "samples_ms": r.samples,
                        "median_ms": median(r.samples),
                        "p99_ms": pct(r.samples, 0.99) if r.samples else None,
                        "warmup": r.warmup,
                        "errors": r.errors,
                    }
                    for r in results
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "report.tsv").write_text(build_tsv(results), encoding="utf-8")
    (out_dir / "report.md").write_text(build_markdown(results) + "\n", encoding="utf-8")
    print(f"\nReport written to {out_dir}/ (report.json, report.tsv, report.md)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
