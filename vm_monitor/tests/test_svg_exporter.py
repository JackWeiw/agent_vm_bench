"""Unit tests for vm_monitor/svg_exporter.py.

The renderer is pure (no /proc, no external libs), so these run on any host.
Asserts: pure helpers, well-formed SVG output, expected reports written, and
that empty histories skip their report.
"""

import math
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from vm_monitor.base import VMMonitorBase
from vm_monitor.svg_exporter import (
    _elapsed_series,
    _finite,
    _nice_max,
    _svg_escape,
    export_svg_reports,
)


class DummyMonitor(VMMonitorBase):
    def get_vms_realtime(self):
        return []

    def get_process_names(self):
        return ("test_process",)

    def extract_vm_id(self, pid, cmdline):
        return "vm0"

    def get_monitor_title(self):
        return "DummyMonitor"

    def get_no_vm_message(self):
        return "No VMs detected"

    def get_csv_filename_prefix(self):
        return "dummy_monitor"


def _full_monitor():
    """Populate every history the SVG exporter reads (2 samples each)."""
    m = DummyMonitor()
    m.interval = 2.0
    m.target_disks = ["sda", "sdb"]

    def dr(r, w, util, ifl, q, ra, wa):
        return {
            "r_mb_s": r,
            "w_mb_s": w,
            "util_pct": util,
            "inflight": ifl,
            "r_mb": r,
            "w_mb": w,
            "avg_queue_depth": q,
            "read_await_ms": ra,
            "write_await_ms": wa,
        }

    m.disk_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "disks": {
                "sda": dr(10.0, 20.0, 5.0, 1, 0.5, 2.0, 3.0),
                "sdb": dr(5.0, 15.0, 3.0, 2, 1.0, 4.0, 5.0),
            },
        },
        {
            "ts": "2026-08-14 10:00:02",
            "disks": {
                "sda": dr(12.0, 25.0, 6.0, 1, 0.8, 2.5, 3.5),
                "sdb": dr(6.0, 18.0, 4.0, 3, 1.2, 4.5, 5.5),
            },
        },
    ]
    m.ublk_history = [
        {"ts": "2026-08-14 10:00:00", "ublk_devices": 3},
        {"ts": "2026-08-14 10:00:02", "ublk_devices": 4},
    ]
    m.host_cpu_history = [40.0, 50.0]
    m.host_mem_history = [
        {"used_mb": 8000.0, "total_mb": 16384.0, "usage": 48.8},
        {"used_mb": 9000.0, "total_mb": 16384.0, "usage": 54.9},
    ]
    m.host_mem_detail_history = [
        {"ts": "2026-08-14 10:00:00", "cached_mb": 1000.0, "buffers_mb": 50.0, "dirty_mb": 10.0, "writeback_mb": 5.0},
        {"ts": "2026-08-14 10:00:02", "cached_mb": 1100.0, "buffers_mb": 55.0, "dirty_mb": 20.0, "writeback_mb": 8.0},
    ]
    m.host_pressure_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "page_scan_mib_s": 5.0,
            "page_reclaim_mib_s": 4.0,
            "file_refault_mib_s": 1.0,
            "anon_pages_mb": 2000.0,
            "file_cache_mb": 3000.0,
            "sreclaimable_mb": 100.0,
            "iowait_pct": 2.0,
            "procs_running": 4,
            "procs_blocked": 0,
        },
        {
            "ts": "2026-08-14 10:00:02",
            "page_scan_mib_s": 8.0,
            "page_reclaim_mib_s": 6.0,
            "file_refault_mib_s": 2.0,
            "anon_pages_mb": 2100.0,
            "file_cache_mb": 3100.0,
            "sreclaimable_mb": 110.0,
            "iowait_pct": 5.0,
            "procs_running": 6,
            "procs_blocked": 1,
        },
    ]
    m.peak_page_scan_mib_s = 8.0
    m.peak_page_reclaim_mib_s = 6.0
    m.peak_file_refault_mib_s = 2.0
    m.dirty_limit_mb = 500.0
    m.dirty_background_limit_mb = 250.0
    m._dirty_limits_read = True
    m.swap_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "capacity": {"used_mb": 100.0, "free_mb": 1900.0, "total_mb": 2000.0},
            "cache": {"cached_mb": 50.0, "cached_ratio_pct": 2.5},
            "activity": {"swap_in_rate": 10.0, "swap_out_rate": 5.0},
        },
        {
            "ts": "2026-08-14 10:00:02",
            "capacity": {"used_mb": 200.0, "free_mb": 1800.0, "total_mb": 2000.0},
            "cache": {"cached_mb": 60.0, "cached_ratio_pct": 3.0},
            "activity": {"swap_in_rate": 20.0, "swap_out_rate": 10.0},
        },
    ]
    m.numa_memory_history = [
        {"ts": "2026-08-14 10:00:00", "nodes": [{"node": 0, "used_mb": 1000.0}, {"node": 5, "used_mb": 2000.0}]},
        {"ts": "2026-08-14 10:00:02", "nodes": [{"node": 0, "used_mb": 1200.0}, {"node": 5, "used_mb": 2400.0}]},
    ]
    m.numa_cpu_history = {0: [10.0, 20.0], 5: [30.0, 40.0]}
    m.vm_total_memory_history = [
        {"ts": "2026-08-14 10:00:00", "total_mb": 6144.0, "vm_count": 2, "per_numa": {0: 3072.0, 5: 3072.0}},
        {"ts": "2026-08-14 10:00:02", "total_mb": 6200.0, "vm_count": 2, "per_numa": {0: 3100.0, 5: 3100.0}},
    ]
    return m


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


class TestPureHelpers(unittest.TestCase):
    def test_svg_escape(self):
        self.assertEqual(_svg_escape("a<b>&'c\"d"), "a&lt;b&gt;&amp;'c&quot;d")

    def test_finite_filters_nan_inf(self):
        out = _finite([1.0, float("nan"), 2.0, float("inf"), 3])
        self.assertEqual(out, [1.0, 2.0, 3])

    def test_nice_max(self):
        self.assertEqual(_nice_max(0), 1.0)
        self.assertEqual(_nice_max(-5), 1.0)
        self.assertAlmostEqual(_nice_max(8.0), 10.0)
        self.assertAlmostEqual(_nice_max(2.0), 2.0)
        self.assertAlmostEqual(_nice_max(0.3), 0.5)  # 0.3 -> scaled 3 -> step 5 -> 0.5
        self.assertAlmostEqual(_nice_max(50.0), 50.0)
        self.assertAlmostEqual(_nice_max(900.0), 1000.0)

    def test_elapsed_series_from_ts(self):
        h = [
            {"ts": "2026-08-14 10:00:00"},
            {"ts": "2026-08-14 10:00:02"},
            {"ts": "2026-08-14 10:00:05"},
        ]
        self.assertEqual(_elapsed_series(h, 2.0), [0.0, 2.0, 5.0])

    def test_elapsed_series_fallback_index_interval(self):
        # No ts -> index*interval (interval clamped to >= 1)
        h = [{"x": 1}, {"x": 2}, {"x": 3}]
        self.assertEqual(_elapsed_series(h, 3.0), [0.0, 3.0, 6.0])
        # interval 0 -> step 1
        self.assertEqual(_elapsed_series(h, 0), [0.0, 1.0, 2.0])

    def test_elapsed_series_empty(self):
        self.assertEqual(_elapsed_series([], 2.0), [])

    def test_elapsed_series_bad_ts_fallback(self):
        h = [{"ts": "not-a-date"}, {"ts": "also-bad"}]
        self.assertEqual(_elapsed_series(h, 2.0), [0.0, 2.0])


class TestSvgExport(unittest.TestCase):
    def setUp(self):
        self.monitor = _full_monitor()
        self.out_dir = tempfile.mkdtemp(prefix="vm_monitor_svg_")

    def tearDown(self):
        for f in os.listdir(self.out_dir):
            try:
                os.unlink(os.path.join(self.out_dir, f))
            except PermissionError:
                pass
        os.rmdir(self.out_dir)

    def test_all_reports_written(self):
        written = export_svg_reports(self.monitor, self.out_dir)
        self.assertEqual(
            sorted(os.path.basename(p) for p in written),
            [
                "disk_io.svg",
                "disk_latency.svg",
                "host_pressure.svg",
                "host_resources.svg",
                "numa.svg",
                "swap.svg",
                "vm_total.svg",
            ],
        )
        for p in written:
            self.assertTrue(os.path.isfile(p))
            self.assertGreater(os.path.getsize(p), 200)

    def test_svg_is_well_formed_and_dark_themed(self):
        export_svg_reports(self.monitor, self.out_dir)
        path = os.path.join(self.out_dir, "disk_io.svg")
        root = ET.parse(path).getroot()
        self.assertEqual(_strip_ns(root.tag), "svg")
        # Dark canvas background rect present.
        rects = [e for e in root.iter() if _strip_ns(e.tag) == "rect"]
        fills = [e.attrib.get("fill") for e in rects]
        self.assertIn("#07101f", fills)
        # At least one polyline curve rendered (2-sample histories).
        polys = [e for e in root.iter() if _strip_ns(e.tag) == "polyline"]
        self.assertGreaterEqual(len(polys), 1)
        # Threshold dashed line on the utilization chart.
        lines = [e for e in root.iter() if _strip_ns(e.tag) == "line"]
        dashed = [e for e in lines if e.attrib.get("stroke-dasharray") == "7 5"]
        self.assertGreaterEqual(len(dashed), 1)

    def test_disk_io_curves_carry_device_data(self):
        export_svg_reports(self.monitor, self.out_dir)
        text = open(os.path.join(self.out_dir, "disk_io.svg"), encoding="utf-8").read()
        # Both target disks appear in legend/labels.
        self.assertIn("sda", text)
        self.assertIn("sdb", text)
        self.assertIn("ublk", text)
        # Utilization threshold label.
        self.assertIn("100%", text)

    def test_host_report_has_dirty_writeback_cached(self):
        export_svg_reports(self.monitor, self.out_dir)
        text = open(os.path.join(self.out_dir, "host_resources.svg"), encoding="utf-8").read()
        self.assertIn("Dirty", text)
        self.assertIn("Writeback", text)
        self.assertIn("Cached", text)
        self.assertIn("Buffers", text)
        # iowait rides on the CPU chart in the resource-baseline file.
        self.assertIn("IOWait", text)
        # Dirty throttle threshold line drawn (dirty_limit_mb=500 set in fixture);
        # it is the only dashed line in host_resources.svg (the util 100% line
        # lives in disk_io.svg).
        self.assertIn("dirty limit", text)
        self.assertGreaterEqual(text.count('stroke-dasharray="7 5"'), 1)
        # Pressure / cache / runstate moved to host_pressure.svg, not here.
        self.assertNotIn("Page-Cache Pressure", text)
        self.assertNotIn("Runnable / Blocked Procs", text)

    def test_host_pressure_report_carries_pressure_charts(self):
        export_svg_reports(self.monitor, self.out_dir)
        text = open(os.path.join(self.out_dir, "host_pressure.svg"), encoding="utf-8").read()
        self.assertIn("Page-Cache Pressure", text)
        self.assertIn("Anonymous / File Cache", text)
        self.assertIn("Runnable / Blocked Procs", text)
        # Dirty threshold stays in host_resources.svg, not here.
        self.assertNotIn("dirty limit", text)

    def test_disk_latency_report_carries_queue_and_await(self):
        export_svg_reports(self.monitor, self.out_dir)
        latency = open(os.path.join(self.out_dir, "disk_latency.svg"), encoding="utf-8").read()
        self.assertIn("Disk Queue Depth", latency)
        self.assertIn("Disk Avg Latency", latency)
        self.assertIn("Queue", latency)
        self.assertIn("R-await", latency)
        self.assertIn("W-await", latency)
        # Throughput charts live in disk_io.svg, not the latency file.
        throughput = open(os.path.join(self.out_dir, "disk_io.svg"), encoding="utf-8").read()
        self.assertNotIn("Disk Queue Depth", throughput)
        self.assertNotIn("Disk Avg Latency", throughput)

    def test_empty_monitor_writes_nothing(self):
        m = DummyMonitor()
        written = export_svg_reports(m, self.out_dir)
        self.assertEqual(written, [])
        self.assertEqual(os.listdir(self.out_dir), [])

    def test_partial_monitor_writes_only_present_reports(self):
        # Only disk history populated -> only disk_io.svg.
        m = DummyMonitor()
        m.interval = 1.0
        m.target_disks = ["sda"]
        m.disk_history = [
            {"ts": "2026-08-14 10:00:00", "disks": {"sda": {"r_mb_s": 1.0, "w_mb_s": 2.0, "util_pct": 1.0}}},
            {"ts": "2026-08-14 10:00:01", "disks": {"sda": {"r_mb_s": 2.0, "w_mb_s": 3.0, "util_pct": 2.0}}},
        ]
        written = export_svg_reports(m, self.out_dir)
        self.assertEqual([os.path.basename(p) for p in written], ["disk_io.svg"])

    def test_single_sample_skips_polyline(self):
        # <polyline> requires >= 2 points; a single-sample history must not
        # produce a polyline (but the file still renders axes/legend).
        m = DummyMonitor()
        m.interval = 1.0
        m.target_disks = ["sda"]
        m.disk_history = [
            {"ts": "2026-08-14 10:00:00", "disks": {"sda": {"r_mb_s": 1.0, "w_mb_s": 2.0, "util_pct": 1.0}}},
        ]
        export_svg_reports(m, self.out_dir)
        root = ET.parse(os.path.join(self.out_dir, "disk_io.svg")).getroot()
        polys = [e for e in root.iter() if _strip_ns(e.tag) == "polyline"]
        self.assertEqual(polys, [])


if __name__ == "__main__":
    unittest.main()
