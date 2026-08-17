"""Unit tests for the pure page-cache pressure rate helper in vm_monitor/base.py.

_compute_pressure_rates does not touch /proc, so these run on any host.
"""

import unittest

from vm_monitor.base import _PAGE_SIZE, _compute_pressure_rates


def _snap(pgscan_kswapd, pgscan_direct, pgscan_direct_throttle, pgsteal_kswapd, pgsteal_direct, refault):
    return {
        "pgscan_kswapd": pgscan_kswapd,
        "pgscan_direct": pgscan_direct,
        "pgscan_direct_throttle": pgscan_direct_throttle,
        "pgsteal_kswapd": pgsteal_kswapd,
        "pgsteal_direct": pgsteal_direct,
        "workingset_refault_file": refault,
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

    def test_zero_or_none_interval_yields_zero(self):
        cur = _snap(1000, 1000, 1000, 500, 500, 250)
        prev = _snap(0, 0, 0, 0, 0, 0)
        for interval in (0, None, -1):
            r = _compute_pressure_rates(cur, prev, interval)
            self.assertEqual(r["page_scan_mib_s"], 0.0)
            self.assertEqual(r["page_reclaim_mib_s"], 0.0)
            self.assertEqual(r["file_refault_mib_s"], 0.0)

    def test_missing_prev_yields_zero(self):
        cur = _snap(1000, 1000, 1000, 500, 500, 250)
        r = _compute_pressure_rates(cur, None, 1.0)
        self.assertEqual(r["page_scan_mib_s"], 0.0)
        self.assertEqual(r["page_reclaim_mib_s"], 0.0)
        self.assertEqual(r["file_refault_mib_s"], 0.0)

    def test_negative_delta_clamped_to_zero(self):
        # counters should never go backwards; guard the math
        cur = _snap(0, 0, 0, 0, 0, 0)
        prev = _snap(100, 100, 100, 50, 50, 25)
        r = _compute_pressure_rates(cur, prev, 1.0)
        self.assertEqual(r["page_scan_mib_s"], 0.0)
        self.assertEqual(r["page_reclaim_mib_s"], 0.0)
        self.assertEqual(r["file_refault_mib_s"], 0.0)

    def test_interval_scales_rate(self):
        cur = _snap(262144, 0, 0, 0, 0, 0)
        prev = _snap(0, 0, 0, 0, 0, 0)
        r1 = _compute_pressure_rates(cur, prev, 1.0, 4096)
        r2 = _compute_pressure_rates(cur, prev, 2.0, 4096)
        # half the interval -> double the rate
        self.assertAlmostEqual(r2["page_scan_mib_s"], round(r1["page_scan_mib_s"] / 2, 3))


if __name__ == "__main__":
    unittest.main()
