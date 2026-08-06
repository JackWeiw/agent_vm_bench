#!/usr/bin/env bash
set -euo pipefail

ASSET_ROOT="${ASSET_ROOT:-/build/assets}"
DOCUMENT_ARCH="${DOCUMENT_ARCH:?DOCUMENT_ARCH must be arm64 or amd64}"
OPM_PDF_URL="${OPM_PDF_URL:-https://www.opm.gov/media/dxrbwvmb/declaration-for-federal-employment-optional-form-august-2023.pdf}"
TLC_PARQUET_URL="${TLC_PARQUET_URL:-https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet}"
TLC_ZONE_URL="${TLC_ZONE_URL:-https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv}"
ANTHROPIC_SKILLS_REPO="${ANTHROPIC_SKILLS_REPO:-https://github.com/anthropics/skills.git}"
ANTHROPIC_SKILLS_COMMIT="${ANTHROPIC_SKILLS_COMMIT:-9d2f1ae187231d8199c64b5b762e1bdf2244733d}"

download() {
    local url="$1" target="$2" temporary="${2}.download"
    rm -f "${temporary}"
    curl -kL --fail --connect-timeout 30 --retry 5 --retry-delay 3 \
        --retry-all-errors -o "${temporary}" "${url}"
    test -s "${temporary}"
    mv "${temporary}" "${target}"
}

ensure_wheels() {
    local lock_file="$1" wheel_dir="$2" requirement package version normalized
    mkdir -p "${wheel_dir}"
    while IFS= read -r requirement; do
        requirement="${requirement%%#*}"
        requirement="${requirement//[[:space:]]/}"
        test -z "${requirement}" && continue
        package="${requirement%%==*}"
        version="${requirement#*==}"
        normalized="${package//-/_}"
        if ! find "${wheel_dir}" -maxdepth 1 -type f \
            \( -iname "${package}-${version}-*.whl" -o -iname "${normalized}-${version}-*.whl" \) \
            -print -quit | grep -q .; then
            python3 -m pip download --dest "${wheel_dir}" --only-binary=:all: --no-deps \
                --retries 10 --timeout 120 --trusted-host pypi.org \
                --trusted-host files.pythonhosted.org "${requirement}"
        fi
    done < "${lock_file}"
}

validate_pdf() {
    local pdf="$1" text_file
    test -s "${pdf}"
    pdfinfo "${pdf}" | grep -Eq '^Pages:[[:space:]]+3$'
    text_file="$(mktemp)"
    if ! pdftotext "${pdf}" "${text_file}"; then
        rm -f "${text_file}"
        return 1
    fi
    if grep -qi 'Optional Form 306' "${text_file}"; then
        rm -f "${text_file}"
    else
        rm -f "${text_file}"
        return 1
    fi
}

validate_xlsx() {
    python3 - "${ASSET_ROOT}/cases/xlsx/input" <<'PY'
import json
import sys
import zipfile
from pathlib import Path
from openpyxl import load_workbook

root = Path(sys.argv[1])
book = root / "monthly_operations_template.xlsx"
required = [
    root / "prepared_monthly_operations_summary.csv",
    root / "prepared_reconciliation_summary.csv",
    root / "template_manifest.json",
    root / "verify_xlsx_enhanced.py",
]
if not book.is_file() or not zipfile.is_zipfile(str(book)) or any(not p.is_file() or p.stat().st_size == 0 for p in required):
    raise SystemExit("missing or invalid XLSX benchmark output")
manifest = json.loads((root / "template_manifest.json").read_text(encoding="utf-8"))
expected = {"Raw_Sample", "Hourly_Summary", "Daily_Summary", "Zone_Summary", "Payment_Summary", "Fare_Distance_Bands", "Reconciliation"}
wb = load_workbook(str(book), read_only=True, data_only=False)
if not expected.issubset(set(wb.sheetnames)):
    raise SystemExit("XLSX workbook is missing expected sheets")
constants = {
    "source_rows": 2964624,
    "clean_rows": 2758427,
    "sample_rows": 100000,
    "expected_clean_total_fare": 50744748.60,
    "expected_clean_total_amount": 75600521.40,
}
for key, expected_value in constants.items():
    if manifest.get(key) != expected_value:
        raise SystemExit("XLSX manifest mismatch for %s" % key)
compile((root / "verify_xlsx_enhanced.py").read_text(encoding="utf-8"), "verify_xlsx_enhanced.py", "exec")
PY
}

BUILD_WHEELS="${ASSET_ROOT}/wheels/${DOCUMENT_ARCH}"
for required in \
    "${ASSET_ROOT}/cases/pdf/input/synthetic_applicants.json" \
    "${ASSET_ROOT}/cases/pdf/input/verify_pdf_batch.py" \
    "${ASSET_ROOT}/cases/xlsx/input/verify_xlsx_enhanced.py" \
    "${ASSET_ROOT}/scripts/build_template_xlsx_v4.py" \
    "${ASSET_ROOT}/image/export_xlsx_csv.py" \
    "${ASSET_ROOT}/image/run_xlsx_helper_atomic.py" \
    "${ASSET_ROOT}/image/write_xlsx_summary.py" \
    "${ASSET_ROOT}/image/validate_image.py"; do
    test -s "${required}" || { echo "required local benchmark asset missing: ${required}" >&2; exit 1; }
done
ensure_wheels "${ASSET_ROOT}/requirements/build.lock" "${BUILD_WHEELS}"
python3 -m pip install --break-system-packages --no-deps --no-index \
    --find-links="${BUILD_WHEELS}" -r "${ASSET_ROOT}/requirements/build.lock"
ensure_wheels "${ASSET_ROOT}/requirements/runtime.lock" "${BUILD_WHEELS}"

PDF_FILE="${ASSET_ROOT}/cases/pdf/input/of306_aug2023.pdf"
if test -e "${PDF_FILE}"; then
    validate_pdf "${PDF_FILE}"
else
    mkdir -p "$(dirname "${PDF_FILE}")"
    download "${OPM_PDF_URL}" "${PDF_FILE}"
    validate_pdf "${PDF_FILE}"
fi

SKILL_REQUIRED_FILES=(
    "pdf/LICENSE.txt"
    "pdf/SKILL.md"
    "pdf/forms.md"
    "pdf/reference.md"
    "pdf/scripts/check_fillable_fields.py"
    "pdf/scripts/convert_pdf_to_images.py"
    "pdf/scripts/extract_form_field_info.py"
    "pdf/scripts/fill_fillable_fields.py"
    "xlsx/LICENSE.txt"
    "xlsx/SKILL.md"
    "xlsx/scripts/recalc.py"
    "xlsx/scripts/office/pack.py"
    "xlsx/scripts/office/soffice.py"
    "xlsx/scripts/office/unpack.py"
    "xlsx/scripts/office/validate.py"
)
missing_skills=()
for required in "${SKILL_REQUIRED_FILES[@]}"; do
    test -s "${ASSET_ROOT}/skills/${required}" || missing_skills+=("${required}")
done

if [ "${#missing_skills[@]}" -eq 0 ]; then
    : # Complete local skills are used as-is.
elif find "${ASSET_ROOT}/skills" -mindepth 1 -type f -print -quit 2>/dev/null | grep -q .; then
    echo "local skills are present but incomplete; missing required files:" >&2
    printf '  %s\n' "${missing_skills[@]}" >&2
    exit 1
else
    skills_checkout="$(mktemp -d)"
    git -c http.sslVerify=false -C "${skills_checkout}" init
    git -c http.sslVerify=false -C "${skills_checkout}" remote add origin "${ANTHROPIC_SKILLS_REPO}"
    git -c http.sslVerify=false -C "${skills_checkout}" fetch --depth 1 origin "${ANTHROPIC_SKILLS_COMMIT}"
    git -C "${skills_checkout}" checkout --detach FETCH_HEAD
    mkdir -p "${ASSET_ROOT}/skills"
    cp -R "${skills_checkout}/skills/pdf" "${skills_checkout}/skills/xlsx" "${ASSET_ROOT}/skills/"
    rm -rf "${skills_checkout}"
fi

missing_skills=()
for required in "${SKILL_REQUIRED_FILES[@]}"; do
    test -s "${ASSET_ROOT}/skills/${required}" || missing_skills+=("${required}")
done
if [ "${#missing_skills[@]}" -ne 0 ]; then
    echo "required skill download is incomplete; missing files:" >&2
    printf '  %s\n' "${missing_skills[@]}" >&2
    exit 1
fi
grep -q '^#' "${ASSET_ROOT}/skills/pdf/SKILL.md"
grep -q '^#' "${ASSET_ROOT}/skills/xlsx/SKILL.md"

XLSX_DIR="${ASSET_ROOT}/cases/xlsx/input"
if test -e "${XLSX_DIR}/monthly_operations_template.xlsx"; then
    validate_xlsx
else
    RAW_DIR="${ASSET_ROOT}/downloads/xlsx"
    mkdir -p "${RAW_DIR}" "${XLSX_DIR}"
    test -e "${RAW_DIR}/yellow_tripdata_2024-01.parquet" || download "${TLC_PARQUET_URL}" "${RAW_DIR}/yellow_tripdata_2024-01.parquet"
    test -e "${RAW_DIR}/taxi_zone_lookup.csv" || download "${TLC_ZONE_URL}" "${RAW_DIR}/taxi_zone_lookup.csv"
    python3 - "${RAW_DIR}" <<'PY'
import sys
from pathlib import Path
import pandas as pd
root = Path(sys.argv[1])
parquet = pd.read_parquet(str(root / "yellow_tripdata_2024-01.parquet"))
zones = pd.read_csv(str(root / "taxi_zone_lookup.csv"))
required_trip = {"VendorID", "tpep_pickup_datetime", "tpep_dropoff_datetime", "fare_amount", "total_amount", "PULocationID", "DOLocationID"}
required_zone = {"LocationID", "Borough", "Zone", "service_zone"}
if len(parquet) != 2964624 or not required_trip.issubset(parquet.columns) or not required_zone.issubset(zones.columns):
    raise SystemExit("TLC source data does not match the pinned January 2024 dataset")
PY
    python3 "${ASSET_ROOT}/scripts/build_template_xlsx_v4.py" --input-dir "${RAW_DIR}" --output-dir "${XLSX_DIR}"
    validate_xlsx
fi

python3 -m compileall -q "${ASSET_ROOT}/image" "${ASSET_ROOT}/skills" \
    "${ASSET_ROOT}/cases/pdf/input" "${ASSET_ROOT}/cases/xlsx/input"
find "${ASSET_ROOT}" -type d -name __pycache__ -prune -exec rm -rf '{}' +
echo "document assets prepared for ${DOCUMENT_ARCH}"
