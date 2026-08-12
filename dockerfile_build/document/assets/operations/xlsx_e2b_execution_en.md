# XLSX Scenario: Call-by-call E2B Execution

This document maps every call in `xlsx_key_operations.json`. The JSON file is the sole executable definition; this English document is explanatory only.

## Executor semantics

Each round copies `/opt/document-bench/xlsx` into the fixed workspace, then runs P01 → P04. A failed or timed-out call stops the round.

- `read`: runs `test -f <path> && head -c 65536 <path> >/dev/null`; this only checks existence and readability.
- `write`: creates the parent directory, then calls `sbx.files.write(path, content, user="root", request_timeout=...)`.
- `exec`: calls `sbx.commands.run(command, timeout=..., user="root")`.

## XLSX-P01-inspect_prepare: Inspect and prepare

Key operations:

- `XLSX-K01-read_requirements`: Read the XLSX skill and inspect the verifier
- `XLSX-K03-copy_template`: Copy the workbook template
- `XLSX-K02-inspect_workbook`: Inspect workbook structure, formulas, charts, and styles

Call-to-key-operation mapping:

| Calls | Key operation |
| --- | --- |
| 1–2 | `XLSX-K01-read_requirements` |
| 3 | `XLSX-K03-copy_template` |
| 4–5 | `XLSX-K02-inspect_workbook` |

### Call 1/5: `read`

Recipe target path:

```text
/root/.openclaw/skills/xlsx/SKILL.md
```

Actual E2B command:

```bash
test -f /root/.openclaw/skills/xlsx/SKILL.md && head -c 65536 /root/.openclaw/skills/xlsx/SKILL.md >/dev/null
```

### Call 2/5: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cat /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01/input/verify_xlsx_enhanced.py
```

### Call 3/5: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cp /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01/input/monthly_operations_template.xlsx /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01/output/monthly_operations_report.xlsx
```

### Call 4/5: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && python3 -c "
from openpyxl import load_workbook
wb = load_workbook('input/monthly_operations_template.xlsx', data_only=False)
print('Sheets:', wb.sheetnames)
for s in wb.sheetnames:
    ws = wb[s]
    print(f'\n=== {s} ===')
    print(f'  Rows: {ws.max_row}, Cols: {ws.max_column}')
    print(f'  Freeze: {ws.freeze_panes}')
    print(f'  Charts: {len(ws._charts)}')
    print(f'  Cond fmt: {len(ws.conditional_formatting)}')
    # Show first 15 rows
    for r in range(1, min(ws.max_row+1, 25)):
        row_vals = []
        for c in range(1, min(ws.max_column+1, 12)):
            cell = ws.cell(row=r, column=c)
            v = cell.value
            if v is not None:
                row_vals.append(f'{cell.coordinate}={v}')
        if row_vals:
            print(f'  Row {r}: {row_vals}')
wb.close()
"

```

### Call 5/5: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && python3 << 'PYEOF'
from openpyxl import load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference

wb = load_workbook('output/monthly_operations_report.xlsx', data_only=False)

# Check charts
for s in wb.sheetnames:
    ws = wb[s]
    charts = ws._charts
    if charts:
        print(f"\n=== {s} charts ({len(charts)}) ===")
        for i, ch in enumerate(charts):
            print(f"  Chart {i}: type={ch.__class__.__name__}, title={ch.title}")

# Check existing cell formatting in key cells
ws_r = wb['Reconciliation']
for cell_ref in ['B10','B14','B5','B24','B25']:
    c = ws_r[cell_ref]
    print(f"\nReconciliation!{cell_ref}: font={c.font.name}/{c.font.size}/{c.font.color}, fill={c.fill}, numfmt={c.number_format}")

ws_h = wb['Hourly_Summary']
for cell_ref in ['B34','B29']:
    c = ws_h[cell_ref]
    print(f"Hourly_Summary!{cell_ref}: font={c.font.name}/{c.font.size}/{c.font.color}, fill={c.fill}, numfmt={c.number_format}")

# Raw_Sample dimensions
ws_raw = wb['Raw_Sample']
print(f"\nRaw_Sample: rows={ws_raw.max_row}, cols={ws_raw.max_column}")
# First row headers
for c in range(1, min(ws_raw.max_column+1, 10)):
    v = ws_raw.cell(row=1, column=c).value
    if v:
        print(f"  Col {c}={v}")

# Check if there are external links
print(f"\nExternal links: {len(wb._external_links)}")
print(f"Defined names: {wb.defined_names}")

# Check data validations across all sheets
for s in wb.sheetnames:
    ws = wb[s]
    if ws.data_validations and ws.data_validations.dataValidation:
        print(f"\nData validations in {s}:")
        for dv in ws.data_validations.dataValidation:
            print(f"  sqref={dv.sqref}, type={dv.type}, formula1={dv.formula1}")

# Check conditional formatting across all sheets
for s in wb.sheetnames:
    ws = wb[s]
    if ws.conditional_formatting:
        print(f"\nConditional formatting in {s}:")
        for cf in ws.conditional_formatting:
            print(f"  range={cf}, rules={len(cf.rules)}")

wb.close()
PYEOF

```

Expected phase outputs:

- `output/monthly_operations_report.xlsx`

## XLSX-P02-build: Build the enhanced workbook

Key operations:

- `XLSX-K04-write_enhancement_helper`: Write the workbook-enhancement helper
- `XLSX-K05-enhance_workbook`: Run workbook enhancement atomically

Call 1 maps to `XLSX-K04-write_enhancement_helper`; call 2 maps to `XLSX-K05-enhance_workbook`.

### Call 1/2: `write`

Write target:

```text
/root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01/enhance_workbook.py
```

Actual E2B API action:

```python
sbx.commands.run("mkdir -p /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01", timeout=..., user="root")
sbx.files.write("/root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01/enhance_workbook.py", arguments["content"], user="root", request_timeout=...)
```

The payload is this call's `tool_call.arguments.content`; its first non-empty line is `#!/usr/bin/env python3`. The full payload remains in JSON to prevent documentation from drifting away from the executable definition.

Complete write payload:

```text
#!/usr/bin/env python3
"""Enhance NYC TLC workbook with Executive_Summary sheet, KPI formulas, charts, formatting."""
from copy import copy
from openpyxl import load_workbook
from openpyxl.chart import LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.series import DataPoint
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

SRC = "/root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01/output/monthly_operations_report.xlsx"
wb = load_workbook(SRC)

# ── 1. Insert Executive_Summary as first sheet ──
ws = wb.create_sheet("Executive_Summary", 0)

# ── Style constants ──
FONT_TITLE = Font(name="Arial", size=16, bold=True, color="FFFFFF")
FONT_SECTION = Font(name="Arial", size=12, bold=True, color="FFFFFF")
FONT_HEADER = Font(name="Arial", size=10, bold=True, color="FFFFFF")
FONT_LABEL = Font(name="Arial", size=10, bold=True)
FONT_BLUE = Font(name="Arial", size=10, color="0000FF")      # hardcoded inputs
FONT_GREEN = Font(name="Arial", size=10, color="008000")      # cross-sheet formulas
FONT_BLACK = Font(name="Arial", size=10, color="000000")      # local formulas
FILL_TITLE = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
FILL_BLUE_HEADER = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
FILL_LIGHT = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")
FILL_WHITE = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
ALIGN_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
ALIGN_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)

# Dimensions
ws.column_dimensions["A"].width = 30
for col_letter in ["B","C","D","E","F"]:
    ws.column_dimensions[col_letter].width = 20

# ── Row 1: Master Title ──
ws.merge_cells("A1:F1")
ws["A1"] = "NYC Taxi Operations — Executive Summary (January 2024)"
ws["A1"].font = FONT_TITLE
ws["A1"].fill = FILL_TITLE
ws["A1"].alignment = ALIGN_CENTER
ws.row_dimensions[1].height = 36

# ── Row 3: KPI section header ──
ws.merge_cells("A3:F3")
ws["A3"] = "Key Performance Indicators"
ws["A3"].font = FONT_SECTION
ws["A3"].fill = FILL_BLUE_HEADER
ws["A3"].alignment = ALIGN_CENTER
ws.row_dimensions[3].height = 28

# ── Row 4: KPI headers ──
kpi_headers = [("A4", "Metric"), ("B4", "Value")]
for ref, val in kpi_headers:
    c = ws[ref]
    c.value = val
    c.font = FONT_HEADER
    c.fill = FILL_BLUE_HEADER
    c.alignment = ALIGN_CENTER
    c.border = THIN_BORDER

# ── Row 5-9: KPI rows ──
kpi_rows = [
    ("B5", "Total Trips", '=Reconciliation!B10', FONT_GREEN, '#,##0'),
    ("B6", "Total Fare Revenue", '=Reconciliation!B14', FONT_GREEN, '$#,##0.00'),
    ("B7", "Overall Average Fare", '=Hourly_Summary!B34', FONT_GREEN, '$#,##0.00'),
    ("B8", "Removal Rate", '=Reconciliation!B5', FONT_GREEN, '0.0%'),
    ("B9", "Reconciliation Status",
     '=IF(AND(Reconciliation!B24="YES - Consistent",Reconciliation!B25="YES - Consistent"),"PASS","REVIEW")',
     FONT_BLACK, '@'),
]

row_num = 5
for ref, label, formula, font, nfmt in kpi_rows:
    ws.cell(row=row_num, column=1, value=label).font = FONT_LABEL
    ws.cell(row=row_num, column=1).alignment = ALIGN_LEFT
    ws.cell(row=row_num, column=1).border = THIN_BORDER
    ws.cell(row=row_num, column=1).fill = FILL_LIGHT if row_num % 2 == 0 else FILL_WHITE

    c = ws[ref]
    c.value = formula
    c.font = font
    c.number_format = nfmt
    c.alignment = ALIGN_CENTER
    c.border = THIN_BORDER
    c.fill = FILL_LIGHT if row_num % 2 == 0 else FILL_WHITE
    row_num += 1

# ── Row 11: Scenario section header ──
ws.merge_cells("A11:F11")
ws["A11"] = "Scenario Analysis"
ws["A11"].font = FONT_SECTION
ws["A11"].fill = FILL_BLUE_HEADER
ws["A11"].alignment = ALIGN_CENTER
ws.row_dimensions[11].height = 28

# ── Row 12: Scenario selector ──
ws.cell(row=12, column=1, value="Selected Scenario:").font = FONT_LABEL
ws.cell(row=12, column=1).alignment = ALIGN_LEFT
ws.cell(row=12, column=1).border = THIN_BORDER
ws.cell(row=12, column=1).fill = FILL_LIGHT

ws["B12"] = "Base"
ws["B12"].font = FONT_BLUE
ws["B12"].alignment = ALIGN_CENTER
ws["B12"].border = THIN_BORDER
ws["B12"].fill = FILL_WHITE

dv = DataValidation(type="list", formula1='"Base,Upside,Downside"', allow_blank=False)
dv.sqref = "B12"
dv.prompt = "Select a scenario"
dv.promptTitle = "Scenario Selector"
ws.add_data_validation(dv)

ws["B12"].comment = Comment("Scenario selector — choose Base, Upside, or Downside to see projected impacts.", "Analyst")

# ── Row 12 headers for scenario table (D-F) ──
scenario_headers = [("D12", "Scenario"), ("E12", "Fare Mult"), ("F12", "Trip Mult")]
for ref, val in scenario_headers:
    c = ws[ref]
    c.value = val
    c.font = FONT_HEADER
    c.fill = FILL_BLUE_HEADER
    c.alignment = ALIGN_CENTER
    c.border = THIN_BORDER

# ── Row 13-15: Scenario table ──
scenario_data = [
    ("D13", "Base", "E13", 1.00, "F13", 1.00),
    ("D14", "Upside", "E14", 1.08, "F14", 1.05),
    ("D15", "Downside", "E15", 0.93, "F15", 0.95),
]
for row_idx, (d_ref, scenario, e_ref, fare_mult, f_ref, trip_mult) in enumerate(scenario_data, start=13):
    dc = ws[d_ref]
    dc.value = scenario
    dc.font = FONT_BLUE
    dc.alignment = ALIGN_CENTER
    dc.border = THIN_BORDER
    dc.fill = FILL_WHITE

    ec = ws[e_ref]
    ec.value = fare_mult
    ec.font = FONT_BLUE
    ec.number_format = '0.00'
    ec.alignment = ALIGN_CENTER
    ec.border = THIN_BORDER
    ec.fill = FILL_WHITE

    fc = ws[f_ref]
    fc.value = trip_mult
    fc.font = FONT_BLUE
    fc.number_format = '0.00'
    fc.alignment = ALIGN_CENTER
    fc.border = THIN_BORDER
    fc.fill = FILL_WHITE

ws["D13"].comment = Comment("Scenario assumptions: fare and trip volume multipliers relative to base. Source: analyst estimates.", "Analyst")

# ── B13: Selected fare multiplier ──
ws["B13"] = '=INDEX(E13:E15,MATCH(B12,D13:D15,0))'
ws["B13"].font = FONT_BLACK
ws["B13"].number_format = '0.00'
ws["B13"].alignment = ALIGN_CENTER
ws["B13"].border = THIN_BORDER
ws["B13"].fill = FILL_WHITE
ws.cell(row=13, column=1, value="Selected Fare Mult:").font = FONT_LABEL
ws.cell(row=13, column=1).alignment = ALIGN_LEFT
ws.cell(row=13, column=1).border = THIN_BORDER
ws.cell(row=13, column=1).fill = FILL_LIGHT

# ── B14: Selected trip multiplier ──
ws["B14"] = '=INDEX(F13:F15,MATCH(B12,D13:D15,0))'
ws["B14"].font = FONT_BLACK
ws["B14"].number_format = '0.00'
ws["B14"].alignment = ALIGN_CENTER
ws["B14"].border = THIN_BORDER
ws["B14"].fill = FILL_WHITE
ws.cell(row=14, column=1, value="Selected Trip Mult:").font = FONT_LABEL
ws.cell(row=14, column=1).alignment = ALIGN_LEFT
ws.cell(row=14, column=1).border = THIN_BORDER
ws.cell(row=14, column=1).fill = FILL_LIGHT

# ── B15: Projected Trips ──
ws["B15"] = '=B5*B14'
ws["B15"].font = FONT_BLACK
ws["B15"].number_format = '#,##0'
ws["B15"].alignment = ALIGN_CENTER
ws["B15"].border = THIN_BORDER
ws["B15"].fill = FILL_WHITE
ws.cell(row=15, column=1, value="Projected Trips:").font = FONT_LABEL
ws.cell(row=15, column=1).alignment = ALIGN_LEFT
ws.cell(row=15, column=1).border = THIN_BORDER
ws.cell(row=15, column=1).fill = FILL_LIGHT

# ── B16: Projected Fare Revenue ──
ws["B16"] = '=B6*B13*B14'
ws["B16"].font = FONT_BLACK
ws["B16"].number_format = '$#,##0.00'
ws["B16"].alignment = ALIGN_CENTER
ws["B16"].border = THIN_BORDER
ws["B16"].fill = FILL_WHITE
ws.cell(row=16, column=1, value="Projected Fare Revenue:").font = FONT_LABEL
ws.cell(row=16, column=1).alignment = ALIGN_LEFT
ws.cell(row=16, column=1).border = THIN_BORDER
ws.cell(row=16, column=1).fill = FILL_LIGHT

# Mark cell B5 comment with source
ws["B5"].comment = Comment("Source: Reconciliation sheet via Hourly_Summary aggregated totals.", "Analyst")

# ── Conditional Formatting ──
# Rule 1: Highlight B9 (Reconciliation Status) red if it says REVIEW
red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
red_font = Font(color="9C0006")
ws.conditional_formatting.add("B9",
    CellIsRule(operator="equal", formula=['"REVIEW"'], fill=red_fill, font=red_font))

# Rule 2: Highlight projected values if > base
green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
ws.conditional_formatting.add("B15:B16",
    CellIsRule(operator="greaterThan", formula=["B5"], fill=green_fill,
              font=Font(color="006100")))

# ── Freeze Panes: freeze row 4 (below header rows) ──
ws.freeze_panes = "A4"

# ── CHARTS ──

# Chart 1: Daily Trip Trend (Line Chart)
daily = wb["Daily_Summary"]
chart1 = LineChart()
chart1.title = "Daily Trip Volume — January 2024"
chart1.style = 10
chart1.y_axis.title = "Trips"
chart1.x_axis.title = "Date"
chart1.width = 22
chart1.height = 13

cats = Reference(daily, min_col=1, min_row=2, max_row=32)  # A2:A32
vals = Reference(daily, min_col=3, min_row=1, max_row=32)   # C1:C32 with header
chart1.add_data(vals, titles_from_data=True)
chart1.set_categories(cats)
s = chart1.series[0]
s.graphicalProperties.line.width = 22000  # in EMUs
s.graphicalProperties.line.solidFill = "1F4E79"
ws.add_chart(chart1, "A18")

# Chart 2: Payment Method Mix (Pie Chart)
payment = wb["Payment_Summary"]
chart2 = PieChart()
chart2.title = "Payment Method Mix — January 2024"
chart2.style = 10
chart2.width = 18
chart2.height = 13

pie_cats = Reference(payment, min_col=1, min_row=2, max_row=5)
pie_vals = Reference(payment, min_col=2, min_row=1, max_row=5)
chart2.add_data(pie_vals, titles_from_data=True)
chart2.set_categories(pie_cats)

chart2.dataLabels = DataLabelList()
chart2.dataLabels.showPercent = True
chart2.dataLabels.showCatName = True
chart2.dataLabels.showVal = False

# Colors for pie slices
colors = ["4472C4", "ED7D31", "A5A5A5", "FFC000"]
for i, color in enumerate(colors):
    pt = DataPoint(idx=i)
    pt.graphicalProperties.solidFill = color
    chart2.series[0].data_points.append(pt)

ws.add_chart(chart2, "A32")

# ── Save ──
wb.save(SRC)
print("Enhancement complete: saved to", SRC)

```

### Call 2/2: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && python3 /opt/document-bench/bin/run_xlsx_helper_atomic.py enhance_workbook.py output/monthly_operations_report.xlsx
```

Expected phase outputs:

- `enhance_workbook.py`
- `output/monthly_operations_report.xlsx`

## XLSX-P03-process_publish: Recalculate, inspect, and publish summaries

Key operations:

- `XLSX-K06-recalculate_workbook`: Recalculate workbook formulas
- `XLSX-K07-validate_workbook`: Inspect formulas and cached values
- `XLSX-K09-export_summary_csvs`: Export two summary CSV files

Call 1 maps to `XLSX-K06-recalculate_workbook`, calls 2–4 map to
`XLSX-K07-validate_workbook`, and call 5 maps to `XLSX-K09-export_summary_csvs`.

### Call 1/5: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && python3 /root/.openclaw/skills/xlsx/scripts/recalc.py output/monthly_operations_report.xlsx __DOCUMENT_RECALC_TIMEOUT__ > output/formula_recalc.json 2>&1
```

### Call 2/5: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && python3 -c "
from openpyxl import load_workbook
wb = load_workbook('output/monthly_operations_report.xlsx', data_only=False)
ws = wb['Executive_Summary']
for r in range(12, 18):
    for c in range(2, 7):
        cell = ws.cell(row=r, column=c)
        if cell.value is not None:
            print(f'{cell.coordinate}: {repr(cell.value)}')
        else:
            print(f'{cell.coordinate}: None')
print()
print('Merged ranges:', [str(m) for m in ws.merged_cells.ranges])
wb.close()
" 2>&1 | head -40
```

### Call 3/5: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && python3 -c "
from openpyxl import load_workbook
wb = load_workbook('output/monthly_operations_report.xlsx', data_only=True)
ws = wb['Executive_Summary']
for r in range(5, 17):
    cell = ws.cell(row=r, column=2)
    print(f'B{r}: {repr(cell.value)}')
wb.close()
" 2>&1 | head -20
```

### Call 4/5: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cat /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01/output/formula_recalc.json
```

### Call 5/5: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && python3 /opt/document-bench/bin/export_xlsx_csv.py output/monthly_operations_report.xlsx output/monthly_operations_summary.csv output/reconciliation_summary.csv
```

Expected phase outputs:

- `output/formula_recalc.json`
- `output/monthly_operations_summary.csv`
- `output/reconciliation_summary.csv`

## XLSX-P04-verify_deliver: Verify and deliver

Key operations:

- `XLSX-K10-verify_business_rules`: Run XLSX business verification
- `XLSX-K11-generate_summary`: Generate the enhancement summary
- `XLSX-K12-check_deliverables`: Check the six deliverables

Calls 1 through 3 map respectively to `XLSX-K10`, `XLSX-K11`, and `XLSX-K12`.

### Call 1/3: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && python3 input/verify_xlsx_enhanced.py output/monthly_operations_report.xlsx output/formula_recalc.json output/business_verification.json output/monthly_operations_summary.csv output/reconciliation_summary.csv 2>&1
```

### Call 2/3: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && python3 /opt/document-bench/bin/write_xlsx_summary.py output/monthly_operations_report.xlsx output/business_verification.json output/formula_recalc.json output/xlsx_enhancement_summary.json output/monthly_operations_summary.csv output/reconciliation_summary.csv
```

### Call 3/3: `exec`

Complete JSON command, executed verbatim; only the recalc-timeout placeholder is substituted:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01 && test -s output/monthly_operations_report.xlsx && test -s output/monthly_operations_summary.csv && test -s output/reconciliation_summary.csv && test -s output/formula_recalc.json && test -s output/business_verification.json && test -s output/xlsx_enhancement_summary.json && python3 -c "import json; verification=json.load(open('output/business_verification.json')); recalc=json.load(open('output/formula_recalc.json')); summary=json.load(open('output/xlsx_enhancement_summary.json')); assert verification['status']=='success' and not verification['failures']; assert recalc['status']=='success'; assert summary['verifier']['status']=='success' and not summary['verifier']['failures']; print('all XLSX deliverables verified')"
```

Expected phase outputs:

- `output/business_verification.json`
- `output/xlsx_enhancement_summary.json`
- `output/monthly_operations_report.xlsx`
- `output/monthly_operations_summary.csv`
- `output/reconciliation_summary.csv`
- `output/formula_recalc.json`

## Final success decision

After all four phases, the executor reads `output/business_verification.json` again. Success requires `status == "success"` and an empty `failures` list.
