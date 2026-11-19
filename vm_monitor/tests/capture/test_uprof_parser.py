"""Tests for the uprof (AMDuProfPcm) timeseries parser + Uprof sheet.

The rotation slot passes -o <log_capture/uprof/<stamp>.csv> and the tool
writes the timeseries CSV exactly there (file form). parse_uprof also
tolerates the older nested layout (uprof/<turn>/AMDuProfPcm-Timeseries_*/)
and flattens every turn's rows with a turn column; _build_uprof_sheet renders
them as the Uprof sheet of resource_report.xlsx.
"""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import pandas as pd

from vm_monitor.capture.log_capture import LogCapture
from vm_monitor.capture.parsers import parse_uprof
from vm_monitor.export.resource_report import _build_uprof_sheet

HEADER = "Time,Total Mem Bw (GB/s),CCD0 Bw (GB/s),Frontend_Bound(%),Backend_Bound.Memory(%),Retiring(%)"


def _write_flat_csv(log_dir, turn, rows):
    """Layout A: the tool wrote uprof/<turn>.csv directly."""
    path = os.path.join(log_dir, "log_capture", "uprof", f"{turn}.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(HEADER + "\n")
        for row in rows:
            f.write(",".join(str(x) for x in row) + "\n")
    return path


def _write_nested_turn(log_dir, turn, rows):
    """Layout B: the tool wrote uprof/<turn>/AMDuProfPcm-Timeseries_0/*.csv."""
    ts_dir = os.path.join(log_dir, "log_capture", "uprof", turn, "AMDuProfPcm-Timeseries_0")
    os.makedirs(ts_dir, exist_ok=True)
    with open(os.path.join(ts_dir, "report-timerseries.csv"), "w", encoding="utf-8") as f:
        f.write(HEADER + "\n")
        for row in rows:
            f.write(",".join(str(x) for x in row) + "\n")
    return ts_dir


class TestParseUprof(unittest.TestCase):
    def setUp(self):
        self.log_dir = tempfile.mkdtemp(prefix="uprof_parse_")

    def tearDown(self):
        shutil.rmtree(self.log_dir, ignore_errors=True)

    def test_missing_uprof_dir_is_empty(self):
        self.assertEqual(parse_uprof(self.log_dir), {"turns": [], "columns": [], "rows": []})

    def test_parses_flat_files_with_turn_column(self):
        # Layout A (current tool behavior): uprof/<turn>.csv files.
        _write_flat_csv(self.log_dir, "20260101-000000", [[0.0, 12.5, 6.1, 10.0, 40.0, 50.0]])
        _write_flat_csv(self.log_dir, "20260101-000015", [[0.0, 14.0, 6.9, 9.5, 41.5, 49.0]])
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000", "20260101-000015"])
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["columns"][0], "Time")
        self.assertEqual(parsed["rows"][0]["turn"], "20260101-000000")
        self.assertEqual(parsed["rows"][1]["turn"], "20260101-000015")
        self.assertEqual(float(parsed["rows"][1]["Total Mem Bw (GB/s)"]), 14.0)

    def test_parses_nested_turns(self):
        # Layout B (tolerated): uprof/<turn>/AMDuProfPcm-Timeseries_*/ CSVs.
        _write_nested_turn(
            self.log_dir, "20260101-000000", [[0.0, 12.5, 6.1, 10.0, 40.0, 50.0], [1.0, 13.2, 6.4, 11.0, 39.0, 50.0]]
        )
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000"])
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["rows"][1]["turn"], "20260101-000000")
        self.assertEqual(float(parsed["rows"][1]["Total Mem Bw (GB/s)"]), 13.2)

    def test_both_layouts_coexist(self):
        _write_flat_csv(self.log_dir, "20260101-000000", [[0.0, 12.5, 6.1, 10.0, 40.0, 50.0]])
        _write_nested_turn(self.log_dir, "20260101-000015", [[0.0, 14.0, 6.9, 9.5, 41.5, 49.0]])
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000", "20260101-000015"])
        self.assertEqual(len(parsed["rows"]), 2)

    def test_turn_dir_without_timeseries_output_is_skipped(self):
        # A turn DIRECTORY without the Timeseries output (failed nested-form
        # turn) yields no rows.
        os.makedirs(os.path.join(self.log_dir, "log_capture", "uprof", "20260101-000000"), exist_ok=True)
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], [])
        self.assertEqual(parsed["rows"], [])

    def test_non_csv_files_are_ignored(self):
        uprof_dir = os.path.join(self.log_dir, "log_capture", "uprof")
        os.makedirs(uprof_dir, exist_ok=True)
        with open(os.path.join(uprof_dir, "20260101-000000.log"), "w") as f:
            f.write("not csv\n")
        self.assertEqual(parse_uprof(self.log_dir)["rows"], [])

    def test_sibling_rotation_root_parsed_for_bench_core_layout(self):
        # bench-core passes --rotation-log-dir <output_dir>/log_capture, a
        # SIBLING of vm-monitor's own log_dir (<output_dir>/vm_monitor): the
        # parser must find the rotation root there too, or the bench-core path
        # would silently never populate the sheet.
        out = os.path.join(self.log_dir, "out")
        uprof_dir = os.path.join(out, "log_capture", "uprof")
        os.makedirs(uprof_dir, exist_ok=True)
        with open(os.path.join(uprof_dir, "20260101-000000.csv"), "w", encoding="utf-8") as f:
            f.write("Time,Total Mem Bw (GB/s)\n0.0,12.5\n")
        vm_monitor_log_dir = os.path.join(out, "vm_monitor")
        os.makedirs(vm_monitor_log_dir, exist_ok=True)
        parsed = parse_uprof(vm_monitor_log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000"])
        self.assertEqual(len(parsed["rows"]), 1)
        self.assertEqual(float(parsed["rows"][0]["Total Mem Bw (GB/s)"]), 12.5)

    def test_relative_log_dir_yields_absolute_o_arg(self):
        # A relative log_dir + Popen(cwd=rotation_log_dir) used to make the
        # child resolve the still-relative -o one level wrong ("Invalid output
        # file!!") -- the turn must pass an absolute -o path ending in .csv.
        rel_dir = "rel_uprof_logs"
        os.makedirs(rel_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, rel_dir, ignore_errors=True)
        old_cwd = os.getcwd()
        os.chdir(self.log_dir)
        try:
            config = {"uprof_path": "/fake/AMDuProfPcm"}
            cap = LogCapture(config, 30, rel_dir, [0], log_rotation=True)
            spawned = []

            def fake_popen(cmd, *a, **k):
                spawned.append(cmd)

                class _Dummy:
                    returncode = 0

                    def poll(self):  # noqa: N802 - proc seam
                        return 0

                    def wait(self, timeout=None):  # noqa: N802 - proc seam
                        return 0

                return _Dummy()

            expected_dirname = os.path.join(os.path.abspath(rel_dir), "log_capture", "uprof")
            with (
                patch("vm_monitor.capture.log_capture.subprocess.Popen", side_effect=fake_popen),
                redirect_stdout(io.StringIO()),
            ):
                cap._rotation_uprof_turn()
        finally:
            os.chdir(old_cwd)
        data_file = spawned[0][spawned[0].index("-o") + 1]
        self.assertTrue(os.path.isabs(data_file))
        self.assertTrue(data_file.endswith(".csv"))
        self.assertEqual(os.path.dirname(data_file), expected_dirname)


class TestBuildUprofSheet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uprof_sheet_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self):
        return [
            {"turn": "20260101-000000", "Time": "0.0", "Total Mem Bw (GB/s)": "12.5", "Retiring(%)": "50.0"},
            {"turn": "20260101-000000", "Time": "1.0", "Total Mem Bw (GB/s)": "13.2", "Retiring(%)": "50.0"},
        ]

    def test_sheet_written_with_numeric_coercion(self):
        xlsx = os.path.join(self.tmp, "r.xlsx")
        with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
            _build_uprof_sheet(writer, {"uprof": {"turns": ["20260101-000000"], "columns": [], "rows": self._rows()}})
        df = pd.read_excel(xlsx, sheet_name="Uprof")
        self.assertEqual(len(df), 2)
        self.assertEqual(df["turn"].iloc[0], "20260101-000000")
        self.assertEqual(float(df["Total Mem Bw (GB/s)"].iloc[0]), 12.5)
        # All-numeric columns (Time) are coerced to numbers, not kept as text.
        self.assertEqual(float(df["Time"].iloc[1]), 1.0)

    def test_empty_parsed_result_skips_sheet(self):
        xlsx = os.path.join(self.tmp, "r.xlsx")
        with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
            pd.DataFrame({"seed": [1]}).to_excel(writer, sheet_name="Seed", index=False)
            _build_uprof_sheet(writer, {})
            _build_uprof_sheet(writer, {"uprof": {"turns": [], "columns": [], "rows": []}})
        # The empty calls wrote nothing: only the seed sheet is present.
        self.assertEqual(set(pd.read_excel(xlsx, sheet_name=None).keys()), {"Seed"})


if __name__ == "__main__":
    unittest.main()
