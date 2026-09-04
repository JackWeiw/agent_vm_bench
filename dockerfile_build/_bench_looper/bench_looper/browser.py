#!/usr/bin/env python3
"""Browser scenario plugin: agent-browser tab mode (round-robin).

Per round (mirrors e2b_bench/task_runner.BrowserRoundRunner):
    open_tab -> page_load -> snapshot -> click -> screenshot

Each round opens a NEW tab (round-robin over the benchmark URLs); tabs are
never closed, so memory grows per round - the intended memory-pressure model
(creates swap-out events when accessing migrated memory).

Warmup opens one tab per warmup URL (snapshot -> click -> screenshot on each)
to preheat memory, matching the host WarmupRunner.

The browser fetches pages from an external http.server on the LAN, so the
container runs with host/bridge networking (NOT --network none) - unlike the
document bench, the page content is not bundled in the image.
"""

import re
import time

from bench_looper.core import BenchScenario, IterationResult, _error_detail, _error_type, load_operations, run_shell

# Extract `[ref=eN]` element refs from `agent-browser snapshot -i` output
# (ported verbatim from e2b_bench/task_runner.extract_element_refs).
ELEMENT_REF_RE = re.compile(r"\[ref=(e\d+)\]")

# Per-step shell timeouts (seconds). Mirrors the host BrowserRoundRunner so
# step timings are comparable across the E2B and in-image runners.
OPEN_TAB_TIMEOUT = 60  # agent-browser tab new
SNAPSHOT_TIMEOUT = 60  # agent-browser snapshot -i
CLICK_TIMEOUT = 30  # agent-browser click <ref>
SCREENSHOT_TIMEOUT = 30  # agent-browser screenshot
WARMUP_OPEN_TIMEOUT = 120  # warmup tab open (first tabs use a longer budget)
WARMUP_WAIT_TIMEOUT = 130
PAGE_LOAD_TIMEOUT = 70  # wrapper around `--timeout 60000ms`


def extract_element_refs(output: str) -> list[str]:
    """Extract element refs from agent-browser snapshot output (max 50)."""
    return ELEMENT_REF_RE.findall(output)[:50]


class BrowserBench(BenchScenario):
    name = "browser"

    def __init__(self, warmup_urls: list[str], benchmark_urls: list[str], warmup: bool = True):
        self.warmup_urls = warmup_urls
        self.benchmark_urls = benchmark_urls
        self._do_warmup = warmup

    @classmethod
    def build(cls, args) -> "BrowserBench":
        ops = load_operations("browser_urls.json")
        warmup_urls = args.warmup_urls or ops.get("warmup_urls", [])
        benchmark_urls = args.urls or ops.get("urls", [])
        return cls(warmup_urls, benchmark_urls, warmup=args.warmup)

    def run_warmup(self) -> None:
        if not self.warmup_urls:
            return
        for i, url in enumerate(self.warmup_urls):
            # First tab uses `open` (replaces the starter page); later tabs use `tab new`.
            cmd = f'agent-browser {"open" if i == 0 else "tab new"} "{url}"'
            run_shell(cmd, WARMUP_OPEN_TIMEOUT)
            run_shell("agent-browser wait --load domcontentloaded --timeout 120000", WARMUP_WAIT_TIMEOUT)
            self._tab_operations()

    def _tab_operations(self) -> None:
        """snapshot -> click -> screenshot on the active tab (all non-fatal)."""
        code, out, _ = run_shell("agent-browser snapshot -i", SNAPSHOT_TIMEOUT)
        if code != 0:
            return
        refs = extract_element_refs(out)
        if refs:
            run_shell(f"agent-browser click {refs[0]}", CLICK_TIMEOUT)
        run_shell("agent-browser screenshot", SCREENSHOT_TIMEOUT)

    def run_one_round(self, round_id: int) -> IterationResult:
        if not self.benchmark_urls:
            return IterationResult(
                round_id,
                False,
                failed_step="open_tab",
                error_type="exception",
                error_message="no benchmark urls configured",
            )
        url = self.benchmark_urls[round_id % len(self.benchmark_urls)]
        steps = {}

        # Step 1a: open new tab
        t = time.perf_counter()
        code, out, err = run_shell(f'agent-browser tab new "{url}"', OPEN_TAB_TIMEOUT)
        steps["open_tab"] = time.perf_counter() - t
        if code != 0:
            return IterationResult(
                round_id,
                False,
                steps,
                failed_step="open_tab",
                error_type=_error_type(code),
                error_message=_error_detail(code, out, err),
                timed_out=code == 124,
            )

        # Step 1b: wait for network idle (page fully loaded)
        t = time.perf_counter()
        code, out, err = run_shell("agent-browser wait --load networkidle --timeout 60000", PAGE_LOAD_TIMEOUT)
        steps["page_load"] = time.perf_counter() - t
        if code != 0:
            return IterationResult(
                round_id,
                False,
                steps,
                failed_step="page_load",
                error_type=_error_type(code),
                error_message=_error_detail(code, out, err),
                timed_out=code == 124,
            )

        # Step 2: DOM snapshot
        t = time.perf_counter()
        code, out, err = run_shell("agent-browser snapshot -i", SNAPSHOT_TIMEOUT)
        steps["snapshot"] = time.perf_counter() - t
        if code != 0:
            return IterationResult(
                round_id,
                False,
                steps,
                failed_step="snapshot",
                error_type=_error_type(code),
                error_message=_error_detail(code, out, err),
                timed_out=code == 124,
            )
        refs = extract_element_refs(out)

        # Step 3: click (non-fatal - mirrors host: failure only logged, not fatal)
        if refs:
            t = time.perf_counter()
            run_shell(f"agent-browser click {refs[0]}", CLICK_TIMEOUT)
            steps["click"] = time.perf_counter() - t

        # Step 4: screenshot (non-fatal)
        t = time.perf_counter()
        run_shell("agent-browser screenshot", SCREENSHOT_TIMEOUT)
        steps["screenshot"] = time.perf_counter() - t

        return IterationResult(round_id, True, steps)
