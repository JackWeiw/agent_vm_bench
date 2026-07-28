"""Unit tests for e2b_bench.snap.common.print_summary."""

from e2b_bench.snap.common import print_summary


class TestPrintSummary:
    """Tests for print_summary terminal output."""

    def test_prints_title_and_borders(self, capsys):
        summary = {
            "restore_sandbox_s": {"count": 5, "success_rate": 1.0, "avg": 2.5},
        }
        print_summary(summary, "Restore Summary")
        out = capsys.readouterr().out
        assert "Restore Summary" in out
        assert out.startswith("=")
        assert out.rstrip().endswith("=")

    def test_prints_metric_rows_and_series_columns(self, capsys):
        summary = {
            "create_sandbox_s": {"count": 10, "avg": 12.5},
            "create_snapshot_s": {"count": 10, "avg": 3.2},
        }
        print_summary(summary, "Title")
        out = capsys.readouterr().out
        # metric column header present
        assert "metric" in out
        # both series names appear as column headers
        assert "create_sandbox_s" in out
        assert "create_snapshot_s" in out
        # stat rows present
        assert "count" in out
        assert "avg" in out

    def test_empty_summary_still_prints_title(self, capsys):
        print_summary({}, "Empty")
        out = capsys.readouterr().out
        assert "Empty" in out

    def test_missing_stat_key_shows_blank_not_crash(self, capsys):
        # series with only one stat key — other rows must show blank, not KeyError
        summary = {"total_s": {"count": 3}}
        print_summary(summary, "Title")
        out = capsys.readouterr().out
        assert "count" in out
        assert "total_s" in out

    def test_table_columns_align(self, capsys):
        """Border/separator widths must equal the content row width."""
        summary = {
            "create_sandbox_s": {"count": 10, "avg": 12.5},
            "create_snapshot_s": {"count": 10, "avg": 3.2},
        }
        print_summary(summary, "Title")
        out = capsys.readouterr().out
        lines = out.splitlines()
        # Find content lines (data rows + header), which are non-empty and not borders/separators/title
        content_widths = {len(line) for line in lines if line and not set(line) <= {"=", "-"} and "Title" not in line}
        # All content rows (header + data) must be the same width
        assert len(content_widths) == 1, f"Rows have differing widths: {content_widths}"
        # Borders and separators must equal that width
        target_w = content_widths.pop()
        border_lines = [line for line in lines if set(line) <= {"="}]
        sep_lines = [line for line in lines if set(line) <= {"-"}]
        assert all(len(l) == target_w for l in border_lines), "border width mismatch"
        assert all(len(l) == target_w for l in sep_lines), "separator width mismatch"
