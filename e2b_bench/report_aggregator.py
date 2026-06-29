"""
Report Aggregator Module

Aggregates metrics from multiple batch test tasks into a single Excel report.
Supports styled output with data source grouping and color coding.
"""

import os
from datetime import datetime
from typing import List, Dict, Any
from pathlib import Path


class ReportAggregator:
    """Aggregate batch test results into Excel report"""

    COLUMN_GROUPS = {
        'Basic': ['task_id', 'total_count', 'ratio', 'benchmark_percent'],
        'Browser': ['Browser_Success_Rate', 'Browser_Avg_Latency_ms', 'Browser_P99_Latency_ms', 'Browser_Total_Tasks'],
        'VM_CPU': ['VM_CPU_Mean', 'VM_CPU_Max'],
        'DevKit_TopDown': [
            'DevKit_TopDown_Cycles_Avg', 'DevKit_TopDown_Instructions_Avg',
            'DevKit_TopDown_IPC_Avg', 'DevKit_TopDown_IPC_Max', 'DevKit_TopDown_IPC_Min',
            'DevKit_TopDown_Bad_Speculation', 'DevKit_TopDown_Frontend_Bound',
            'DevKit_TopDown_Retiring', 'DevKit_TopDown_Backend_Bound',
            'DevKit_TopDown_L3_Bound', 'DevKit_TopDown_Mem_Bound',
            'DevKit_TopDown_Latency_Bound', 'DevKit_TopDown_Bandwidth_Bound',
        ],
        'DevKit_Memory': [
            'DevKit_Memory_L1D_Miss', 'DevKit_Memory_L1I_Miss',
            'DevKit_Memory_L2D_Miss', 'DevKit_Memory_L2I_Miss',
            'DevKit_Memory_DDR_Read', 'DevKit_Memory_DDR_Write',
        ],
    }

    SOURCE_COLORS = {
        'Basic': '#FFFFFF',
        'Browser': '#E3F2FD',
        'VM_CPU': '#E8F5E9',
        'DevKit_TopDown': '#FFF3E0',
        'DevKit_Memory': '#FCE4EC',
        'NUMA_Bandwidth': '#F3E5F5',
        'KSys': '#E0F7FA',
        'UBWatch_Latency': '#FFF8E1',
        'UBWatch_Bandwidth': '#EFEBE9',
        'SMAPBW': '#E8EAF6',
        'Getfre': '#FBE9E7',
    }

    def __init__(self, output_dir: str = "results/e2b/batch"):
        self.output_dir = output_dir

    def aggregate(self, metrics_data: List[Dict[str, Any]], output_filename: str = None) -> str:
        """
        Aggregate all test metrics into Excel report

        Args:
            metrics_data: List of dicts, each containing task_id and metrics
            output_filename: Optional custom filename

        Returns:
            Path to generated Excel file
        """
        if not metrics_data:
            print("[ReportAggregator] No metrics data to aggregate")
            return ""

        import pandas as pd

        df = self._build_dataframe(metrics_data)
        df = df.sort_values(by=['total_count', 'ratio', 'benchmark_percent'])

        os.makedirs(self.output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = output_filename or f"e2b_batch_summary_{timestamp}.xlsx"
        output_path = os.path.join(self.output_dir, filename)

        self._export_excel(df, output_path)

        print(f"[ReportAggregator] Report saved to: {output_path}")
        return output_path

    def _build_dataframe(self, metrics_data: List[Dict[str, Any]]) -> 'pd.DataFrame':
        """Build DataFrame from metrics list"""
        import pandas as pd

        rows = []
        for m in metrics_data:
            row = {
                'task_id': m.get('task_id', ''),
                'total_count': m.get('total_count', 0),
                'ratio': m.get('ratio', 0),
                'benchmark_percent': m.get('benchmark_percent', 0),
            }

            for key, value in m.items():
                if key not in row and key not in ['success', 'error_msg', 'result_dir']:
                    row[key] = value

            rows.append(row)

        df = pd.DataFrame(rows)

        # Remove columns that are entirely empty/NaN (tools not enabled)
        # Keep Basic columns (task_id, total_count, ratio, benchmark_percent) always
        basic_cols = ['task_id', 'total_count', 'ratio', 'benchmark_percent']

        # Identify columns to drop: all values are NaN, None, 0, or empty string
        cols_to_drop = []
        for col in df.columns:
            if col in basic_cols:
                continue  # Always keep basic columns

            # Check if column is entirely empty
            if df[col].isna().all():
                cols_to_drop.append(col)
            elif (df[col].fillna(0) == 0).all() and df[col].dtype in ['float64', 'int64']:
                # All numeric values are 0 (tool not enabled or no data)
                cols_to_drop.append(col)
            elif (df[col].fillna('') == '').all() and df[col].dtype == 'object':
                # All string values are empty
                cols_to_drop.append(col)

        if cols_to_drop:
            print(f"[ReportAggregator] Dropping empty columns (tools not enabled): {cols_to_drop}")
            df = df.drop(columns=cols_to_drop)

        return df

    def _export_excel(self, df: 'pd.DataFrame', output_path: str) -> None:
        """Export DataFrame to styled Excel with colored header groups"""
        import pandas as pd

        with pd.ExcelWriter(output_path, engine='xlsxwriter') as writer:
            df.to_excel(writer, sheet_name='Summary', index=False, startrow=1)

            workbook = writer.book
            worksheet = writer.sheets['Summary']

            # Write colored headers based on column groups
            for col_idx, col_name in enumerate(df.columns):
                # Find which group this column belongs to
                source = self._find_column_group(col_name)
                color = self.SOURCE_COLORS.get(source, '#FFFFFF')

                header_format = workbook.add_format({
                    'bold': True,
                    'align': 'center',
                    'valign': 'vcenter',
                    'border': 1,
                    'bg_color': color
                })
                worksheet.write(0, col_idx, col_name, header_format)

            # Set column widths
            for col_idx, col_name in enumerate(df.columns):
                max_len = max(
                    len(str(col_name)),
                    df[col_name].astype(str).str.len().max() if len(df) > 0 else 0
                )
                worksheet.set_column(col_idx, col_idx, min(max_len + 2, 30))

    def _find_column_group(self, col_name: str) -> str:
        """Find which group a column belongs to"""
        # Check defined groups first
        for group, columns in self.COLUMN_GROUPS.items():
            if col_name in columns:
                return group

        # Infer group from column name prefix
        if col_name.startswith('Browser_'):
            return 'Browser'
        elif col_name.startswith('VM_CPU'):
            return 'VM_CPU'
        elif col_name.startswith('DevKit_TopDown'):
            return 'DevKit_TopDown'
        elif col_name.startswith('DevKit_Memory'):
            return 'DevKit_Memory'
        elif col_name.startswith('NUMA_Bandwidth'):
            return 'NUMA_Bandwidth'
        elif col_name.startswith('KSys'):
            return 'KSys'
        elif col_name.startswith('UBWatch_Latency'):
            return 'UBWatch_Latency'
        elif col_name.startswith('UBWatch_Bandwidth'):
            return 'UBWatch_Bandwidth'
        elif col_name.startswith('SMAPBW'):
            return 'SMAPBW'
        elif col_name.startswith('Getfre'):
            return 'Getfre'

        return 'Basic'
