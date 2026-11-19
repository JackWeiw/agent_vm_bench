"""Tests for the uprof (AMDuProfPcm) timeseries parser + Uprof sheet.

The rotation slot passes -O pointing at log_capture/uprof/ and the tool
AUTO-GENERATES timestamped folders inside it per turn (the observed layout:
uprof/<tool timestamp>/AMDuProfPcm-Timeseries_*/report-timerseries.csv).
parse_uprof walks those folders, flattens all *.csv rows with the folder
name as the turn column, and _build_uprof_sheet renders them as the Uprof
sheet of resource_report.xlsx.
"""

import os
import shutil
import tempfile
import unittest

import pandas as pd

from vm_monitor.capture.parsers import parse_uprof
from vm_monitor.export.resource_report import _build_uprof_sheet

HEADER = "Time,Total Mem Bw (GB/s),CCD0 Bw (GB/s),Frontend_Bound(%),Backend_Bound.Memory(%),Retiring(%)"
ROW = "0.0,12.5,6.1,10.0,40.0,50.0"


def _write_turn(log_dir, turn, rows):
    """Observed layout: uprof/<turn>/AMDuProfPcm-Timeseries_0/report-timerseries.csv."""
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

    def test_parses_turn_dirs_with_turn_column(self):
        _write_turn(self.log_dir, "20260101-000000", [[0.0, 12.5, 6.1, 10.0, 40.0, 50.0]])
        _write_turn(self.log_dir, "20260101-000015", [[0.0, 14.0, 6.9, 9.5, 41.5, 49.0]])
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000", "20260101-000015"])
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["columns"][0], "Time")
        self.assertEqual(parsed["rows"][0]["turn"], "20260101-000000")
        self.assertEqual(parsed["rows"][1]["turn"], "20260101-000015")
        self.assertEqual(float(parsed["rows"][1]["Total Mem Bw (GB/s)"]), 14.0)

    def test_csv_directly_in_turn_dir_is_parsed_too(self):
        # Tolerated depth: a CSV sitting directly in the turn dir (no
        # AMDuProfPcm-Timeseries_* nesting) is still picked up.
        turn_dir = os.path.join(self.log_dir, "log_capture", "uprof", "20260101-000000")
        os.makedirs(turn_dir, exist_ok=True)
        with open(os.path.join(turn_dir, "report-timerseries.csv"), "w", encoding="utf-8") as f:
            f.write(HEADER + "\n" + ROW + "\n")
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000"])
        self.assertEqual(len(parsed["rows"]), 1)

    def test_turn_dir_without_csv_is_skipped(self):
        # A turn dir the tool created but never wrote CSVs into (failed turn)
        # yields no rows.
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

    def test_threaded_rotation_log_dir_beats_the_guess(self):
        # bench-core derives --rotation-log-dir from output_dir, independent of
        # a (possibly custom) monitor.log_dir: the parser must use the threaded
        # root, not the dirname(log_dir) guess.
        out = os.path.join(self.log_dir, "out")
        turn_dir = os.path.join(out, "log_capture", "uprof", "20260101-000000")
        os.makedirs(turn_dir, exist_ok=True)
        with open(os.path.join(turn_dir, "report-timerseries.csv"), "w", encoding="utf-8") as f:
            f.write(HEADER + "\n" + ROW + "\n")
        vm_monitor_log_dir = os.path.join(out, "vm_monitor")
        os.makedirs(vm_monitor_log_dir, exist_ok=True)
        parsed = parse_uprof(vm_monitor_log_dir, rotation_log_dir=os.path.join(out, "log_capture"))
        self.assertEqual(parsed["turns"], ["20260101-000000"])
        self.assertEqual(len(parsed["rows"]), 1)


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
