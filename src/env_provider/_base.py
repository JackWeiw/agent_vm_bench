"""Shared backend infrastructure: backend status enum, creation metrics, and the
lifecycle template the e2b/docker managers inherit.

Two things are identical across the e2b and docker backends and therefore live
here rather than in each backend's package:

1. **Backend status + creation metrics.** Both backends track the same creation
   states (PENDING/CREATING/CREATED/PORT_READY/.../KILLED) and the same timing
   fields (submit_time, port_ready_time, port_wait_elapsed, ...). The
   workflow-neutral contract status in :mod:`env_provider` (READY/READY_FAILED)
   is a *different* enum the adapters translate into via ``_STATUS_MAP``.

2. **The lifecycle skeleton.** ``create_all`` (batched vs concurrent dispatch),
   ``_create_batched`` (batch loop + stop-event + inter-batch sleep),
   ``_create_batch_concurrent`` (ThreadPoolExecutor + post-create result→status
   mapping driven by :class:`ReadyChecker`), ``detect_existing`` (list → attach →
   ready-check → set status), and ``cleanup_all`` (iterate → kill → set flags)
   are byte-for-byte identical except for the SDK calls. The base owns them; a
   subclass supplies the SDK seams via a handful of abstract methods + class
   attrs.

Readiness itself is delegated to :class:`env_provider._ready.ReadyChecker`,
constructed by the base from the subclass's ``_exec_probe`` and the
provider-transparent ``_ready_config`` (shared workflow constants).
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from typing import Any, Protocol

from env_provider._ready import BROWSER_REQUIRED_PORTS, READY_INTERVAL, READY_MAX_WAIT, ReadyChecker

logger = logging.getLogger(__name__)


class BackendSandboxStatus(Enum):
    """Backend-granular sandbox status (shared by e2b and docker).

    More granular than the contract's workflow-neutral
    :class:`env_provider.SandboxStatus` (which collapses PORT_READY→READY,
    PORT_FAILED→READY_FAILED): the manager sets these during creation/detect,
    and the adapter's ``_STATUS_MAP`` (keyed by ``.value``) translates out.
    """

    PENDING = "pending"  # Waiting for creation
    CREATING = "creating"  # Creating in progress
    CREATED = "created"  # create succeeded, waiting for readiness probe
    PORT_READY = "port_ready"  # Readiness probe passed, can execute tasks
    ACTIVE = "active"  # Active, executing tasks
    FAILED = "failed"  # Creation failed
    PORT_FAILED = "port_failed"  # Readiness probe failed
    OFFLINE = "offline"  # Runtime offline
    KILLED = "killed"  # Killed/removed


@dataclass
class BackendCreationMetrics:
    """Backend creation/readiness timing (shared by e2b and docker).

    The adapter maps ``port_ready_time``→contract ``ready_time`` and
    ``port_wait_elapsed``→``ready_check_elapsed``.
    """

    submit_time: float = 0.0  # Creation submit time
    create_ready_time: float = 0.0  # create success time (excluding readiness wait)
    port_ready_time: float = 0.0  # Readiness probe passed time
    create_elapsed: float = 0.0  # create elapsed time (seconds)
    port_wait_elapsed: float = 0.0  # Readiness-wait elapsed time (seconds)
    total_elapsed: float = 0.0  # Total = create_elapsed + port_wait_elapsed
    status: BackendSandboxStatus = BackendSandboxStatus.PENDING
    error_msg: str = ""
    port_check_error: str = ""  # Readiness-probe error message


class BackendState(Protocol):
    """The per-sandbox state fields the base lifecycle touches.

    Both :class:`env_provider.e2b.schemas.SandboxState` and
    :class:`env_provider.docker.schemas.ContainerState` satisfy this
    structurally. The handle is reached via ``setattr``/``getattr`` on
    :attr:`BaseSandboxManager._handle_attr` (the handle attr name differs:
    ``sandbox_obj`` vs ``docker_container``), so it is not on this Protocol.
    """

    creation_metrics: BackendCreationMetrics
    is_alive: bool
    stopped_by_cleanup: bool


class BaseSandboxManager(ABC):
    """Lifecycle template for a backend sandbox manager.

    Owns the workflow-agnostic create/detect/cleanup skeleton. A subclass
    supplies the backend SDK seams (abstract methods) + a few class attrs.
    Readiness is delegated to a :class:`ReadyChecker` built from the subclass's
    ``_exec_probe`` and the base's provider-transparent ``_ready_config``
    (shared workflow constants -- no per-backend readiness knobs).
    """

    # State attribute name on the backend State (``sandbox_obj`` / ``docker_container``).
    _handle_attr: str = ""
    # Log noun (``"Sandbox"`` / ``"Container"``).
    _noun: str = "Sandbox"
    # Identifier attribute name on the backend State (``sandbox_id`` /
    # ``container_id``), used for log labels when only the state (not the index)
    # is in hand.
    _id_attr: str = "sandbox_id"
    # Whether cleanup_all sets status=KILLED. e2b is False (keeps the original
    # creation status for stats); docker is True.
    _set_killed_on_cleanup: bool = False

    def __init__(self, kernel_config: Any, stop_event: Event) -> None:
        self.kernel_config = kernel_config
        self.stop_event = stop_event
        self.kernel_config.validate()
        self._states: dict[int, BackendState] = {}
        self._ready: ReadyChecker | None = None  # built lazily by _ready_checker()

    # ------------------------------------------------------------------ ready
    def _ready_checker(self) -> ReadyChecker:
        """The lazily-built :class:`ReadyChecker` (uses subclass seams)."""
        if self._ready is None:
            max_wait, interval, ports = self._ready_config()
            self._ready = ReadyChecker(
                self.stop_event,
                self._exec_probe,
                max_wait=max_wait,
                interval=interval,
                ports=ports,
            )
        return self._ready

    # ------------------------------------------------------------- create_all
    def create_all(self) -> dict[int, BackendState]:
        """Batched (if ``create_batch_size`` set) or full-concurrent creation."""
        if self.kernel_config.create_batch_size and self.kernel_config.create_batch_size > 0:
            return self._create_batched()
        return self._create_concurrent()

    def _create_batched(self) -> dict[int, BackendState]:
        total = self.kernel_config.total_count
        batch_size = self.kernel_config.create_batch_size
        batch_count = self.kernel_config.create_batch_count

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Batched {self._noun} Creation")
        for extra in self._create_header_extras():
            logger.info(f"  {extra}")
        logger.info(f"  Total: {total} {self._noun.lower()}s")
        logger.info(f"  Batches: {batch_count} x {batch_size}")
        logger.info(f"  Interval: {self.kernel_config.create_batch_interval}s")
        logger.info(f"{'=' * 60}")

        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                logger.info("Stop event detected, aborting creation")
                break

            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)
            logger.info(
                f"\n[Batch {batch_id}/{batch_count - 1}] " f"Creating {self._noun.lower()}s {start_idx + 1}-{end_idx}"
            )

            # Concurrent creation of the current batch (mutates self._states in
            # place -- _create_batch_concurrent writes each state before submit).
            self._create_batch_concurrent(batch_id, start_idx, end_idx)

            if batch_id < batch_count - 1 and self.kernel_config.create_batch_interval:
                logger.info(f"Waiting {self.kernel_config.create_batch_interval}s before next batch...")
                time.sleep(self.kernel_config.create_batch_interval)

        return self._states

    def _create_batch_concurrent(self, batch_id: int, start: int, end: int) -> dict[int, BackendState]:
        states: dict[int, BackendState] = {}

        with ThreadPoolExecutor(max_workers=end - start) as executor:
            futures = {}
            for i in range(start, end):
                index = i + 1
                state = self._new_state(index, batch_id=batch_id)
                self._states[index] = state
                future = executor.submit(self._create_single, state)
                futures[future] = index

            for future in as_completed(futures):
                index = futures[future]
                state = self._states[index]
                label = f"{self._noun}{index}"

                try:
                    result = future.result()
                    if result["success"]:
                        logger.info(f"[{label}] Created in {result['create_elapsed']:.1f}s, checking ready...")
                        ready = self._ready_checker().check(
                            self._handle_of(state),
                            self.kernel_config.workflow_type,
                            label,
                        )
                        self._apply_ready(state, ready, create_elapsed=result["create_elapsed"])
                    else:
                        state.creation_metrics.status = BackendSandboxStatus.FAILED
                        state.creation_metrics.error_msg = result["error"]
                        logger.error(f"[{label}] Failed: {result['error'][:80]}")
                except Exception as e:
                    state.creation_metrics.status = BackendSandboxStatus.FAILED
                    state.creation_metrics.error_msg = str(e)
                    logger.error(f"[{label}] Exception: {str(e)[:80]}")

        return {i + 1: self._states[i + 1] for i in range(start, end)}

    def _create_concurrent(self) -> dict[int, BackendState]:
        total = self.kernel_config.total_count
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Concurrent {self._noun} Creation")
        for extra in self._create_header_extras():
            logger.info(f"  {extra}")
        logger.info(f"  Total: {total} {self._noun.lower()}s (full concurrent)")
        logger.info(f"{'=' * 60}")
        return self._create_batch_concurrent(batch_id=0, start=0, end=total)

    # ---------------------------------------------------------- detect_existing
    def detect_existing(self) -> dict[int, BackendState]:
        """List running sandboxes, attach, ready-check, set status."""
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Detecting Existing {self._noun}s")
        logger.info(f"{'=' * 60}")

        try:
            listed = self._list_existing()
            logger.info(f"  Found {len(listed)} running {self._noun.lower()}s")
        except Exception as e:
            logger.error(f"  Failed to list {self._noun.lower()}s: {e}")
            return {}

        if not listed:
            logger.info(f"  No existing {self._noun.lower()}s found")
            return {}

        return self._detect_each(listed, word="all")

    def _detect_each(self, listed: list, *, word: str = "matched") -> dict[int, BackendState]:
        """Per-item detect loop: attach + ready-check + status mapping.

        Shared by :meth:`detect_existing` (``word="all"``) and a backend's own
        detect variant: e2b's ``detect_from_file`` filters the running list
        itself, then calls this with ``word="matched"``. Reuses
        :meth:`_apply_ready` (``create_elapsed=None`` -- no creation timing on
        detect) so the create and detect paths share one ready->status mapping.
        """
        logger.info(f"  Processing {word}...")
        for i, item in enumerate(listed):
            index = i + 1
            ext_id = self._external_id(item)
            label = f"{self._noun}{index}"
            state = self._new_state(index, external_id=ext_id)
            self._states[index] = state

            logger.info(f"\n[{label}] {ext_id}...")

            try:
                handle = self._attach(item)
                setattr(state, self._handle_attr, handle)
                state.creation_metrics.status = BackendSandboxStatus.CREATED
                logger.info(f"[{label}] Attached successfully")

                ready = self._ready_checker().check(handle, self.kernel_config.workflow_type, label)
                self._apply_ready(state, ready)
            except Exception as e:
                state.creation_metrics.status = BackendSandboxStatus.FAILED
                state.creation_metrics.error_msg = str(e)
                logger.error(f"[{label}] {str(e)[:80]}")
        return self._states

    # ------------------------------------------------------------- cleanup_all
    def cleanup_all(self) -> None:
        """Kill/remove every tracked sandbox (e2b kill_all / docker remove_all)."""
        logger.info(f"\nKilling all {self._noun.lower()}s...")
        killed = 0
        for state in self._states.values():
            handle = self._handle_of(state)
            if not handle:
                continue
            try:
                was_alive = state.is_alive
                self._kill_one(state)
                state.is_alive = False
                # Only explain the transition when the sandbox was alive before
                # this intentional kill (preserve genuine offline states).
                if was_alive:
                    state.stopped_by_cleanup = True
                if self._set_killed_on_cleanup:
                    state.creation_metrics.status = BackendSandboxStatus.KILLED
                killed += 1
            except Exception as e:
                logger.warning(f"[{self._label(state)}] Kill error: {str(e)[:50]}")
        logger.info(f"Killed {killed} {self._noun.lower()}s")

    # ----------------------------------------------------------- cleanup_existing
    def cleanup_existing(self) -> int:
        """List running sandboxes and kill them all (standalone ``--cleanup``).

        Unlike :meth:`cleanup_all` (which kills only sandboxes tracked in this
        manager's ``_states``), this lists live sandboxes fresh, attaches each,
        and kills it -- so it tears down sandboxes a prior ``--create-only`` /
        ``--detect`` run left running. The readiness probe is deliberately
        skipped: we are tearing them down, not running tasks, so a dead sandbox
        or a browser container whose services never came up must not stall the
        teardown on the port/command wait. Reuses the same SDK seams
        (``_list_existing`` / ``_external_id`` / ``_attach`` / ``_kill_one``) as
        detect + cleanup, so no new abstract method.

        Returns the number of sandboxes actually torn down.
        """
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Cleaning up existing {self._noun.lower()}s")
        logger.info(f"{'=' * 60}")
        try:
            listed = self._list_existing()
            logger.info(f"  Found {len(listed)} running {self._noun.lower()}s")
        except Exception as e:
            logger.error(f"  Failed to list {self._noun.lower()}s: {e}")
            return 0

        if not listed:
            logger.info(f"  No existing {self._noun.lower()}s found")
            return 0

        killed = 0
        for i, item in enumerate(listed, start=1):
            ext_id = self._external_id(item)
            label = f"{self._noun}{i}"
            try:
                handle = self._attach(item)
                # _kill_one reads the handle off a state via _handle_attr, so
                # build a throwaway state and set the handle on it. _new_state's
                # other fields are unused on the kill path.
                state = self._new_state(i, external_id=ext_id)
                setattr(state, self._handle_attr, handle)
                self._kill_one(state)
                killed += 1
                logger.info(f"[{label}] Killed {ext_id}")
            except Exception as e:
                logger.warning(f"[{label}] Kill error: {str(e)[:80]}")
        logger.info(f"Killed {killed} of {len(listed)} {self._noun.lower()}s")
        return killed

    # ----------------------------------------------------------------- helpers
    def _handle_of(self, state: BackendState) -> Any:
        """The SDK handle on a state (``sandbox_obj`` / ``docker_container``)."""
        return getattr(state, self._handle_attr, None)

    def _label(self, state: BackendState) -> str:
        """Log label for a state (``Sandbox3`` / ``Container2``) via ``_id_attr``."""
        return f"{self._noun}{getattr(state, self._id_attr, '?')}"

    def _apply_ready(self, state: BackendState, ready: dict, *, create_elapsed: float | None = None) -> None:
        """Map a ReadyChecker result onto a state's creation_metrics.

        On the create path pass ``create_elapsed`` so ``total_elapsed`` is set
        and the log line includes the total; on the detect path leave it None
        (no creation timing) and only the readiness wait is recorded.
        """
        label = self._label(state)
        if ready["success"]:
            state.creation_metrics.status = BackendSandboxStatus.PORT_READY
            state.creation_metrics.port_wait_elapsed = ready["wait_elapsed"]
            state.creation_metrics.port_ready_time = time.time()
            if create_elapsed is not None:
                state.creation_metrics.total_elapsed = create_elapsed + ready["wait_elapsed"]
                logger.info(
                    f"[{label}] Ready in {ready['wait_elapsed']:.1f}s, "
                    f"total {state.creation_metrics.total_elapsed:.1f}s"
                )
            else:
                logger.info(f"[{label}] Ready in {ready['wait_elapsed']:.1f}s")
        else:
            state.creation_metrics.status = BackendSandboxStatus.PORT_FAILED
            state.creation_metrics.port_check_error = ready["error"]
            logger.warning(f"[{label}] Ready check failed: {ready['error'][:50]}")

    # --------------------------------------------------------- subclass seams
    @abstractmethod
    def _new_state(self, index: int, *, batch_id: int = -1, external_id: str = "") -> BackendState:
        """Build a fresh per-sandbox State (backend dataclass)."""

    @abstractmethod
    def _create_single(self, state: BackendState) -> dict:
        """Backend SDK create. Returns ``{success, create_elapsed, error}`` and
        must set the handle on ``state`` + the creation timing fields."""

    @abstractmethod
    def _list_existing(self) -> list:
        """List running sandboxes (e2b paginator flattened; docker prefix-filtered)."""

    @abstractmethod
    def _external_id(self, listed: Any) -> str:
        """The stable external id of a listed sandbox (e2b sandbox_id / docker name)."""

    @abstractmethod
    def _attach(self, listed: Any) -> Any:
        """Obtain the SDK handle for a listed sandbox. docker returns the listed
        container; e2b connects via ``Sandbox.connect``."""

    @abstractmethod
    def _kill_one(self, state: BackendState) -> None:
        """Backend SDK kill/remove of one sandbox's handle."""

    @abstractmethod
    def _exec_probe(self, handle: Any, cmd: str, timeout: int) -> tuple[int, str, str]:
        """Run a probe command in a sandbox handle -> (exit_code, stdout, stderr)."""

    def _ready_config(self) -> tuple[int, int, list[int]]:
        """Ready-check tuning: (max_wait, interval, ports).

        Provider-transparent: readiness is a workflow concern (browser ports
        / coding uname / document validate -- see :class:`ReadyChecker`), so the
        tuning is the same shared constants for every backend. A backend with a
        genuine reason to differ (e.g. a slow-to-boot image) may override, but
        e2b and docker do not -- hence this lives on the base, not as an
        abstract seam. ``ports`` are only consulted by the browser probe.
        """
        return (READY_MAX_WAIT, READY_INTERVAL, [p for p, _ in BROWSER_REQUIRED_PORTS])

    def _create_header_extras(self) -> list[str]:
        """Extra header lines for the create banner (docker: image/spec; e2b: none)."""
        return []
