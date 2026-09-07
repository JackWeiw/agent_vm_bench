"""Interrupt / partial-flush behavior for :func:`bench_core.bench.run_benchmark`.

Covers the cooperative-SIGTERM shutdown + unconditional ``finally`` artifact
flush added so an interrupted oversub trial still emits a partial
``run_summary.json`` (and, for lifecycle/trajectory, a partial
``trajectories/index.json``) instead of carrying nothing for a trial that ran
for hours.

Cases:
  1. ``_make_sigterm_handler`` is a reusable module-level utility: sets
     ``stop_event`` AND raises ``KeyboardInterrupt`` (BaseException -> bypasses
     ``except Exception`` -> unwinds straight to ``finally``).
  2. An exception mid-dispatch (exec_only fixed mode) still produces a partial
     ``run_summary.json`` via the ``finally`` gap-fill, with the interruption
     WARNING logged -- proving the driver's primary contract artifact survives
     an interrupt.
  3. An exception mid-dispatch (lifecycle round-robin) still produces a partial
     ``trajectories/index.json`` from the partially-flushed series -- the
     series is line-buffered + flush-per-record, so events written before the
     crash survive and ``export_trajectories`` aggregates them in the
     ``finally``.
  4. Boundary (second-SIGTERM-during-partial-flush): ``_atomic_write_text``
     writes via temp-file + ``os.replace``; a kill between writing the temp and
     the rename leaves the *previously written* file intact rather than torn.
     Completion of the in-flight file is not guaranteed, but already-written
     files are never corrupted.
"""
from __future__ import annotations

import json
import os
import signal
import threading
from pathlib import Path

import pytest

from bench_core.bench import _make_sigterm_handler, run_benchmark
from bench_core.config import KernelConfig
from bench_core.round_robin import RoundRobinTaskManager
from bench_core.task_manager import TaskManager
from bench_core.utils import _atomic_write_text


def _seed_traj_pool(tmp_path: Path) -> None:
    """Two minimal trajectories under tmp_path/traj (FakeProvider exec/lifecycle)."""
    traj = tmp_path / "traj"
    traj.mkdir(exist_ok=True)
    for tid in ("a", "b"):
        (traj / f"{tid}.replay.json").write_text(
            json.dumps({"instance_id": tid, "trajectory": [{"action": f"echo {tid}", "delay_time": 0}]}),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# 1. SIGTERM handler utility (reusable, testable in isolation)
# ---------------------------------------------------------------------------


def test_sigterm_handler_sets_stop_event_and_raises_keyboardinterrupt():
    """The handler cooperatively unwinds: stop_event.set + raise KeyboardInterrupt.

    KeyboardInterrupt is a BaseException, so it bypasses the ``except Exception``
    block in ``run_benchmark`` and propagates straight to the ``finally`` -- the
    reason a bare ``stop_event.set()`` (polled only at round boundaries) is not
    enough and the handler must also raise.
    """
    stop = threading.Event()
    handler = _make_sigterm_handler(stop)
    assert not stop.is_set()
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGTERM, None)
    assert stop.is_set()


# ---------------------------------------------------------------------------
# 2. Exception mid-dispatch -> partial run_summary.json (exec_only)
# ---------------------------------------------------------------------------


def test_partial_run_summary_flushed_on_dispatch_exception(tmp_path, monkeypatch, caplog):
    """An exception during dispatch still yields a partial run_summary.json.

    The ``finally`` gap-fill (``_artifacts_flushed`` False) calls
    ``write_run_summary`` on the partial stats. No trajectory index is written
    here (exec_only has no lifecycle series); the lifecycle case below covers
    index.json. The interruption WARNING is logged so users can distinguish a
    clean run from an interrupted partial save.
    """
    from bench_core.payload.replay_payload import reset_pool_cache
    from env_provider.fake import FakeProvider

    _seed_traj_pool(tmp_path)
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=2,
        benchmark_mode="fixed",
        test_duration=1,
        replay_trajectory_dir=str(tmp_path / "traj"),
        replay_mode="exec_only",
        replay_delay_scale=0.0,
        output_dir=str(tmp_path),
        filename_prefix="irq",
    )

    def _boom(self, *args, **kwargs):
        raise RuntimeError("dispatch boom")

    monkeypatch.setattr(TaskManager, "start_all", _boom)

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError, match="dispatch boom"):
        run_benchmark(cfg, FakeProvider(count=2))

    # The finally partial-flush wrote run_summary.json despite the exception.
    hits = list(tmp_path.glob("irq_run_summary.json"))
    assert len(hits) == 1, f"expected one partial run_summary.json, got {hits}"
    data = json.loads(hits[0].read_text(encoding="utf-8"))
    assert data["workflow_type"] == "replay"
    assert data["replay_mode"] == "exec_only"
    # Partial state: dispatch never started, so 0 trajectories completed.
    assert data["throughput"]["total"] == 2
    assert data["throughput"]["succeeded"] == 0
    # The interruption is surfaced in the log.
    assert any("Trial interrupted, writing partial artifacts" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. Exception mid-dispatch -> partial trajectories/index.json (lifecycle)
# ---------------------------------------------------------------------------


def test_partial_trajectory_index_flushed_on_lifecycle_exception(tmp_path, monkeypatch, caplog):
    """A lifecycle dispatch that crashes after writing some series events still
    produces a partial ``trajectories/index.json`` + ``run_summary.json``.

    The lifecycle series is line-buffered + flush-per-record, so events written
    before the crash are already on disk; the ``finally``'s
    ``export_trajectories`` aggregates them into ``index.json`` even though the
    happy-path step 8 never ran.
    """
    from bench_core.payload.replay_payload import reset_pool_cache
    from env_provider.tests.lifecycle_fake import FakeLifecycleProvider

    _seed_traj_pool(tmp_path)
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=4,
        replay_running_concurrency=2,
        benchmark_mode="round_robin",
        round_size=4,
        round_count=1,
        round_interval=0,
        test_duration=2,
        replay_trajectory_dir=str(tmp_path / "traj"),
        replay_mode="lifecycle",
        replay_delay_scale=0.0,
        replay_control_plane_qps=1000.0,
        output_dir=str(tmp_path),
        filename_prefix="lc",
    )

    def _run_partial_then_die(self):
        """Simulate dispatch writing two partial trajectory step events, then dying.

        The real runner flushes each event on write, so these survive the crash
        without ``close()``. ``self.series`` is the ``LifecycleSeriesWriter``
        passed into ``RoundRobinTaskManager`` (see round_robin.py:57).
        """
        if self.series is not None:
            for tid in ("traj-a", "traj-b"):
                self.series.write(
                    {
                        "event": "step",
                        "sandbox_index": 0,
                        "trajectory_id": tid,
                        "step_index": 0,
                        "action_type": "shell",
                        "resume_start": None,
                        "exec_start": None,
                        "exec_end": None,
                        "pause_start": None,
                        "pause_end": None,
                        "resume_end": None,
                        "resume_sec": 0.1,
                        "exec_sec": 0.4,
                        "pause_sec": 0.2,
                        "slice_total_sec": 0.7,
                        "interaction_total_sec": 0.75,
                        "slot_contention_wait_sec": 0.0,
                        "resume_queue_wait_sec": 0.0,
                        "resume_api_sec": 0.0,
                        "resume_ready_wait_sec": 0.0,
                        "pause_queue_wait_sec": 0.0,
                        "pause_api_sec": 0.0,
                        "running_slot_held_sec": 0.0,
                        "slice_failed": False,
                        "timed_out": False,
                        "exit_code": 0,
                    }
                )
        raise RuntimeError("lifecycle dispatch boom")

    monkeypatch.setattr(RoundRobinTaskManager, "run", _run_partial_then_die)

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError, match="lifecycle dispatch boom"):
        run_benchmark(cfg, FakeLifecycleProvider(count=4))

    # Partial trajectory index written from the flushed series events.
    index = tmp_path / "trajectories" / "index.json"
    assert index.exists(), "partial trajectories/index.json was not flushed"
    idx = json.loads(index.read_text(encoding="utf-8"))
    assert idx["n_trajectories"] == 2
    tids = {row["trajectory_id"] for row in idx["trajectories"]}
    assert tids == {"traj-a", "traj-b"}

    # Partial run_summary written too.
    hits = list(tmp_path.glob("lc_run_summary.json"))
    assert len(hits) == 1
    data = json.loads(hits[0].read_text(encoding="utf-8"))
    assert data["replay_mode"] == "lifecycle"
    assert data["paths"]["trajectory_index"] is not None
    assert any("Trial interrupted, writing partial artifacts" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. Boundary: second SIGTERM during partial flush must not corrupt prior files
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_prior_file_intact_on_mid_replace_kill(tmp_path, monkeypatch):
    """A second SIGTERM during the partial flush cannot be cooperatively handled.

    What we CAN guarantee: files already renamed (``os.replace``'d) are durable
    and never torn. ``_atomic_write_text`` writes to a temp file, flushes+fsyncs,
    then atomically renames. A kill between the temp write and the rename leaves
    the destination at its *previous* state (or absent if none), never half-written.
    This models the second-SIGTERM-during-flush boundary: completion of the
    in-flight file is not guaranteed, but already-written files are not corrupted.
    """
    # File A is fully written (rename succeeds) -> durable on disk.
    a = tmp_path / "a.json"
    _atomic_write_text(a, json.dumps({"file": "A", "v": 1}))
    assert json.loads(a.read_text(encoding="utf-8"))["v"] == 1

    # Model the kill as arriving between the temp-file write+fsync and the
    # atomic rename. The first write of A above ran on the real os.replace
    # (before patching), so it is durable on disk; every patched rename from
    # here on is killed (the kill lands on whichever file is mid-rename at
    # that instant). Completion of the in-flight file is not guaranteed;
    # already-written files must not be corrupted.
    b = tmp_path / "b.json"

    def _kill_patched_rename(src, dst, *args, **kwargs):
        raise KeyboardInterrupt("second SIGTERM during flush")

    monkeypatch.setattr(os, "replace", _kill_patched_rename)

    # Rename #2: B's rename is killed -> B never lands (absent), A intact.
    with pytest.raises(KeyboardInterrupt):
        _atomic_write_text(b, json.dumps({"file": "B", "v": 1}))
    assert not b.exists()
    assert json.loads(a.read_text(encoding="utf-8"))["v"] == 1

    # Rename #3: overwriting A with v=2 is killed mid-rename -> A must still
    # hold its PREVIOUS (v=1) content, not a torn/partial v=2.
    with pytest.raises(KeyboardInterrupt):
        _atomic_write_text(a, json.dumps({"file": "A", "v": 2}))
    assert json.loads(a.read_text(encoding="utf-8"))["v"] == 1


# ---------------------------------------------------------------------------
# 5. export_trajectories throw on the happy path must not abort the run
# ---------------------------------------------------------------------------


def test_trajectory_export_failure_does_not_abort_run(tmp_path, monkeypatch, caplog):
    """A throw from ``export_trajectories`` on the happy path must not crash
    the run or skip ``run_summary.json``.

    Previously the happy-path call was unguarded; an exception propagated to
    ``except Exception`` -> ``raise`` -> ``finally``, which crashed
    ``run_benchmark`` AFTER dispatch completed. That turned a non-critical
    catalog-artifact failure (replay_result.json is a browsable extra, not the
    driver contract) into an invalid oversub trial (non-zero exit). The guard
    logs + continues, so the happy path still writes ``run_summary.json`` and
    returns normally.

    Uses lifecycle round-robin (not exec_only) because lifecycle writes step
    events to the series, so the series file exists at step 8 and the guarded
    ``export_trajectories`` call is actually reached (and patched to throw).
    """
    from bench_core.payload.replay_payload import reset_pool_cache
    from env_provider.tests.lifecycle_fake import FakeLifecycleProvider
    from bench_core.observability import trajectory_export

    _seed_traj_pool(tmp_path)
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=4,
        replay_running_concurrency=2,
        benchmark_mode="round_robin",
        round_size=4,
        round_count=1,
        round_interval=0,
        test_duration=2,
        replay_trajectory_dir=str(tmp_path / "traj"),
        replay_mode="lifecycle",
        replay_delay_scale=0.0,
        replay_control_plane_qps=1000.0,
        output_dir=str(tmp_path),
        filename_prefix="exp",
    )

    def _boom_export(*args, **kwargs):
        raise RuntimeError("export boom")

    monkeypatch.setattr(trajectory_export, "export_trajectories", _boom_export)

    with caplog.at_level("ERROR"):
        result = run_benchmark(cfg, FakeLifecycleProvider(count=4))  # must NOT raise

    # The run completed normally (clean return, not a crash that would mark
    # an oversub trial invalid via non-zero exit).
    assert result is not None
    # run_summary.json was still written on the happy path -- the export
    # failure did not skip the downstream write_run_summary call.
    hits = list(tmp_path.glob("exp_run_summary.json"))
    assert len(hits) == 1, f"expected run_summary.json despite export failure, got {hits}"
    # The export failure is surfaced in the log, not swallowed silently.
    assert any("trajectory export failed" in r.message for r in caplog.records)
