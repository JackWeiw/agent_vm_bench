#!/usr/bin/env python3
"""Bench looper CLI entry point.

Installed as three /usr/local/bin shims (browser-bench, coding-bench-go,
coding-bench-ts) that each exec this module with their scenario name as the
first positional argument:

    #!/bin/sh
    exec python3 /opt/bench-looper/bench_looper/runner.py browser "$@"

Default CMD is `sleep infinity` (long-running container for slicing
attachment); the entry points run one scenario end-to-end and exit.

Run end-to-end:
    docker run --rm --network host -v $PWD/results:/results \
        -e BENCH_RESULTS_DIR=/results <image> browser-bench --loops 100

The package is vendored at /opt/bench-looper/bench_looper; the parent
(/opt/bench-looper) is prepended to sys.path below so the absolute imports
resolve whether invoked as a script or via `python -m bench_looper.runner`.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench_looper.core import DEFAULT_LOOPS, DEFAULT_RESULTS_DIR, RESULTS_DIR_ENV, RUN_ID_ENV, BenchLooper
from bench_looper import browser, coding_go, coding_ts

SCENARIOS = {
    "browser": browser.BrowserBench,
    "coding-go": coding_go.CodingGoBench,
    "coding-ts": coding_ts.CodingTsBench,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bench-looper",
        description="In-image benchmark looper (browser / coding-go / coding-ts).",
    )
    p.add_argument("scenario", choices=list(SCENARIOS))
    p.add_argument("--loops", type=int, default=DEFAULT_LOOPS, help=f"round count (default {DEFAULT_LOOPS})")
    p.add_argument("--duration", type=float, default=0.0, help="wall-clock stop in seconds (0 = no limit)")
    p.add_argument("--warmup", dest="warmup", action="store_true", default=True)
    p.add_argument("--no-warmup", dest="warmup", action="store_false")
    p.add_argument("--results-dir", default=os.environ.get(RESULTS_DIR_ENV, DEFAULT_RESULTS_DIR))
    p.add_argument("--run-id", default=os.environ.get(RUN_ID_ENV))
    # Browser overrides
    p.add_argument("--urls", nargs="*", default=None, help="benchmark URLs (default: baked browser_urls.json)")
    p.add_argument("--warmup-urls", nargs="*", default=None, help="warmup URLs (default: baked browser_urls.json)")
    # Coding overrides
    p.add_argument("--skip-verify", action="store_true", help="coding: skip the verify step (edit-only)")
    p.add_argument("--verify-timeout", type=int, default=120, help="coding: verify command timeout in seconds")
    p.add_argument(
        "--verify-repeat", type=int, default=0, help="coding-ts: N npx tsx processes per verify (0 = profile default)"
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    scenario = SCENARIOS[args.scenario].build(args)
    looper = BenchLooper(
        scenario,
        loops=args.loops,
        duration=args.duration or None,
        warmup=args.warmup,
        results_dir=args.results_dir,
        run_id=args.run_id,
    )
    return looper.run()


if __name__ == "__main__":
    sys.exit(main())
