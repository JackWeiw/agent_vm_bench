"""
Test Logging Module

Guards the print()->logging refactor (issue #70):
- setup_logging() configures the root logger with a timestamped format
- every refactored core module exposes a logging.Logger named after itself
- a real converted code path emits a record at the expected level
"""

import logging

import pytest

from e2b_bench.utils import setup_logging

# Modules converted from print() -> logging in issue #70.
E2B_REFACTORED_MODULES = [
    "e2b_bench.bench",
    "e2b_bench.task_runner",
    "e2b_bench.coding_task_runner",
    "e2b_bench.document_task_runner",
    "e2b_bench.round_robin",
    "e2b_bench.sandbox_manager",
    "e2b_bench.batch_scheduler",
    "e2b_bench.metrics_extractor",
    "e2b_bench.stats_collector",
    "e2b_bench.report_aggregator",
    "e2b_bench.helpers",
    "e2b_bench.__main__",
]


class TestSetupLogging:
    """setup_logging must configure root level + a timestamped handler."""

    def test_installs_timestamped_handler_and_sets_level(self):
        """basicConfig should add one StreamHandler with asctime+levelname and set the level."""
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        try:
            # basicConfig is a no-op once root has handlers, so clear them first
            # to exercise the real configuration path.
            root.handlers.clear()
            setup_logging(logging.DEBUG)

            assert root.level == logging.DEBUG
            assert len(root.handlers) == 1

            handler = root.handlers[0]
            assert isinstance(handler, logging.StreamHandler)

            formatter = handler.formatter
            assert formatter is not None
            # Access the format string portably across Python versions.
            fmt_string = getattr(formatter, "_fmt", str(formatter))
            assert "%(asctime)s" in fmt_string
            assert "%(levelname)s" in fmt_string
            assert "%(message)s" in fmt_string
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)

    def test_default_level_is_info(self):
        """Without an explicit level, setup_logging defaults to INFO."""
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        try:
            root.handlers.clear()
            setup_logging()
            assert root.level == logging.INFO
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)


class TestModuleLoggers:
    """Each refactored module must expose a logger named <package>.<module>."""

    @pytest.mark.parametrize("module_name", E2B_REFACTORED_MODULES)
    def test_module_defines_named_logger(self, module_name):
        import importlib

        module = importlib.import_module(module_name)
        assert hasattr(module, "logger"), f"{module_name} has no `logger` attribute"
        assert isinstance(module.logger, logging.Logger)
        assert module.logger.name == module_name


class TestConvertedCodePaths:
    """Verify a real converted code path emits log records at the right level."""

    def test_missing_analysis_file_logs_warning(self, caplog):
        """MetricsExtractor.extract on a missing file must emit a WARNING (was print)."""
        from e2b_bench.metrics_extractor import MetricsExtractor

        with caplog.at_level(logging.WARNING, logger="e2b_bench.metrics_extractor"):
            result = MetricsExtractor().extract("/nonexistent/e2b_metrics_report.xlsx")

        # Functional contract unchanged: missing file -> empty dict.
        assert result == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "File not found" in warnings[0].message
        assert warnings[0].name == "e2b_bench.metrics_extractor"

    def test_report_aggregator_empty_data_logs_warning(self, caplog):
        """ReportAggregator.aggregate on empty data must emit a WARNING (was print)."""
        from e2b_bench.report_aggregator import ReportAggregator

        with caplog.at_level(logging.WARNING, logger="e2b_bench.report_aggregator"):
            result = ReportAggregator(output_dir="/tmp/e2b_test_agg").aggregate([])

        assert result == ""
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "No metrics data" in warnings[0].message


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
