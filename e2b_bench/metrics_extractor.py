"""
Metrics Extractor Module

Extracts performance metrics from vm_monitor analysis_report.xlsx.
Supports all sheet types: Summary, DevKit_TopDown, DevKit_Memory,
NUMA_Bandwidth, KSys, UBWatch_Latency, UBWatch_Bandwidth, SMAPBW, Getfre.
"""

import os
import re
from typing import Dict, Any, Optional
from pathlib import Path


class MetricsExtractor:
    """Extract metrics from vm_monitor analysis_report.xlsx"""

    def __init__(self):
        pass

    def extract(self, analysis_file: str) -> Dict[str, Any]:
        """
        Extract all metrics from analysis_report.xlsx

        Args:
            analysis_file: Path to analysis_report.xlsx

        Returns:
            Dict containing all extracted metrics with prefixed keys
        """
        if not os.path.exists(analysis_file):
            print(f"[MetricsExtractor] File not found: {analysis_file}")
            return {}

        metrics = {}

        try:
            import pandas as pd
            xls = pd.ExcelFile(analysis_file)

            # Extract from each sheet
            metrics.update(self._extract_summary(xls))
            metrics.update(self._extract_devkit_topdown(xls))
            metrics.update(self._extract_devkit_memory(xls))
            metrics.update(self._extract_numa_bandwidth(xls))
            metrics.update(self._extract_ksys(xls))
            metrics.update(self._extract_ubwatch_latency(xls))
            metrics.update(self._extract_ubwatch_bandwidth(xls))
            metrics.update(self._extract_smapbw_summary(xls))
            metrics.update(self._extract_smapbw_cycles(xls))
            metrics.update(self._extract_getfre(xls))

            print(f"[MetricsExtractor] Extracted {len(metrics)} metrics from {analysis_file}")

        except Exception as e:
            print(f"[MetricsExtractor] Error extracting metrics: {e}")

        return metrics

    def _extract_summary(self, xls) -> Dict[str, Any]:
        """Extract Summary sheet metrics (VM CPU)"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="Summary")
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric == "VM Avg CPU":
                    metrics["VM_CPU_Mean"] = self._to_float(value)
                elif metric == "VM Peak Total CPU":
                    metrics["VM_CPU_Max"] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_devkit_topdown(self, xls) -> Dict[str, Any]:
        """Extract DevKit_TopDown sheet (13 metrics)"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="DevKit_TopDown")
            key_map = {
                "Cycles Avg": "DevKit_TopDown_Cycles_Avg",
                "Instructions Avg": "DevKit_TopDown_Instructions_Avg",
                "IPC Avg": "DevKit_TopDown_IPC_Avg",
                "IPC Max": "DevKit_TopDown_IPC_Max",
                "IPC Min": "DevKit_TopDown_IPC_Min",
                "Bad Speculation (%)": "DevKit_TopDown_Bad_Speculation",
                "Frontend Bound (%)": "DevKit_TopDown_Frontend_Bound",
                "Retiring (%)": "DevKit_TopDown_Retiring",
                "Backend Bound (%)": "DevKit_TopDown_Backend_Bound",
                "L3 Bound (%)": "DevKit_TopDown_L3_Bound",
                "Mem Bound (%)": "DevKit_TopDown_Mem_Bound",
                "Latency Bound (%)": "DevKit_TopDown_Latency_Bound",
                "Bandwidth Bound (%)": "DevKit_TopDown_Bandwidth_Bound",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    metrics[key_map[metric]] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_devkit_memory(self, xls) -> Dict[str, Any]:
        """Extract DevKit_Memory sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="DevKit_Memory")
            key_map = {
                "L1D Miss (%)": "DevKit_Memory_L1D_Miss",
                "L1I Miss (%)": "DevKit_Memory_L1I_Miss",
                "L2D Miss (%)": "DevKit_Memory_L2D_Miss",
                "L2I Miss (%)": "DevKit_Memory_L2I_Miss",
                "DDR Read (MB/s)": "DevKit_Memory_DDR_Read",
                "DDR Write (MB/s)": "DevKit_Memory_DDR_Write",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    metrics[key_map[metric]] = self._to_float(value)
                elif "L3 Hit Rate" in metric:
                    numa_match = re.match(r"NUMA(\d+)\s+L3 Hit Rate", metric)
                    if numa_match:
                        node_id = numa_match.group(1)
                        metrics[f"DevKit_Memory_NUMA{node_id}_L3_Hit_Rate"] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_numa_bandwidth(self, xls) -> Dict[str, Any]:
        """Extract NUMA_Bandwidth sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="NUMA_Bandwidth")
            total_read = 0.0
            total_write = 0.0
            for idx, row in df.iterrows():
                node = str(row.get("NUMA Node", "")).strip()
                read = self._to_float(row.get("Read (MB/s)", 0))
                write = self._to_float(row.get("Write (MB/s)", 0))
                if node:
                    metrics[f"NUMA_Bandwidth_{node}_Read"] = read
                    metrics[f"NUMA_Bandwidth_{node}_Write"] = write
                    total_read += read
                    total_write += write
            metrics["NUMA_Bandwidth_Total_Read"] = total_read
            metrics["NUMA_Bandwidth_Total_Write"] = total_write
        except Exception:
            pass
        return metrics

    def _extract_ksys(self, xls) -> Dict[str, Any]:
        """Extract KSys sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="KSys")
            key_map = {
                "L2 Miss Latency Max": "KSys_L2_Miss_Latency_Max",
                "L2 Miss Latency Min": "KSys_L2_Miss_Latency_Min",
                "L2 Miss Latency Avg": "KSys_L2_Miss_Latency_Avg",
                "L3 Miss Latency Max": "KSys_L3_Miss_Latency_Max",
                "L3 Miss Latency Min": "KSys_L3_Miss_Latency_Min",
                "L3 Miss Latency Avg": "KSys_L3_Miss_Latency_Avg",
                "IPC": "KSys_IPC",
                "Retiring (%)": "KSys_Retiring",
                "Frontend Bound (%)": "KSys_Frontend_Bound",
                "Bad Speculation (%)": "KSys_Bad_Speculation",
                "Backend Bound (%)": "KSys_Backend_Bound",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    metrics[key_map[metric]] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_ubwatch_latency(self, xls) -> Dict[str, Any]:
        """Extract UBWatch_Latency sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="UBWatch_Latency")
            key_map = {
                "Samples": "UBWatch_Latency_Samples",
                "Avg Read (ns)": "UBWatch_Latency_Avg_Read_ns",
                "Avg Write (ns)": "UBWatch_Latency_Avg_Write_ns",
                "Min Read (ns)": "UBWatch_Latency_Min_Read_ns",
                "Min Write (ns)": "UBWatch_Latency_Min_Write_ns",
                "Max Read (ns)": "UBWatch_Latency_Max_Read_ns",
                "Max Write (ns)": "UBWatch_Latency_Max_Write_ns",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    try:
                        metrics[key_map[metric]] = float(value) if value is not None else 0
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass
        return metrics

    def _extract_ubwatch_bandwidth(self, xls) -> Dict[str, Any]:
        """Extract UBWatch_Bandwidth sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="UBWatch_Bandwidth")
            total_avg_wr = 0.0
            total_avg_rd = 0.0
            total_avg_sum = 0.0

            for idx, row in df.iterrows():
                chip = int(row.get("Chip", -1)) if row.get("Chip") is not None else None
                ports = str(row.get("Ports", "")).strip()
                if chip is not None and chip >= 0 and ports:
                    port_key = "p" + ports.replace("&", "")

                    avg_wr = self._to_float(row.get("Avg Write (MB/s)", 0))
                    avg_rd = self._to_float(row.get("Avg Read (MB/s)", 0))
                    avg_sum = self._to_float(row.get("Avg Sum (MB/s)", 0))

                    key_prefix = f"UBWatch_Bandwidth_Chip{chip}_{port_key}"
                    metrics[f"{key_prefix}_Avg_Write"] = avg_wr
                    metrics[f"{key_prefix}_Avg_Read"] = avg_rd
                    metrics[f"{key_prefix}_Avg_Sum"] = avg_sum

                    total_avg_wr += avg_wr
                    total_avg_rd += avg_rd
                    total_avg_sum += avg_sum

            metrics["UBWatch_Bandwidth_Total_Avg_Write"] = total_avg_wr
            metrics["UBWatch_Bandwidth_Total_Avg_Read"] = total_avg_rd
            metrics["UBWatch_Bandwidth_Total_Avg_Sum"] = total_avg_sum
        except Exception:
            pass
        return metrics

    def _extract_smapbw_summary(self, xls) -> Dict[str, Any]:
        """Extract SMAPBW_Summary sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="SMAPBW_Summary")
            key_map = {
                "Total Cycles": "SMAPBW_Total_Cycles",
                "Total Pages": "SMAPBW_Total_Pages",
                "Avg Bandwidth (GB/s)": "SMAPBW_Avg_Bandwidth_GB_s",
                "Min Bandwidth (GB/s)": "SMAPBW_Min_Bandwidth_GB_s",
                "Max Bandwidth (GB/s)": "SMAPBW_Max_Bandwidth_GB_s",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    metrics[key_map[metric]] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_smapbw_cycles(self, xls) -> Dict[str, Any]:
        """Extract SMAPBW_Cycles sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="SMAPBW_Cycles")
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric and pd.notna(value):
                    key = f"SMAPBW_Cycles_{metric.replace(' ', '_').replace('(', '').replace(')', '')}"
                    metrics[key] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_getfre(self, xls) -> Dict[str, Any]:
        """Extract Getfre_Summary sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="Getfre_Summary")
            for idx, row in df.iterrows():
                numa = str(row.get("NUMA", "")).strip()
                core_freq = self._to_float(row.get("CoreFreq (MHz)", 0))
                if numa:
                    metrics[f"Getfre_{numa}_CoreFreq_MHz"] = core_freq
        except Exception:
            pass
        return metrics

    def _to_float(self, value: Any) -> float:
        """Convert value to float, handling percentage strings"""
        if value is None:
            return 0.0
        try:
            if isinstance(value, str):
                if '%' in value:
                    return float(value.replace('%', '').strip())
                return float(value.strip())
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    def extract_browser_metrics(self, report_file: str) -> Dict[str, Any]:
        """Extract browser metrics from bench_report.txt"""
        metrics = {}
        if not report_file or not os.path.exists(report_file):
            return metrics

        try:
            with open(report_file, 'r', encoding='utf-8') as f:
                content = f.read()

            match = re.search(r"Success Rate:\s+([\d.]+)%", content)
            if match:
                metrics["Browser_Success_Rate"] = float(match.group(1))

            match = re.search(r"Avg Latency:\s+([\d.]+)ms", content)
            if match:
                metrics["Browser_Avg_Latency_ms"] = float(match.group(1))

            match = re.search(r"P99 Latency:\s+([\d.]+)ms", content)
            if match:
                metrics["Browser_P99_Latency_ms"] = float(match.group(1))

            match = re.search(r"Total Tasks:\s+(\d+)", content)
            if match:
                metrics["Browser_Total_Tasks"] = int(match.group(1))

        except Exception as e:
            print(f"[MetricsExtractor] Error extracting browser metrics: {e}")

        return metrics
