"""Unit tests for the pure disk-I/O rate helper in vm_monitor/base.py.

_compute_disk_io_rates does not touch /proc or /sys, so these run on any host.
"""

import unittest

from vm_monitor.base import _compute_disk_io_rates


class TestComputeDiskIoRates(unittest.TestCase):
    def _snap(self, sectors_read, sectors_written, ms_io, inflight):
        return {
            "sectors_read": sectors_read,
            "sectors_written": sectors_written,
            "ms_io": ms_io,
            "inflight": inflight,
            "reads_completed": 0,
            "writes_completed": 0,
            "read_ms": 0,
            "write_ms": 0,
            "weighted_ms": 0,
        }

    def test_normal_delta(self):
        cur = {"sda": self._snap(2048, 4096, 100, 2)}
        prev = {"sda": self._snap(0, 0, 0, 1)}
        r = _compute_disk_io_rates(cur, prev, 1.0)["sda"]
        # 2048 sectors * 512 B = 1 MiB; 4096 sectors = 2 MiB; util = (100/10)/1*100 capped 100
        self.assertAlmostEqual(r["r_mb_s"], 1.0)
        self.assertAlmostEqual(r["w_mb_s"], 2.0)
        self.assertEqual(r["util_pct"], 100.0)
        self.assertEqual(r["inflight"], 2)
        self.assertAlmostEqual(r["r_mb"], 1.0)
        self.assertAlmostEqual(r["w_mb"], 2.0)

    def test_util_below_cap(self):
        cur = {"sda": self._snap(100, 200, 5, 1)}
        prev = {"sda": self._snap(0, 0, 0, 0)}
        r = _compute_disk_io_rates(cur, prev, 1.0)["sda"]
        # util = (5/10)/1*100 = 50.0
        self.assertEqual(r["util_pct"], 50.0)

    def test_zero_or_none_interval_yields_zero_rates(self):
        cur = {"sda": self._snap(2048, 4096, 100, 3)}
        prev = {"sda": self._snap(0, 0, 0, 0)}
        for interval in (0, None, -1):
            r = _compute_disk_io_rates(cur, prev, interval)["sda"]
            self.assertEqual(r["r_mb_s"], 0.0)
            self.assertEqual(r["w_mb_s"], 0.0)
            self.assertEqual(r["util_pct"], 0.0)
            self.assertEqual(r["inflight"], 3)  # inflight is current-state, not a rate

    def test_missing_prev_treated_as_zero_baseline(self):
        # first sample: prev is None -> zero rates, inflight preserved
        cur = {"sda": self._snap(500, 500, 10, 4)}
        r = _compute_disk_io_rates(cur, None, 1.0)["sda"]
        self.assertEqual(r["r_mb_s"], 0.0)
        self.assertEqual(r["w_mb_s"], 0.0)
        self.assertEqual(r["inflight"], 4)

    def test_multiple_devices_independent(self):
        cur = {
            "sda": self._snap(2048, 4096, 100, 2),
            "sdb": self._snap(1024, 0, 0, 0),
        }
        prev = {
            "sda": self._snap(0, 0, 0, 1),
            "sdb": self._snap(0, 0, 0, 0),
        }
        r = _compute_disk_io_rates(cur, prev, 1.0)
        self.assertAlmostEqual(r["sda"]["w_mb_s"], 2.0)
        self.assertAlmostEqual(r["sdb"]["r_mb_s"], 0.5)  # 1024*512/2**20 = 0.5 MiB
        self.assertEqual(r["sdb"]["w_mb_s"], 0.0)

    def test_negative_delta_clamped_by_raw_sectors(self):
        # counters should never go backwards, but verify the math follows the raw delta
        cur = {"sda": self._snap(10, 10, 5, 1)}
        prev = {"sda": self._snap(100, 100, 0, 0)}
        r = _compute_disk_io_rates(cur, prev, 1.0)["sda"]
        self.assertEqual(r["r_mb_s"], round((10 - 100) * 512 / 2**20, 2))

    def _full_snap(self, reads, writes, read_ms, write_ms, weighted_ms, ms_io, inflight):
        return {
            "sectors_read": reads * 2,  # 2 sectors/read so MB math stays independent
            "sectors_written": writes * 2,
            "ms_io": ms_io,
            "inflight": inflight,
            "reads_completed": reads,
            "writes_completed": writes,
            "read_ms": read_ms,
            "write_ms": write_ms,
            "weighted_ms": weighted_ms,
        }

    def test_queue_depth_and_await(self):
        # 100 reads in 200 read-ms -> 2ms/read await; weighted_ms 3000 over 1s
        # interval -> queue depth = 3000/1000/1 = 3.0
        cur = {"sda": self._full_snap(100, 50, 200, 100, 3000, 1000, 2)}
        prev = {"sda": self._full_snap(0, 0, 0, 0, 0, 0, 0)}
        r = _compute_disk_io_rates(cur, prev, 1.0)["sda"]
        self.assertAlmostEqual(r["read_await_ms"], 2.0)
        self.assertAlmostEqual(r["write_await_ms"], 2.0)  # 100ms / 50 writes
        self.assertAlmostEqual(r["avg_queue_depth"], 3.0)

    def test_await_zero_when_no_io(self):
        # No completed I/Os in interval -> await is 0 (no div-by-zero)
        cur = {"sda": self._full_snap(0, 0, 0, 0, 0, 0, 1)}
        prev = {"sda": self._full_snap(0, 0, 0, 0, 0, 0, 0)}
        r = _compute_disk_io_rates(cur, prev, 1.0)["sda"]
        self.assertEqual(r["read_await_ms"], 0.0)
        self.assertEqual(r["write_await_ms"], 0.0)
        self.assertEqual(r["avg_queue_depth"], 0.0)

    def test_zero_interval_yields_zero_queue_await(self):
        cur = {"sda": self._full_snap(100, 50, 200, 100, 3000, 1000, 2)}
        prev = {"sda": self._full_snap(0, 0, 0, 0, 0, 0, 0)}
        r = _compute_disk_io_rates(cur, prev, 0)["sda"]
        self.assertEqual(r["avg_queue_depth"], 0.0)
        self.assertEqual(r["read_await_ms"], 0.0)
        self.assertEqual(r["write_await_ms"], 0.0)


if __name__ == "__main__":
    unittest.main()
