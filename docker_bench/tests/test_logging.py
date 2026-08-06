"""
Test Logging Module (docker_bench)

Guards the print()->logging refactor (issue #70) for the docker_bench package:
- setup_logging() configures the root logger with a timestamped format
- refactored modules expose a logging.Logger named after themselves
- a module logger propagates records to the root handler
"""

import logging

import pytest

from docker_bench.utils import setup_logging

# Modules converted from print() -> logging that do not require the docker SDK,
# so they can be imported in any (even docker-less) test environment.
# (docker_bench.utils only provides setup_logging — it has no log calls and no
# module-level logger, so it is intentionally excluded.)
DOCKER_SAFE_MODULES = [
    "docker_bench.stats_collector",
]

# Converted modules that import the docker SDK at module load time. Skipped
# when the docker package is unavailable (it is an implicit runtime dep, not
# listed in pyproject [project.dependencies]).
DOCKER_SDK_MODULES = [
    "docker_bench.bench",
    "docker_bench.container_manager",
    "docker_bench.task_runner",
]


class TestSetupLogging:
    """setup_logging must configure root level + a timestamped handler."""

    def test_installs_timestamped_handler_and_sets_level(self):
        """basicConfig adds one StreamHandler with asctime+levelname and sets the level."""
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        try:
            root.handlers.clear()
            setup_logging(logging.DEBUG)

            assert root.level == logging.DEBUG
            assert len(root.handlers) == 1

            handler = root.handlers[0]
            assert isinstance(handler, logging.StreamHandler)

            formatter = handler.formatter
            assert formatter is not None
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

    @pytest.mark.parametrize("module_name", DOCKER_SAFE_MODULES)
    def test_safe_module_defines_named_logger(self, module_name):
        import importlib

        module = importlib.import_module(module_name)
        assert hasattr(module, "logger"), f"{module_name} has no `logger` attribute"
        assert isinstance(module.logger, logging.Logger)
        assert module.logger.name == module_name

    @pytest.mark.parametrize("module_name", DOCKER_SDK_MODULES)
    def test_docker_module_defines_named_logger(self, module_name):
        """Docker-SDK-dependent modules: skip cleanly when docker is absent."""
        pytest.importorskip("docker")
        import importlib

        module = importlib.import_module(module_name)
        assert hasattr(module, "logger"), f"{module_name} has no `logger` attribute"
        assert isinstance(module.logger, logging.Logger)
        assert module.logger.name == module_name


class TestConvertedCodePaths:
    """Verify a converted module logger propagates records to the root handler."""

    def test_stats_collector_logger_emits_info(self, caplog):
        """docker_bench.stats_collector.logger must emit records at the INFO level."""
        import docker_bench.stats_collector as sc

        with caplog.at_level(logging.INFO, logger="docker_bench.stats_collector"):
            sc.logger.info("probe message")

        matching = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(matching) == 1
        assert matching[0].name == "docker_bench.stats_collector"
        assert matching[0].message == "probe message"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
