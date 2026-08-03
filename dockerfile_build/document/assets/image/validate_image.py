#!/usr/bin/env python3
"""Lightweight ready-check for the shared PDF/XLSX runtime image."""

import importlib.metadata
import json
import shutil
import sys
from pathlib import Path


ROOT = Path("/opt/document-bench")
REQUIRED_FILES = (
    "pdf/input/of306_aug2023.pdf",
    "pdf/input/synthetic_applicants.json",
    "pdf/input/verify_pdf_batch.py",
    "xlsx/input/monthly_operations_template.xlsx",
    "xlsx/input/prepared_monthly_operations_summary.csv",
    "xlsx/input/prepared_reconciliation_summary.csv",
    "xlsx/input/template_manifest.json",
    "xlsx/input/verify_xlsx_enhanced.py",
    "skills/pdf/LICENSE.txt",
    "skills/pdf/SKILL.md",
    "skills/pdf/forms.md",
    "skills/pdf/reference.md",
    "skills/pdf/scripts/check_fillable_fields.py",
    "skills/pdf/scripts/convert_pdf_to_images.py",
    "skills/pdf/scripts/extract_form_field_info.py",
    "skills/pdf/scripts/fill_fillable_fields.py",
    "skills/xlsx/LICENSE.txt",
    "skills/xlsx/SKILL.md",
    "skills/xlsx/scripts/recalc.py",
    "skills/xlsx/scripts/office/pack.py",
    "skills/xlsx/scripts/office/soffice.py",
    "skills/xlsx/scripts/office/unpack.py",
    "skills/xlsx/scripts/office/validate.py",
    "bin/export_xlsx_csv.py",
    "bin/run_xlsx_helper_atomic.py",
    "bin/write_xlsx_summary.py",
    "requirements/runtime.lock",
)
REQUIRED_COMMANDS = ("python3", "soffice", "pdfinfo", "pdftoppm")


def read_lock(path):
    result = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise ValueError("unlocked requirement: %s" % line)
        name, version = line.split("==", 1)
        result[name.strip()] = version.strip()
    return result


def main():
    missing = [name for name in REQUIRED_FILES if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError("missing document assets: %s" % missing)
    for command in REQUIRED_COMMANDS:
        if not shutil.which(command):
            raise RuntimeError("required command unavailable: %s" % command)
    if (
        not Path("/root/.openclaw/skills/pdf/SKILL.md").is_file()
        or not Path("/root/.openclaw/skills/xlsx/SKILL.md").is_file()
    ):
        raise RuntimeError("OpenClaw skill compatibility path is unavailable")
    for package, expected in read_lock(ROOT / "requirements/runtime.lock").items():
        actual = importlib.metadata.version(package)
        if actual != expected:
            raise RuntimeError(f"{package} version mismatch: {actual} != {expected}")
    manifest = json.loads((ROOT / "xlsx/input/template_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("source_rows") != 2964624 or manifest.get("clean_rows") != 2758427:
        raise RuntimeError("XLSX manifest row constants do not match the benchmark")
    print("document benchmark runtime is ready")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("document image validation failed: %s" % exc, file=sys.stderr)
        raise SystemExit(1) from None
