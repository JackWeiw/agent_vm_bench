#!/usr/bin/env python3
"""Run a recorded XLSX helper without exposing a partially written report."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("helper", type=Path)
    parser.add_argument("report", type=Path)
    return parser.parse_args()


def redirect_report(source: str, temporary_report: Path) -> str:
    """Redirect either recorded helper variant to a same-directory temp file."""
    replacement = repr(str(temporary_report))
    source, main_count = re.subn(
        r'(?m)^SRC\s*=\s*["\'][^"\']*monthly_operations_report\.xlsx["\']\s*$',
        f"SRC = {replacement}",
        source,
        count=1,
    )
    source, repair_count = re.subn(
        r'(?m)^REPORT\s*=\s*OUTPUT\s*/\s*["\']monthly_operations_report\.xlsx["\']\s*$',
        f"REPORT = Path({replacement})",
        source,
        count=1,
    )
    if main_count + repair_count != 1:
        raise ValueError("helper must define exactly one recognized XLSX report target")
    return source


def main() -> int:
    args = parse_args()
    helper = args.helper.resolve(strict=True)
    report = args.report.resolve()
    if helper.suffix != ".py":
        raise ValueError(f"helper is not a Python file: {helper}")
    if not report.is_file():
        raise FileNotFoundError(f"input report does not exist: {report}")

    report.parent.mkdir(parents=True, exist_ok=True)
    report_fd, temporary_name = tempfile.mkstemp(prefix=f".{report.stem}.", suffix=report.suffix, dir=report.parent)
    os.close(report_fd)
    temporary_report = Path(temporary_name)
    temporary_helper: Path | None = None
    try:
        shutil.copy2(report, temporary_report)
        transformed = redirect_report(helper.read_text(encoding="utf-8"), temporary_report)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{helper.stem}.",
            suffix=".py",
            dir=helper.parent,
            delete=False,
        ) as handle:
            handle.write(transformed)
            temporary_helper = Path(handle.name)

        subprocess.run(
            [sys.executable, str(temporary_helper)],
            cwd=helper.parent,
            check=True,
        )
        if not temporary_report.is_file() or temporary_report.stat().st_size < 1024:
            raise RuntimeError("helper did not produce a valid temporary workbook")
        os.replace(temporary_report, report)
        print(f"Atomically published workbook: {report}")
        return 0
    finally:
        if temporary_helper is not None:
            temporary_helper.unlink(missing_ok=True)
        temporary_report.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
