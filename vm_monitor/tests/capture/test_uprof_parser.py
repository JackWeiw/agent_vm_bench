"""Tests for the uprof (AMDuProfPcm) timeseries parser + Uprof sheet.

The rotation slot passes no -o: AMDuProfPcm streams its counters to stdout,
captured per-turn into log_capture/uprof/<stamp>.log -- the log IS the
collected sample. parse_uprof reads each log as comma-separated tabular data
(tolerating preamble/stderr lines) and flattens every turn's rows with a turn
column (the log's stamp); _build_uprof_sheet renders them as the Uprof sheet
of resource_report.xlsx.
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


def _write_log(log_dir, turn, text):
    path = os.path.join(log_dir, "log_capture", "uprof", f"{turn}.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class TestParseUprof(unittest.TestCase):
    def setUp(self):
        self.log_dir = tempfile.mkdtemp(prefix="uprof_parse_")

    def tearDown(self):
        shutil.rmtree(self.log_dir, ignore_errors=True)

    def test_missing_uprof_dir_is_empty(self):
        self.assertEqual(parse_uprof(self.log_dir), {"turns": [], "columns": [], "rows": []})

    def test_parses_stdout_logs_with_turn_column(self):
        # Layout: each turn's stdout log holds the tabular counters; the turn
        # label is the log's stamp (joinable onto the rotation timeline).
        _write_log(self.log_dir, "20260101-000000", HEADER + "\n" + ROW + "\n")
        _write_log(self.log_dir, "20260101-000015", HEADER + "\n0.0,14.0,6.9,9.5,41.5,49.0\n")
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000", "20260101-000015"])
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["columns"][0], "Time")
        self.assertEqual(parsed["rows"][0]["turn"], "20260101-000000")
        self.assertEqual(parsed["rows"][1]["turn"], "20260101-000015")
        self.assertEqual(float(parsed["rows"][1]["Total Mem Bw (GB/s)"]), 14.0)

    def test_log_with_preamble_and_stderr_interleave(self):
        # stdout+stderr are merged: preamble text before the table and error
        # lines interleaved after it are skipped; the tabular rows survive.
        text = (
            "Starting collection...\n"
            "[WARN] driver not loaded\n"
            + HEADER
            + "\n"
            + ROW
            + "\n"
            + "1.0,13.2,6.4,11.0,39.0,50.0\n"
            + "Invalid output file!! Failed to process args\n"
        )
        _write_log(self.log_dir, "20260101-000000", text)
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], ["20260101-000000"])
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["rows"][1]["turn"], "20260101-000000")
        self.assertEqual(float(parsed["rows"][1]["Total Mem Bw (GB/s)"]), 13.2)

    def test_non_tabular_log_is_skipped(self):
        # Pure diagnostics (no tabular section) yields no rows and no error.
        _write_log(self.log_dir, "20260101-000000", "Starting...\nDone\n")
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], [])
        self.assertEqual(parsed["rows"], [])

    def test_empty_log_is_skipped(self):
        # The lifecycle drops empty logs; parse_uprof tolerates one anyway.
        _write_log(self.log_dir, "20260101-000000", "")
        parsed = parse_uprof(self.log_dir)
        self.assertEqual(parsed["turns"], [])
        self.assertEqual(parsed["rows"], [])

    def test_threaded_rotation_log_dir_beats_the_guess(self):
        # bench-core derives --rotation-log-dir from output_dir, independent of
        # a (possibly custom) monitor.log_dir: the parser must use the threaded
        # root, not the dirname(log_dir) guess.
        out = os.path.join(self.log_dir, "out")
        real_uprof = os.path.join(out, "log_capture", "uprof")
        os.makedirs(real_uprof, exist_ok=True)
        with open(os.path.join(real_uprof, "20260101-000000.log"), "w", encoding="utf-8") as f:
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
