"""Unit tests for the pure page-cache pressure rate helper in vm_monitor/base.py.

_compute_pressure_rates does not touch /proc, so these run on any host.
"""

import unittest

from vm_monitor.base import _PAGE_SIZE, _compute_pressure_rates


def _snap(pgscan_kswapd, pgscan_direct, pgscan_direct_throttle, pgsteal_kswapd, pgsteal_direct, refault, allocstall=0):
    # Modern /proc/vmstat exposes allocstall per zone-type (allocstall_normal/
    # _dma32/...), not a plain sum. Carrier the value on allocstall_normal
    # (the zone that actually carries stalls on most configs) so the fixture
    # mirrors reality -- a plain "allocstall" key would never exist in /proc/vmstat
    # and would give false confidence that the production path works.
    return {
        "pgscan_kswapd": pgscan_kswapd,
        "pgscan_direct": pgscan_direct,
        "pgscan_direct_throttle": pgscan_direct_throttle,
        "pgsteal_kswapd": pgsteal_kswapd,
        "pgsteal_direct": pgsteal_direct,
        "workingset_refault_file": refault,
        "allocstall_normal": allocstall,
    }


class TestComputePressureRates(unittest.TestCase):
    def test_normal_delta(self):
        # 262144 pages scanned over 1s = 262144 * page_size / 2**20 MiB
        scan_pages = 262144
        cur = _snap(scan_pages, 0, 0, scan_pages // 2, 0, scan_pages // 4)
        prev = _snap(0, 0, 0, 0, 0, 0)
        r = _compute_pressure_rates(cur, prev, 1.0, _PAGE_SIZE)
        mib_per_page = _PAGE_SIZE / 2**20
        self.assertAlmostEqual(r["page_scan_mib_s"], round(scan_pages * mib_per_page, 3))
        self.assertAlmostEqual(r["page_reclaim_mib_s"], round((scan_pages // 2) * mib_per_page, 3))
        self.assertAlmostEqual(r["file_refault_mib_s"], round((scan_pages // 4) * mib_per_page, 3))

    def test_scan_sums_all_three_counters(self):
        cur = _snap(10, 20, 30, 0, 0, 0)
        prev = _snap(0, 0, 0, 0, 0, 0)
        r = _compute_pressure_rates(cur, prev, 1.0, 4096)
        # 60 pages * 4096 B / 2**20 = 0.234375 MiB
        self.assertAlmostEqual(r["page_scan_mib_s"], round(60 * 4096 / 2**20, 3))

    def test_direct_reclaim_excludes_kswapd(self):
        # direct=20, throttle=30 -> 50 blocking pages; kswapd=10 excluded.
        cur = _snap(10, 20, 30, 0, 0, 0)
        prev = _snap(0, 0, 0, 0, 0, 0)
        r = _compute_pressure_rates(cur, prev, 1.0, 4096)
        self.assertAlmostEqual(r["direct_reclaim_mib_s"], round(50 * 4096 / 2**20, 3))
        # allocstall=7 over 1s -> 7/s
        cur7 = _snap(0, 0, 0, 0, 0, 0, allocstall=7)
        prev0 = _snap(0, 0, 0, 0, 0, 0, allocstall=0)
        r7 = _compute_pressure_rates(cur7, prev0, 1.0)
        self.assertEqual(r7["allocstall_per_s"], 7.0)

    def test_allocstall_rate_scales_with_interval(self):
        cur = _snap(0, 0, 0, 0, 0, 0, allocstall=100)
        prev = _snap(0, 0, 0, 0, 0, 0, allocstall=0)
        self.assertEqual(_compute_pressure_rates(cur, prev, 1.0)["allocstall_per_s"], 100.0)
        self.assertEqual(_compute_pressure_rates(cur, prev, 2.0)["allocstall_per_s"], 50.0)

    def test_allocstall_sums_per_zone_variants(self):
        # /proc/vmstat exposes allocstall per zone-type; the rate must sum them.
        cur = {
            "pgscan_kswapd": 0,
            "pgscan_direct": 0,
            "pgscan_direct_throttle": 0,
            "pgsteal_kswapd": 0,
            "pgsteal_direct": 0,
            "workingset_refault_file": 0,
            "allocstall_normal": 10,
            "allocstall_dma32": 5,
            "allocstall_movable": 3,
        }
        prev = {**cur, "allocstall_normal": 0, "allocstall_dma32": 0, "allocstall_movable": 0}
        r = _compute_pressure_rates(cur, prev, 1.0)
        # 10 + 5 + 3 = 18 stalls over 1s
        self.assertEqual(r["allocstall_per_s"], 18.0)

    def test_allocstall_legacy_plain_field_when_no_variants(self):
        # Legacy kernels exposed a single allocstall sum. With no allocstall_*
        # variants present, the fallback reads the plain field so the metric
        # isn't a silent zero.
        cur = {"allocstall": 7}
        prev = {"allocstall": 0}
        self.assertEqual(_compute_pressure_rates(cur, prev, 1.0)["allocstall_per_s"], 7.0)

    def test_zero_or_none_interval_yields_zero(self):
        cur = _snap(1000, 1000, 1000, 500, 500, 250, allocstall=10)
        prev = _snap(0, 0, 0, 0, 0, 0)
        for interval in (0, None, -1):
            r = _compute_pressure_rates(cur, prev, interval)
            self.assertEqual(r["page_scan_mib_s"], 0.0)
            self.assertEqual(r["page_reclaim_mib_s"], 0.0)
            self.assertEqual(r["file_refault_mib_s"], 0.0)
            self.assertEqual(r["direct_reclaim_mib_s"], 0.0)
            self.assertEqual(r["allocstall_per_s"], 0.0)

    def test_missing_prev_yields_zero(self):
        cur = _snap(1000, 1000, 1000, 500, 500, 250, allocstall=10)
        r = _compute_pressure_rates(cur, None, 1.0)
        self.assertEqual(r["page_scan_mib_s"], 0.0)
        self.assertEqual(r["page_reclaim_mib_s"], 0.0)
        self.assertEqual(r["file_refault_mib_s"], 0.0)
        self.assertEqual(r["direct_reclaim_mib_s"], 0.0)
        self.assertEqual(r["allocstall_per_s"], 0.0)

    def test_negative_delta_clamped_to_zero(self):
        # counters should never go backwards; guard the math
        cur = _snap(0, 0, 0, 0, 0, 0)
        prev = _snap(100, 100, 100, 50, 50, 25, allocstall=100)
        r = _compute_pressure_rates(cur, prev, 1.0)
        self.assertEqual(r["page_scan_mib_s"], 0.0)
        self.assertEqual(r["page_reclaim_mib_s"], 0.0)
        self.assertEqual(r["file_refault_mib_s"], 0.0)
        self.assertEqual(r["direct_reclaim_mib_s"], 0.0)
        self.assertEqual(r["allocstall_per_s"], 0.0)

    def test_interval_scales_rate(self):
        cur = _snap(262144, 0, 0, 0, 0, 0)
        prev = _snap(0, 0, 0, 0, 0, 0)
        r1 = _compute_pressure_rates(cur, prev, 1.0, 4096)
        r2 = _compute_pressure_rates(cur, prev, 2.0, 4096)
        # half the interval -> double the rate
        self.assertAlmostEqual(r2["page_scan_mib_s"], round(r1["page_scan_mib_s"] / 2, 3))


if __name__ == "__main__":
    unittest.main()
