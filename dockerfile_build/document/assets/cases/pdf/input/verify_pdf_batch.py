#!/usr/bin/env python3
"""Strictly verify the OF-306 synthetic batch-fill benchmark output."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops
from pypdf import PdfReader


SENSITIVE_FIELDS = {
    "Social Security Number",
    "17a Applicants Signature",
    "17b Appointees Signature 1",
    "Date",
    "Date_2",
    "Appointing Officer Enter Date of Appointment or Conversion MM  DD  YYYY",
    "When did you leave your last Federal job",
    "Did you waive Basic Life Insurance or any type of optional life insurance",
    "If yes to 18b did you later cancel the waivers",
}
SHARED_NO_FIELDS = {
    "Have you ever served in the U.S. Military",
    "Have you been convicted imprisoned probation or paroled last 7 years",
    "Have you been court martialed in the last 7 years",
    "Are you currently under charges",
    "Have you been fired or debarred or quit due to a specific problem or quit after being told you would be fired",
    "Are you delinquent on any Federal debt",
    "Do any of your relatives work for the agency or government organization for which you are submitting this form",
    "Have you applied or do you receive retirement or pension benefits from the military Federal or D.C. government",
}


def terminal_field_count(fields: dict[str, Any]) -> int:
    """Count leaf text/signature fields plus button groups exposed to the Skill."""
    leaf_fields = sum(
        not field.get("/Kids") and field.get("/FT") in {"/Tx", "/Sig", "/Ch"}
        for field in fields.values()
    )
    button_groups = sum(
        bool(field.get("/Kids")) and field.get("/FT") == "/Btn"
        for field in fields.values()
    )
    return leaf_fields + button_groups


def parse_args() -> argparse.Namespace:
    """Parse verifier paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template_pdf", type=Path)
    parser.add_argument("applicants_json", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("report_json", type=Path)
    return parser.parse_args()


def add_check(
    checks: dict[str, Any],
    failures: list[str],
    name: str,
    ok: bool,
    detail: Any,
) -> None:
    """Record one verifier check."""
    checks[name] = {"ok": bool(ok), "detail": detail}
    if not ok:
        failures.append(name)


def field_value(fields: dict[str, Any], name: str) -> str:
    """Normalize a pypdf form field value."""
    field = fields.get(name) or {}
    value = field.get("/V")
    if value is None:
        return ""
    return str(value).lstrip("/")


def changed_pixel_count(template_path: Path, output_path: Path) -> int:
    """Count visibly changed grayscale pixels between two rendered pages."""
    with Image.open(template_path).convert("RGB") as template:
        with Image.open(output_path).convert("RGB") as output:
            if template.size != output.size:
                return 0
            difference = ImageChops.difference(template, output).convert("L")
            return sum(value > 12 for value in difference.getdata())


def verify_one_pdf(
    applicant: dict[str, Any],
    output_dir: Path,
    template_page: Path,
) -> tuple[bool, dict[str, Any]]:
    """Verify one filled form, values JSON, and rendered output."""
    applicant_id = str(applicant["id"])
    pdf_path = output_dir / "filled" / f"{applicant_id}.pdf"
    values_path = output_dir / "field_values" / f"{applicant_id}.json"
    render_dir = output_dir / "rendered" / applicant_id
    detail: dict[str, Any] = {
        "pdf": str(pdf_path),
        "values": str(values_path),
        "render_dir": str(render_dir),
    }
    if not pdf_path.is_file() or not values_path.is_file():
        detail["error"] = "missing PDF or field-values JSON"
        return False, detail
    try:
        reader = PdfReader(pdf_path)
        fields = reader.get_fields() or {}
        values = json.loads(values_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        detail["error"] = str(exc)
        return False, detail

    provided_ids = {str(item.get("field_id")) for item in values if isinstance(item, dict)}
    sensitive_values = {name: field_value(fields, name) for name in SENSITIVE_FIELDS}
    expected_radio = {
        "Are you a U.S. Citizen?": "Yes" if applicant["us_citizen"] else "No",
        "Male": "Yes" if applicant["born_male_after_1959"] else "No",
        "Have you registered with Selective Service": (
            "Yes" if applicant["registered_selective_service"] else "No"
        ),
    }
    actual_radio = {name: field_value(fields, name) for name in expected_radio}
    shared_no = {name: field_value(fields, name) for name in SHARED_NO_FIELDS}
    render_pages = sorted(render_dir.glob("page_*.png"))
    changed_pixels = (
        changed_pixel_count(template_page, render_dir / "page_2.png")
        if (render_dir / "page_2.png").is_file()
        else 0
    )
    checks = {
        "pages": len(reader.pages) == 3,
        "file_size": pdf_path.stat().st_size > 100_000,
        "full_name": field_value(fields, "Full Name") == applicant["full_name"],
        "place_of_birth": field_value(
            fields,
            "PLACE OF BIRTH Include city and state or country",
        )
        == applicant["place_of_birth"],
        "date_of_birth": field_value(fields, "DATE OF BIRTH MM  DD  YYYY")
        == applicant["date_of_birth"],
        "day_phone": field_value(fields, "Day") == applicant["day_phone"],
        "radio_answers": actual_radio == expected_radio,
        "shared_no_answers": all(value == "No" for value in shared_no.values()),
        "sensitive_fields_blank": all(not value for value in sensitive_values.values()),
        "sensitive_fields_omitted": not (provided_ids & SENSITIVE_FIELDS),
        "rendered_pages": len(render_pages) == 3
        and all(path.stat().st_size > 10_000 for path in render_pages),
        "visible_render_difference": changed_pixels > 500,
    }
    detail.update(
        {
            "checks": checks,
            "radio_values": actual_radio,
            "shared_no_values": shared_no,
            "sensitive_values": sensitive_values,
            "rendered_page_count": len(render_pages),
            "changed_pixels_page_2": changed_pixels,
        }
    )
    return all(checks.values()), detail


def main() -> int:
    """Run all batch PDF checks and write a machine-readable report."""
    args = parse_args()
    checks: dict[str, Any] = {}
    failures: list[str] = []
    applicants_data = json.loads(args.applicants_json.read_text(encoding="utf-8"))
    applicants = applicants_data["applicants"]
    template_reader = PdfReader(args.template_pdf)
    template_fields = template_reader.get_fields() or {}
    terminal_fields = terminal_field_count(template_fields)
    add_check(
        checks,
        failures,
        "template_form",
        len(template_reader.pages) == 3 and terminal_fields == 38,
        {
            "pages": len(template_reader.pages),
            "raw_pypdf_fields": len(template_fields),
            "terminal_skill_fields": terminal_fields,
        },
    )
    field_info_path = args.output_dir / "form_field_info.json"
    try:
        field_info = json.loads(field_info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        field_info = []
    add_check(
        checks,
        failures,
        "field_inspection_output",
        len(field_info) == 38,
        len(field_info),
    )
    template_renders = sorted((args.output_dir / "rendered" / "template").glob("page_*.png"))
    add_check(
        checks,
        failures,
        "template_render",
        len(template_renders) == 3
        and all(path.stat().st_size > 10_000 for path in template_renders),
        [str(path) for path in template_renders],
    )
    item_details: dict[str, Any] = {}
    passed = 0
    template_page_2 = args.output_dir / "rendered" / "template" / "page_2.png"
    for applicant in applicants:
        ok, detail = verify_one_pdf(applicant, args.output_dir, template_page_2)
        item_details[str(applicant["id"])] = detail
        passed += int(ok)
    add_check(
        checks,
        failures,
        "filled_pdf_batch",
        len(applicants) == 10 and passed == 10,
        {"expected": len(applicants), "passed": passed, "items": item_details},
    )
    batch_summary_path = args.output_dir / "batch_summary.json"
    try:
        batch_summary = json.loads(batch_summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        batch_summary = {}
    add_check(
        checks,
        failures,
        "batch_summary",
        int(batch_summary.get("filled_pdf_count") or 0) == 10
        and int(batch_summary.get("rendered_page_count") or 0) == 33
        and bool(batch_summary.get("ssn_and_signatures_left_blank")),
        batch_summary,
    )
    report = {
        "status": "success" if not failures else "failed",
        "checks": checks,
        "failures": failures,
    }
    args.report_json.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
