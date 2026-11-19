"""Tests for the uprof (AMDuProfPcm) Timeseries parser + Uprof sheet.

The rotation slot passes -o <log_capture/uprof/<turn>> and AMDuProfPcm builds
AMDuProfPcm-Timeseries_*/report-timerseries.csv inside it. parse_uprof
flattens all turns' CSVs into rows with a turn column and _build_uprof_sheet
renders them as the Uprof sheet of resource_report.xlsx.
"""

import os
import shutil
import tempfile
import unittest

import pandas as pd

from vm_monitor.capture.parsers import parse_uprof
from vm_monitor.export.resource_report import _build_uprof_sheet


def _write_turn(log_dir, turn, rows):
    """Create one turn's AMDuProfPcm-Timeseries output with a representative header."""
    ts_dir = os.path.join(log_dir, "log_capture", "uprof", turn, "AMDuProfPcm-Timeseries_0")
    os.makedirs(ts_dir, exist_ok=True)
    header = "Time,Total Mem Bw (GB/s),CCD0 Bw (GB/s),Frontend_Bound(%),Backend_Bound.Memory(%),Retiring(%)"
    with open(os.path.join(ts_dir, "report-timerseries.csv"), "w", encoding="utf-8") as f:
        f.write(header + "\n")
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

    def test_parses_all_turns_with_turn_column(self):
        _write_turn(
            self.log_dir,
            "20260101-000000",
            [[0.0, 12.5, 6.1, 10.0, 40.0, 50.0], [1.0, 13.2, 6.4, 11.0, 39.0, 50.0]],
        )
        _write_turn(self.log_dir, "20260101-000015", [[0.0, 14.0, 6.9, 9.5, 41.5, 49.0]])
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000", "20260101-000015"])
        self.assertEqual(len(parsed["rows"]), 3)
        self.assertEqual(parsed["columns"][0], "Time")
        self.assertEqual(parsed["rows"][0]["turn"], "20260101-000000")
        self.assertEqual(parsed["rows"][2]["turn"], "20260101-000015")
        self.assertEqual(float(parsed["rows"][1]["Total Mem Bw (GB/s)"]), 13.2)

    def test_turn_without_timeseries_output_is_skipped(self):
        # The turn dir exists but the tool never wrote Timeseries output into
        # it (failed turn) -- no rows, no error.
        os.makedirs(os.path.join(self.log_dir, "log_capture", "uprof", "20260101-000000"), exist_ok=True)
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000"])
        self.assertEqual(parsed["rows"], [])

    def test_flat_csv_next_to_turn_dir_is_not_parsed(self):
        # Only the tool's Timeseries layout is consumed; stray files directly
        # under uprof/ (e.g. older layouts) are left alone.
        os.makedirs(os.path.join(self.log_dir, "log_capture", "uprof"), exist_ok=True)
        with open(os.path.join(self.log_dir, "log_capture", "uprof", "legacy.csv"), "w") as f:
            f.write("Time,X\n0,1\n")
        self.assertEqual(parse_uprof(self.log_dir)["rows"], [])


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
