"""In-memory :class:`EnvironmentProvider` for kernel unit tests.

``FakeProvider`` lets the benchmark kernel be exercised end-to-end with no e2b
or docker backend. Instances live in a dict; ``exec`` returns a canned
:class:`CommandResult` (echoing the command's last token by default, or a
caller-configured result for a given command). It is intentionally simple --
production providers wrap real SDKs; this one wraps nothing.
"""
from __future__ import annotations

from typing import Mapping

from env_provider import CommandResult, CreationMetrics, EnvironmentProvider, SandboxInstance, SandboxStatus


class FakeProvider(EnvironmentProvider):
    """A fake sandbox backend for tests.

    Parameters
    ----------
    count:
        Number of instances ``create_all`` materialises.
    exec_results:
        Optional map from command string to the :class:`CommandResult` to
        return for that exact command. Unmatched commands echo their last
        token on stdout with exit code 0.
    """

    name = "fake"

    def __init__(
        self,
        count: int = 2,
        *,
        exec_results: Mapping[str, CommandResult] | None = None,
    ) -> None:
        self._count = count
        self._exec_results: dict[str, CommandResult] = dict(exec_results or {})
        self._instances: dict[int, SandboxInstance] = {}
        self.cleanup_called = False
        self.prepare_env_calls = 0
        self.prepare_calls = 0
        self.save_ids_calls = 0

    # --- lifecycle ---
    def create_all(self) -> dict[int, SandboxInstance]:
        self._instances = {
            i: SandboxInstance(
                id=f"fake-{i}",
                index=i,
                ready=True,
                is_alive=True,
                creation_metrics=CreationMetrics(status=SandboxStatus.READY),
            )
            for i in range(self._count)
        }
        return dict(self._instances)

    def detect_existing(self) -> dict[int, SandboxInstance]:
        return dict(self._instances)

    def check_alive(self, inst: SandboxInstance) -> bool:
        return inst.is_alive

    def cleanup_all(self) -> None:
        self.cleanup_called = True
        for inst in self._instances.values():
            inst.is_alive = False

    # --- setup hooks (record calls so tests can assert wiring) ---
    def prepare_env(self) -> None:
        self.prepare_env_calls += 1

    def prepare(self, inst: SandboxInstance) -> None:
        self.prepare_calls += 1

    # --- id persistence (default no-op; tests assert the spine calls it) ---
    def save_ids(self, instances, ids_file=None) -> None:  # type: ignore[override]
        self.save_ids_calls += 1

    # --- command exec ---
    def exec(
        self,
        inst: SandboxInstance,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if command in self._exec_results:
            return self._exec_results[command]
        token = command.strip().split()[-1] if command.strip() else ""
        return CommandResult(exit_code=0, stdout=f"{token}\n", stderr="")
