"""
Test MetricsExtractor Module

Tests for MetricsExtractor: _to_float, extract_browser_metrics, extract from Excel
"""

import pytest
import os
import tempfile

from e2b_bench.metrics_extractor import MetricsExtractor


class TestToFloat:
    """Tests for _to_float helper"""

    def test_float_value(self):
        extractor = MetricsExtractor()
        assert extractor._to_float(123.45) == 123.45
        assert extractor._to_float(0) == 0.0

    def test_string_value(self):
        extractor = MetricsExtractor()
        assert extractor._to_float("123.45") == 123.45

    def test_percentage_string(self):
        extractor = MetricsExtractor()
        assert extractor._to_float("45.5%") == 45.5
        assert extractor._to_float("100%") == 100.0

    def test_none_value(self):
        extractor = MetricsExtractor()
        assert extractor._to_float(None) == 0.0

    def test_invalid_string(self):
        extractor = MetricsExtractor()
        assert extractor._to_float("invalid") == 0.0
        assert extractor._to_float("") == 0.0


class TestExtractBrowserMetrics:
    """Tests for extract_browser_metrics"""

    def test_file_not_found(self):
        extractor = MetricsExtractor()
        result = extractor.extract_browser_metrics("/nonexistent/file.txt")
        assert result == {}

    def test_full_report(self):
        """Extract all metrics from complete report"""
        report_content = """
Browser Task Statistics
========================

Total Tasks: 100
Success Rate: 95.50%
Avg Latency: 1234.56ms
P99 Latency: 5678.90ms
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(report_content)
            f.flush()
            extractor = MetricsExtractor()
            result = extractor.extract_browser_metrics(f.name)
            os.unlink(f.name)

        assert result["Browser_Success_Rate"] == 95.50
        assert result["Browser_Avg_Latency_ms"] == 1234.56
        assert result["Browser_P99_Latency_ms"] == 5678.90
        assert result["Browser_Total_Tasks"] == 100

    def test_partial_report(self):
        """Extract partial metrics"""
        report_content = """
Total Tasks: 50
Success Rate: 80.0%
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(report_content)
            f.flush()
            extractor = MetricsExtractor()
            result = extractor.extract_browser_metrics(f.name)
            os.unlink(f.name)

        assert result["Browser_Success_Rate"] == 80.0
        assert result["Browser_Total_Tasks"] == 50
        assert "Browser_Avg_Latency_ms" not in result


class TestExtractExcel:
    """Tests for extract from Excel file"""

    def test_file_not_found(self):
        extractor = MetricsExtractor()
        result = extractor.extract("/nonexistent/file.xlsx")
        assert result == {}

    @pytest.mark.skipif(
        not pytest.importorskip("pandas", reason="pandas not installed"),
        reason="pandas not installed"
    )
    def test_extract_summary_sheet(self):
        """Extract from Summary sheet"""
        import pandas as pd

        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            excel_path = f.name

        try:
            with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
                summary_df = pd.DataFrame({
                    'Metric': ['VM Avg CPU', 'VM Peak Total CPU'],
                    'Value': [25.5, 80.0]
                })
                summary_df.to_excel(writer, sheet_name='Summary', index=False)

            extractor = MetricsExtractor()
            result = extractor.extract(excel_path)

            assert result["VM_CPU_Mean"] == 25.5
            assert result["VM_CPU_Max"] == 80.0

        finally:
            os.unlink(excel_path)

    @pytest.mark.skipif(
        not pytest.importorskip("pandas", reason="pandas not installed"),
        reason="pandas not installed"
    )
    def test_extract_devkit_topdown(self):
        """Extract from DevKit_TopDown sheet"""
        import pandas as pd

        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            excel_path = f.name

        try:
            with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
                topdown_df = pd.DataFrame({
                    'Metric': ['IPC Avg', 'Backend Bound (%)', 'Mem Bound (%)'],
                    'Value': [0.85, 35.5, 20.0]
                })
                topdown_df.to_excel(writer, sheet_name='DevKit_TopDown', index=False)

            extractor = MetricsExtractor()
            result = extractor.extract(excel_path)

            assert result["DevKit_TopDown_IPC_Avg"] == 0.85
            assert result["DevKit_TopDown_Backend_Bound"] == 35.5
            assert result["DevKit_TopDown_Mem_Bound"] == 20.0

        finally:
            os.unlink(excel_path)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])