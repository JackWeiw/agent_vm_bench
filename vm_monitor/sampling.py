"""Per-VM sampling cadence and the numa_maps parser.

Two costs dominate one sampling cycle on a large fleet: discovering VMs, and
re-reading each VM's memory files (numa_maps + smaps_rollup). Both of those
walks walk the target's page tables -- measured at a 192-VM fleet, ~35 ms per
GiB when the mapping is 4 KiB-backed and ~12 ms per GiB when THP-backed (the
production shape). At 384 x 4 GiB THP-backed that extrapolates to roughly 18
CPU-seconds per 2 s sample, i.e. ~9 cores; the monitor would perturb the
benchmark it exists to measure.

``HeavyRefreshScheduler`` keeps that pass inside a fixed CPU budget. This
module is the policy seam: a monitor holds one, calls it once per cycle, and a
different policy (priority refresh, event-driven refresh) is a drop-in
replacement subclass rather than an edit to the monitor.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def parse_numa_maps(text: str, base_page_kb: int) -> dict:
    """Aggregate one /proc/<pid>/numa_maps document into per-node MB totals.

    ``base_page_kb`` is the host base page size; numa_maps reports per-line
    page counts and, for huge mappings, its own ``kernelpagesize_kB``. The
    swap cache is always base-page granular regardless of that key.

    Pure text in, dict out -- the caller owns the file I/O, so this is the
    part that can be unit-tested on a literal and reasoned about on its own.
    """
    result = {
        "total_mb": 0.0,
        "huge_mb": 0.0,
        "private_mb": 0.0,
        "heap_mb": 0.0,
        "stack_mb": 0.0,
        "per_node": {},
        "swapcache_mb": 0.0,
        "swapcache_per_node": {},
    }
    base_page_size_mb = base_page_kb / 1024.0
    # Local accumulators. Rebuilding the per_node dict-of-dicts on every line
    # cost ~100 us/VM on a 17-line file (~0.4 CPU-s per sample at 3.8k VMs);
    # node -> [total, huge, heap, stack, private] is the same arithmetic with
    # a single list lookup per line.
    node_totals: dict[int, list[float]] = {}
    total_mb = huge_mb = private_mb = heap_mb = stack_mb = swapcache_mb = 0.0
    swapcache_per_node: dict[int, float] = {}

    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 2:
            continue

        # tokens[0] is the virtual address range; the rest are key=value
        # pairs plus a few bare markers.
        node_pages: dict[int, int] = {}
        anon_pages = 0
        kernelpagesize_kb = base_page_kb
        is_heap = is_stack = is_huge = False
        swapcache_pages = 0

        for token in tokens[1:]:
            if "=" not in token:
                # Bare markers; "default" and policy tokens (bind:N,
                # interleave:N-M, prefer:N) carry no value and are ignored.
                if token == "heap":
                    is_heap = True
                elif token == "stack":
                    is_stack = True
                elif token == "huge":
                    is_huge = True
                continue
            key, _, value = token.partition("=")
            if not value.isdigit():
                continue  # file, mapped, dirty, active, mapmax — informational
            if key[:1] == "N" and key[1:].isdigit():
                node_pages[int(key[1:])] = int(value)
            elif key == "anon":
                anon_pages = int(value)
            elif key == "kernelpagesize_kB":
                kernelpagesize_kb = int(value)
            elif key == "swapcache":
                swapcache_pages = int(value)

        if not node_pages:  # no physical pages on this mapping
            continue

        if kernelpagesize_kb > base_page_kb:
            is_huge = True

        line_pages = sum(node_pages.values())
        page_size_mb = kernelpagesize_kb / 1024.0
        line_total_mb = line_pages * page_size_mb

        total_mb += line_total_mb
        if is_huge:
            huge_mb += line_total_mb
        if is_heap:
            heap_mb += line_total_mb
        if is_stack:
            stack_mb += line_total_mb

        # anon pages are the private set; they share the mapping's page size.
        anon_mb = anon_pages * page_size_mb
        if anon_mb > 0:
            private_mb += anon_mb
        swap_line_mb = swapcache_pages * base_page_size_mb
        if swap_line_mb > 0:
            swapcache_mb += swap_line_mb

        for node_id, pages in node_pages.items():
            acc = node_totals.get(node_id)
            if acc is None:
                acc = node_totals[node_id] = [0.0, 0.0, 0.0, 0.0, 0.0]
            node_mb = pages * page_size_mb
            acc[0] += node_mb
            if is_huge:
                acc[1] += node_mb
            if is_heap:
                acc[2] += node_mb
            if is_stack:
                acc[3] += node_mb
            proportion = pages / line_pages
            if anon_mb > 0:
                acc[4] += anon_mb * proportion
            if swap_line_mb > 0:
                swapcache_per_node[node_id] = swapcache_per_node.get(node_id, 0.0) + swap_line_mb * proportion

    result["total_mb"] = total_mb
    result["huge_mb"] = huge_mb
    result["private_mb"] = private_mb
    result["heap_mb"] = heap_mb
    result["stack_mb"] = stack_mb
    result["swapcache_mb"] = swapcache_mb
    result["per_node"] = {
        node_id: {
            "total_mb": values[0],
            "huge_mb": values[1],
            "heap_mb": values[2],
            "stack_mb": values[3],
            "private_mb": values[4],
        }
        for node_id, values in node_totals.items()
    }
    result["swapcache_per_node"] = swapcache_per_node
    return result


class HeavyRefreshScheduler:
    """Round-robin refresh of per-VM memory fields inside a CPU budget.

    Each cycle refreshes one round-robin slice of the fleet; VMs that are not
    due report their last measured values. ``stride`` (samples per full pass)
    follows the measured per-VM cost, so small fleets and cheap, THP-backed
    fleets stay at 1 and behave exactly as they did before.

    A pid with no cached value -- new VM, first cycle, or a recycled pid whose
    start time changed -- is always read, so a carried-forward value never
    outlives the process it was measured on.
    """

    def __init__(self, cpu_fraction: float = 0.25) -> None:
        # Fraction of one core the heavy pass may spend per sample interval.
        self.cpu_fraction = cpu_fraction
        self.interval = 2.0
        self.stride = 1
        self._cache: dict[int, dict] = {}
        self._slots: dict[int, int] = {}
        self._starts: dict[int, int] = {}  # pid -> start time, spots pid reuse
        self._slot_seq = 0
        self._cycle = 0
        self._due: set[int] = set()
        self._cpu_cycle = 0.0
        self._refreshed = 0
        # Measured CPU seconds per VM per refresh (EWMA); public so a caller
        # can seed or inspect the estimate the stride is derived from.
        self.cost_per_vm = 0.0

    def prepare(self, vm_candidates: list[dict], interval: float) -> set[int]:
        """Pick the VMs that re-read their memory files this cycle.

        Returns the pids whose identity changed (a recycled pid), so the
        caller can drop the CPU baseline those pids accumulated as another
        process. Empty in the normal case.
        """
        self.interval = interval if interval > 0 else 2.0
        self._due = set()
        self._cpu_cycle = 0.0
        self._refreshed = 0

        live = {c["pid"] for c in vm_candidates}
        for cache in (self._cache, self._slots, self._starts):
            for pid in [p for p in cache if p not in live]:
                cache.pop(pid, None)

        # A recycled pid is a different VM: drop its cached values, and report
        # it so the caller's CPU baseline reseeds instead of computing a delta
        # across two processes.
        reused = set()
        for candidate in vm_candidates:
            pid = candidate["pid"]
            start = candidate.get("pid_start")
            if start is None:
                continue
            if self._starts.setdefault(pid, start) != start:
                self._starts[pid] = start
                self._cache.pop(pid, None)
                self._slots.pop(pid, None)
                reused.add(pid)

        if not vm_candidates:
            return reused
        if self.stride <= 1:
            self._due = live
            return reused
        slot = self._cycle % self.stride
        self._cycle += 1
        for candidate in vm_candidates:
            pid = candidate["pid"]
            if pid not in self._slots:
                self._slots[pid] = self._slot_seq
                self._slot_seq += 1
            if pid not in self._cache or self._slots[pid] % self.stride == slot:
                self._due.add(pid)
        return reused

    def fields_for(self, pid: int, read_fn) -> tuple[dict, float | None]:
        """Fields for one VM plus the CPU seconds a fresh read cost.

        ``read_fn(pid)`` is called only when the VM is due (or has no cached
        value); a cache hit returns ``(cached, None)`` and costs no I/O.
        """
        if pid not in self._due and pid in self._cache:
            return self._cache[pid], None
        started = time.thread_time()
        fields = read_fn(pid)
        return fields, time.thread_time() - started

    def record(self, pid: int, fields: dict, cpu_sec: float) -> None:
        """Store a fresh read and charge it to this cycle's budget."""
        self._cache[pid] = fields
        self._cpu_cycle += cpu_sec
        self._refreshed += 1

    def end_cycle(self, n_vms: int) -> None:
        """Fold this cycle's measured cost into the stride for the next one."""
        if self._refreshed:
            per_vm = self._cpu_cycle / self._refreshed
            self.cost_per_vm = per_vm if self.cost_per_vm == 0 else 0.7 * self.cost_per_vm + 0.3 * per_vm
        if n_vms <= 0:
            self.stride = 1
            return
        budget = self.cpu_fraction * self.interval
        full_cost = self.cost_per_vm * n_vms
        if budget <= 0 or full_cost <= budget:
            stride = 1
        else:
            stride = int(full_cost / budget) + (1 if full_cost % budget else 0)
        if stride != self.stride:
            self.stride = stride
            if stride > 1:
                logger.info(
                    "Per-VM memory refresh: every %d samples (%.1f s at %.0f s interval); "
                    "estimated full pass %.2f CPU-s vs %.2f CPU-s budget",
                    stride,
                    stride * self.interval,
                    self.interval,
                    full_cost,
                    budget,
                )
