# PDF 场景在 E2B 中的逐调用执行说明

本文逐条对应 `pdf_key_operations.json`。JSON 是唯一可执行定义；本文为中文说明，不参与运行。

## 执行器语义

每轮先复制 `/opt/document-bench/pdf` 到固定工作区，再按 P01 → P04 执行。任一调用失败或超时即停止。

- `read`：运行 `test -f <path> && head -c 65536 <path> >/dev/null`，仅检查文件存在且可读。
- `write`：创建父目录后调用 `sbx.files.write(path, content, user="root", request_timeout=...)`。
- `exec`：调用 `sbx.commands.run(command, timeout=..., user="root")`。

## PDF-P01-inspect_prepare: 检查与准备

关键操作:

- `PDF-K01-read_requirements`: 读取 PDF Skill 与表单指南
- `PDF-K02-inspect_tools`: 检查 verifier 和 PDF 工具脚本
- `PDF-K03-prepare_outputs`: 创建输出目录
- `PDF-K04-check_form_fields`: 检查 PDF 是否为可填写表单
- `PDF-K05-extract_fields`: 提取表单字段信息
- `PDF-K06-render_blank_form`: 渲染空白模板

调用与关键操作对应关系：

| 调用 | 关键操作 |
| --- | --- |
| 1–2 | `PDF-K01-read_requirements` |
| 3–7 | `PDF-K02-inspect_tools` |
| 8 | `PDF-K03-prepare_outputs` |
| 9–10 | `PDF-K04-check_form_fields` |
| 11–12 | `PDF-K05-extract_fields` |
| 13 | `PDF-K06-render_blank_form` |

### 调用 1/13: `read`

配方目标路径:

```text
/root/.openclaw/skills/pdf/SKILL.md
```

E2B 实际命令:

```bash
test -f /root/.openclaw/skills/pdf/SKILL.md && head -c 65536 /root/.openclaw/skills/pdf/SKILL.md >/dev/null
```

### 调用 2/13: `read`

配方目标路径:

```text
/root/.openclaw/skills/pdf/forms.md
```

E2B 实际命令:

```bash
test -f /root/.openclaw/skills/pdf/forms.md && head -c 65536 /root/.openclaw/skills/pdf/forms.md >/dev/null
```

### 调用 3/13: `read`

配方目标路径:

```text
/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/input/verify_pdf_batch.py
```

E2B 实际命令:

```bash
test -f /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/input/verify_pdf_batch.py && head -c 65536 /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/input/verify_pdf_batch.py >/dev/null
```

### 调用 4/13: `read`

配方目标路径:

```text
/root/.openclaw/skills/pdf/scripts/check_fillable_fields.py
```

E2B 实际命令:

```bash
test -f /root/.openclaw/skills/pdf/scripts/check_fillable_fields.py && head -c 65536 /root/.openclaw/skills/pdf/scripts/check_fillable_fields.py >/dev/null
```

### 调用 5/13: `read`

配方目标路径:

```text
/root/.openclaw/skills/pdf/scripts/extract_form_field_info.py
```

E2B 实际命令:

```bash
test -f /root/.openclaw/skills/pdf/scripts/extract_form_field_info.py && head -c 65536 /root/.openclaw/skills/pdf/scripts/extract_form_field_info.py >/dev/null
```

### 调用 6/13: `read`

配方目标路径:

```text
/root/.openclaw/skills/pdf/scripts/fill_fillable_fields.py
```

E2B 实际命令:

```bash
test -f /root/.openclaw/skills/pdf/scripts/fill_fillable_fields.py && head -c 65536 /root/.openclaw/skills/pdf/scripts/fill_fillable_fields.py >/dev/null
```

### 调用 7/13: `read`

配方目标路径:

```text
/root/.openclaw/skills/pdf/scripts/convert_pdf_to_images.py
```

E2B 实际命令:

```bash
test -f /root/.openclaw/skills/pdf/scripts/convert_pdf_to_images.py && head -c 65536 /root/.openclaw/skills/pdf/scripts/convert_pdf_to_images.py >/dev/null
```

### 调用 8/13: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
mkdir -p /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/rendered/template /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/field_values /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/filled
```

### 调用 9/13: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
cd /root/.openclaw/skills/pdf && python3 scripts/check_fillable_fields.py /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/input/of306_aug2023.pdf > /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/check_fillable_fields.log 2>&1
```

### 调用 10/13: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
cat /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/check_fillable_fields.log
```

### 调用 11/13: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
cd /root/.openclaw/skills/pdf && python3 scripts/extract_form_field_info.py /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/input/of306_aug2023.pdf /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/form_field_info.json
```

### 调用 12/13: `read`

配方目标路径:

```text
/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/form_field_info.json
```

E2B 实际命令:

```bash
test -f /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/form_field_info.json && head -c 65536 /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/form_field_info.json >/dev/null
```

### 调用 13/13: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
cd /root/.openclaw/skills/pdf && python3 scripts/convert_pdf_to_images.py /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/input/of306_aug2023.pdf /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/rendered/template
```

阶段预期输出：

- `output/rendered/template`
- `output/field_values`
- `output/filled`
- `output/check_fillable_fields.log`
- `output/form_field_info.json`
- `output/rendered/template/page_1.png`
- `output/rendered/template/page_2.png`
- `output/rendered/template/page_3.png`

## PDF-P02-build: 构建批处理输入

关键操作:

- `PDF-K07-write_mapping_helper`: 写入字段映射 helper
- `PDF-K08-generate_field_values`: 生成 10 份字段值 JSON
- `PDF-K09-validate_field_values`: 校验字段 ID、页码和敏感字段
- `PDF-K10-write_batch_helper`: 写入批量填充与渲染 helper

调用 1 至 4 分别对应 `PDF-K07` 至 `PDF-K10`。

### 调用 1/4: `write`

写入目标:

```text
/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/generate_and_run_batch.py
```

E2B 实际 API 动作:

```python
sbx.commands.run("mkdir -p /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01", timeout=..., user="root")
sbx.files.write("/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/generate_and_run_batch.py", arguments["content"], user="root", request_timeout=...)
```

写入内容是本调用的 `tool_call.arguments.content`；首个非空行是 `#!/usr/bin/env python3`。完整 payload 保留在 JSON 中，避免说明文档复制源码后与可执行定义漂移。

完整写入内容:

```text
#!/usr/bin/env python3
"""Generate field-values JSONs for all 10 applicants and run fill + render."""

import json
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path("/root/.openclaw/skills/pdf")
INPUT_PDF = Path("/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/input/of306_aug2023.pdf")
OUTPUT = Path("/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output")
FIELD_VALUES_DIR = OUTPUT / "field_values"
FILLED_DIR = OUTPUT / "filled"

with open(Path("/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/input/synthetic_applicants.json")) as f:
    data = json.load(f)

applicants = data["applicants"]
shared = data["shared_answers"]
continuation_text = shared["continuation"]

for a in applicants:
    aid = a["id"]
    values = []

    # --- Page 2 fields ---

    # Full Name
    values.append({
        "field_id": "Full Name",
        "description": "Full name of applicant",
        "page": 2,
        "value": a["full_name"]
    })

    # Place of birth
    values.append({
        "field_id": "PLACE OF BIRTH Include city and state or country",
        "description": "Place of birth",
        "page": 2,
        "value": a["place_of_birth"]
    })

    # Country of Citizenship
    values.append({
        "field_id": "Country of Citizenship",
        "description": "Country of citizenship",
        "page": 2,
        "value": "United States"
    })

    # US Citizen - all Yes
    values.append({
        "field_id": "Are you a U.S. Citizen?",
        "description": "US Citizen radio",
        "page": 2,
        "value": "/Yes"
    })

    # Date of birth
    values.append({
        "field_id": "DATE OF BIRTH MM  DD  YYYY",
        "description": "Date of birth",
        "page": 2,
        "value": a["date_of_birth"]
    })

    # Day phone
    values.append({
        "field_id": "Day",
        "description": "Daytime phone number",
        "page": 2,
        "value": a["day_phone"]
    })

    # Other names
    values.append({
        "field_id": "Other Names Used 2",
        "description": "Other names used",
        "page": 2,
        "value": a["other_names"]
    })

    # Male (born male after 1959)
    male_value = "/Yes" if a["born_male_after_1959"] else "/No"
    values.append({
        "field_id": "Male",
        "description": "Born male after Dec 31 1959",
        "page": 2,
        "value": male_value
    })

    # Selective Service registration
    ss_value = "/Yes" if a["registered_selective_service"] else "/No"
    values.append({
        "field_id": "Have you registered with Selective Service",
        "description": "Selective Service registration",
        "page": 2,
        "value": ss_value
    })

    # --- Shared background answers (all No) ---

    # Military service - No
    values.append({
        "field_id": "Have you ever served in the U.S. Military",
        "description": "Military service",
        "page": 2,
        "value": "/No"
    })

    # Convicted/paroled - No
    values.append({
        "field_id": "Have you been convicted imprisoned probation or paroled last 7 years",
        "description": "Convicted/paroled last 7 years",
        "page": 2,
        "value": "/No"
    })

    # Court martialed - No
    values.append({
        "field_id": "Have you been court martialed in the last 7 years",
        "description": "Court martialed last 7 years",
        "page": 2,
        "value": "/No"
    })

    # Currently under charges - No
    values.append({
        "field_id": "Are you currently under charges",
        "description": "Currently under charges",
        "page": 2,
        "value": "/No"
    })

    # Fired/debarred - No
    values.append({
        "field_id": "Have you been fired or debarred or quit due to a specific problem or quit after being told you would be fired",
        "description": "Fired/debarred/quit",
        "page": 2,
        "value": "/No"
    })

    # Delinquent federal debt - No
    values.append({
        "field_id": "Are you delinquent on any Federal debt",
        "description": "Delinquent federal debt",
        "page": 2,
        "value": "/No"
    })

    # --- Page 3 fields ---

    # Relative at agency - No
    values.append({
        "field_id": "Do any of your relatives work for the agency or government organization for which you are submitting this form",
        "description": "Relative at agency",
        "page": 3,
        "value": "/No"
    })

    # Government retirement/pension - No
    values.append({
        "field_id": "Have you applied or do you receive retirement or pension benefits from the military Federal or D.C. government",
        "description": "Government retirement/pension",
        "page": 3,
        "value": "/No"
    })

    # Continuation text
    values.append({
        "field_id": "Continuation of Space or Agency Specific Questions",
        "description": "Continuation text",
        "page": 3,
        "value": continuation_text
    })

    # Write the field-values JSON
    out_path = FIELD_VALUES_DIR / f"{aid}.json"
    with open(out_path, "w") as f:
        json.dump(values, f, indent=2)
    print(f"Wrote {out_path}")

print("--- All field-values JSONs written ---")

```

### 调用 2/4: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01 && python3 generate_and_run_batch.py
```

### 调用 3/4: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
python3 -c "
import json
# Verify all field IDs exist in field_info
with open('/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/form_field_info.json') as f:
    fi = json.load(f)
valid_ids = {x['field_id'] for x in fi}
for i in range(1, 11):
    with open(f'/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/field_values/applicant_{i:02d}.json') as f:
        vals = json.load(f)
    for v in vals:
        fid = v['field_id']
        if fid not in valid_ids:
            print(f'ERROR: {fid} not in field_info')
        fi_entry = [x for x in fi if x['field_id'] == fid][0]
        if v['page'] != fi_entry.get('page'):
            print(f'ERROR: page mismatch for {fid}')
    # Check no forbidden fields
    forbidden = {'Social Security Number','17a Applicants Signature','17b Appointees Signature 1','Date','Date_2','Appointing Officer Enter Date of Appointment or Conversion MM  DD  YYYY','When did you leave your last Federal job','Did you waive Basic Life Insurance or any type of optional life insurance','If yes to 18b did you later cancel the waivers'}
    provided_ids = {v['field_id'] for v in vals}
    overlap = provided_ids & forbidden
    if overlap:
        print(f'ERROR: applicant_{i:02d} includes forbidden fields: {overlap}')
print('Validation complete')
" 2>&1
```

### 调用 4/4: `write`

写入目标:

```text
/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/run_batch_fill_render.py
```

E2B 实际 API 动作:

```python
sbx.commands.run("mkdir -p /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01", timeout=..., user="root")
sbx.files.write("/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/run_batch_fill_render.py", arguments["content"], user="root", request_timeout=...)
```

写入内容是本调用的 `tool_call.arguments.content`；首个非空行是 `#!/usr/bin/env python3`。完整 payload 保留在 JSON 中，避免说明文档复制源码后与可执行定义漂移。

完整写入内容:

```text
#!/usr/bin/env python3
"""Execute fill+render for all 10 applicants. Invocations: 10 fills, 10 renders."""

import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path("/root/.openclaw/skills/pdf")
INPUT_PDF = Path("/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/input/of306_aug2023.pdf")
OUTPUT = Path("/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output")
FIELD_VALUES_DIR = OUTPUT / "field_values"
FILLED_DIR = OUTPUT / "filled"

FILL_SCRIPT = SKILL_DIR / "scripts/fill_fillable_fields.py"
RENDER_SCRIPT = SKILL_DIR / "scripts/convert_pdf_to_images.py"

applicant_ids = [f"applicant_{i:02d}" for i in range(1, 11)]

fill_count = 0
render_count = 0

for aid in applicant_ids:
    values_json = FIELD_VALUES_DIR / f"{aid}.json"
    out_pdf = FILLED_DIR / f"{aid}.pdf"

    # Fill
    cmd_fill = [
        "python3", str(FILL_SCRIPT),
        str(INPUT_PDF), str(values_json), str(out_pdf)
    ]
    result = subprocess.run(cmd_fill, capture_output=True, text=True, cwd=str(SKILL_DIR))
    fill_count += 1
    if result.returncode != 0:
        print(f"FAIL fill {aid}: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"Filled {aid} -> {out_pdf}")

    # Render
    render_dir = OUTPUT / "rendered" / aid
    render_dir.mkdir(parents=True, exist_ok=True)
    cmd_render = [
        "python3", str(RENDER_SCRIPT),
        str(out_pdf), str(render_dir)
    ]
    result = subprocess.run(cmd_render, capture_output=True, text=True, cwd=str(SKILL_DIR))
    render_count += 1
    if result.returncode != 0:
        print(f"FAIL render {aid}: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"Rendered {aid} -> {render_dir}")

print(f"\nDone. Fills: {fill_count}, Renders: {render_count}")

```

阶段预期输出：

- `generate_and_run_batch.py`
- `output/field_values/applicant_01.json ... applicant_10.json`
- `run_batch_fill_render.py`

## PDF-P03-process_publish: 批量处理与发布

关键操作:

- `PDF-K11-fill_and_render`: 批量填充并渲染 10 份 PDF
- `PDF-K12-write_summary`: 写入批处理摘要

调用 1 对应 `PDF-K11-fill_and_render`；调用 2 对应 `PDF-K12-write_summary`。

### 调用 1/2: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01 && python3 run_batch_fill_render.py
```

### 调用 2/2: `write`

写入目标:

```text
/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/batch_summary.json
```

E2B 实际 API 动作:

```python
sbx.commands.run("mkdir -p /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output", timeout=..., user="root")
sbx.files.write("/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/batch_summary.json", arguments["content"], user="root", request_timeout=...)
```

写入内容是本调用的 `tool_call.arguments.content`；首个非空行是 `{`。完整 payload 保留在 JSON 中，避免说明文档复制源码后与可执行定义漂移。

完整写入内容:

```text
{
  "input_form": "of306_aug2023.pdf",
  "applicant_count": 10,
  "filled_pdf_count": 10,
  "rendered_page_count": 33,
  "fill_script_invocations": 10,
  "render_script_invocations": 11,
  "ssn_and_signatures_left_blank": true
}

```

阶段预期输出：

- `output/filled/applicant_01.pdf ... applicant_10.pdf`
- `output/rendered/applicant_01 ... applicant_10`
- `output/batch_summary.json`

## PDF-P04-verify_deliver: 业务验收与交付检查

关键操作:

- `PDF-K13-verify_outputs`: 运行 PDF 业务验收
- `PDF-K14-check_deliverables`: 核对全部交付物

调用 1 对应 `PDF-K13-verify_outputs`；调用 2 对应 `PDF-K14-check_deliverables`。

### 调用 1/2: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
cd /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01 && python3 input/verify_pdf_batch.py input/of306_aug2023.pdf input/synthetic_applicants.json output output/business_verification.json
```

### 调用 2/2: `exec`

JSON 中的完整命令，E2B 原样执行；仅重算超时占位符会被替换:

```bash
echo "=== Output tree ===" && find /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output -type f | sort && echo "=== PNG count ===" && find /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/rendered -name '*.png' | wc -l && echo "=== Filled PDF count ===" && ls /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/filled/*.pdf | wc -l && echo "=== Field values count ===" && ls /root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/field_values/*.json | wc -l && echo "=== business_verification.json status ===" && python3 -c "import json; r=json.load(open('/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01/output/business_verification.json')); print(r['status'])"
```

阶段预期输出：

- `output/business_verification.json`
- `output/check_fillable_fields.log`
- `output/form_field_info.json`
- `output/field_values/*.json`
- `output/filled/*.pdf`
- `output/rendered/**/*.png`
- `output/batch_summary.json`

## 最终成功判定

四阶段结束后，执行器再次读取 `output/business_verification.json`；仅当 `status == "success"` 且 `failures` 为空时成功。
