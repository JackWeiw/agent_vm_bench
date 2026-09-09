"""Smoke matrix A-F: the four independent wait components are observable
through the runner under each knob/oversub combination, and the sum invariants
hold. FakeLifecycleProvider + real ``QpsRateLimiter`` / ``RunningSlotScheduler``;
deterministic where possible (future-deadline injection / real inter-step
delay), barrier-synchronized concurrency where the wait only manifests under
contention (inflight fuse / FIFO).

Cases (per the wait-decomposition directive):

  A  qps=None   inflight=None  1:1     -> all four waits 0 (admission is None)
  B  qps=20     inflight=None  1:1     -> rate_pacing>0, inflight=0, capacity=0
  C  qps=None   inflight=2     3 @ rc -> inflight>0 (fuse saturates), rate=0
  D  qps=20     inflight=2     3 @ rc -> rate_pacing>0; both knobs plumbed
  E  qps=None   inflight=None  2 @ 1   -> capacity>0 (FIFO oversub), rate=0
  F  qps=None   inflight=None  +delay   -> natural_delay>0, others 0
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from bench_core.admission import Admission, QpsRateLimiter, RunningSlotScheduler
from bench_core.config import KernelConfig
from bench_core.payload.replay_payload import ReplayStep
from bench_core.schemas import BenchSandbox
from bench_core.task_runner.replay import ReplayRoundRunner
from env_provider import SandboxInstance
from env_provider.tests.lifecycle_fake import FakeLifecycleProvider

REPLAY_FIXTURES = Path(__file__).parent.parent / "fixtures" / "replay"


def _config(tmp_path, **kw):
    base = dict(
        workflow_type="replay",
        total_count=1,
        benchmark_mode="fixed",
        test_duration=1,
        replay_trajectory_dir=str(REPLAY_FIXTURES),
        replay_mode="lifecycle",
        replay_delay_scale=0.0,
        replay_ready_probe=False,
        output_dir=str(tmp_path),
        filename_prefix="smoke",
    )
    base.update(kw)
    return KernelConfig(**base)


def _state(i):
    s = BenchSandbox.from_instance(SandboxInstance(id=f"x{i}", index=i), workflow_type="replay")
    s.ready = True
    return s


def _rate_pacing(sr):
    """Rate-pacing total = resume + pause 1/qps time-waits."""
    return sr.resume_rate_pacing_wait_sec + sr.pause_rate_pacing_wait_sec


def _inflight(sr):
    """Inflight-fuse total = resume + pause semaphore-block waits."""
    return sr.resume_inflight_wait_sec + sr.pause_inflight_wait_sec


def _assert_invariants(sr):
    """The wait-decoupling sum invariants must hold for every slice."""
    assert abs(sr.resume_sec - (sr.resume_inflight_wait_sec + sr.resume_api_sec + sr.resume_ready_wait_sec)) < 1e-6
    assert abs(sr.pause_sec - (sr.pause_rate_pacing_wait_sec + sr.pause_inflight_wait_sec + sr.pause_api_sec)) < 1e-6
    assert abs(sr.slice_total_sec - (sr.resume_sec + sr.exec_elapsed_sec + sr.pause_sec)) < 1e-6
    assert abs(sr.slot_contention_wait_sec - (sr.natural_delay_sec + sr.capacity_wait_sec)) < 1e-6


class TestSmokeMatrixSingle:
    """Deterministic single-runner cases (no concurrency timing races)."""

    def test_A_knobs_off_all_waits_zero(self, tmp_path):
        """A: qps=None, inflight=None, 1:1 -> admission is None -> all four wait
        components are 0; the sum invariants hold vacuously."""
        config = _config(tmp_path)
        provider = FakeLifecycleProvider(count=1)
        stop = threading.Event()
        runner = ReplayRoundRunner(_state(0), config, stop, round_id=0, provider=provider)
        runner._init_lifecycle()
        step = ReplayStep(index=0, action_type="shell", action="true", delay_time_sec=0.0)
        sr = runner._run_slice(step, trajectory_id="t0")
        assert _rate_pacing(sr) == 0.0
        assert _inflight(sr) == 0.0
        assert sr.capacity_wait_sec == 0.0
        assert sr.natural_delay_sec == 0.0
        _assert_invariants(sr)

    def test_B_qps_only_rate_pacing_nonzero(self, tmp_path):
        """B: qps set, inflight=None -> rate_pacing flows into StepResult
        (deterministic via future-deadline injection so the first time_wait
        sleeps); inflight=0 (fuse bypassed); capacity=0 (single runner)."""
        config = _config(tmp_path)
        provider = FakeLifecycleProvider(count=1)
        stop = threading.Event()
        qps = QpsRateLimiter(qps=20.0, inflight_cap=None)
        adm = Admission(slots=RunningSlotScheduler(maximum=1), qps=qps)
        runner = ReplayRoundRunner(_state(0), config, stop, round_id=0, provider=provider, admission=adm)
        runner._init_lifecycle()
        # Inject a future dispatch deadline so the first resume time_wait sleeps
        # (the limiter seeds _next_dispatch_at = construction time -> 0 on the
        # first call). interval=0.05s slides the deadline forward, so the pause
        # time_wait that follows also lands in the future.
        qps._next_dispatch_at = time.monotonic() + 0.04
        step = ReplayStep(index=0, action_type="shell", action="true", delay_time_sec=0.0)
        sr = runner._run_slice(step, trajectory_id="t0")
        assert _rate_pacing(sr) > 0.0, "rate pacing should be non-zero"
        assert _inflight(sr) < 0.001, "inflight fuse bypassed (cap=None -> no-op)"
        assert sr.capacity_wait_sec < 0.001  # slots.acquire runs but is uncontended (sub-ms overhead)
        assert sr.natural_delay_sec == 0.0
        _assert_invariants(sr)

    def test_F_natural_delay_nonzero(self, tmp_path):
        """F: inter-step pause_duration -> the 2nd slice's ready_at is in the
        future, so slots.acquire parks (natural_delay>0). Other waits 0."""
        config = _config(tmp_path, replay_pause_duration_sec=0.05)
        provider = FakeLifecycleProvider(count=1)
        stop = threading.Event()
        # Slots present (so acquire runs and measures natural_delay), no QPS fuse.
        adm = Admission(slots=RunningSlotScheduler(maximum=1), qps=None)
        runner = ReplayRoundRunner(_state(0), config, stop, round_id=0, provider=provider, admission=adm)
        runner._init_lifecycle()
        # 1st slice: _prev_pause_end_monotonic is None -> ready_at=None -> no
        # pre-delay park; it seeds prev_pause_end for the 2nd slice.
        runner._run_slice(
            ReplayStep(index=0, action_type="shell", action="true", delay_time_sec=0.0), trajectory_id="t0"
        )
        # 2nd slice: ready_at = prev_pause_end + 0.05 (future) -> natural_delay.
        sr1 = runner._run_slice(
            ReplayStep(index=1, action_type="shell", action="true", delay_time_sec=0.0), trajectory_id="t0"
        )
        assert sr1.natural_delay_sec > 0.0, "natural delay should be non-zero on the 2nd slice"
        assert _rate_pacing(sr1) == 0.0
        assert _inflight(sr1) == 0.0
        assert sr1.capacity_wait_sec < 0.001  # uncontended slots.acquire, sub-ms overhead
        _assert_invariants(sr1)


def _run_concurrent(n, *, adm, config, provider, delay_time=0.0):
    """Run n single-slice runners sharing one admission; barrier-synchronized
    start so inflight/FIFO contention is observable. Returns the n StepResults."""
    stop = threading.Event()
    results: list = [None] * n
    barrier = threading.Barrier(n + 1)  # +1 for the main thread release

    def _work(i):
        runner = ReplayRoundRunner(_state(i), config, stop, round_id=0, provider=provider, admission=adm)
        runner._init_lifecycle()
        barrier.wait()  # all sandboxes paused -> simultaneous slice start
        step = ReplayStep(index=0, action_type="shell", action="true", delay_time_sec=delay_time)
        results[i] = runner._run_slice(step, trajectory_id=f"t{i}")

    threads = [threading.Thread(target=_work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    barrier.wait()  # release the workers simultaneously
    for t in threads:
        t.join()
    return results


class TestSmokeMatrixConcurrent:
    """Barrier-synchronized multi-runner cases (waits that only manifest under
    contention). Inequality + structural assertions; tolerant bounds."""

    def test_C_inflight_only_fuse_blocks(self, tmp_path):
        """C: qps=None, inflight_cap=2, 3 runners @ running_concurrency=3 -> the
        fuse saturates (2 concurrent resume, each held ~0.02s); the 3rd blocks
        -> max(inflight_wait)>0. rate_pacing=0 (qps=None), capacity=0 (no oversub)."""
        config = _config(tmp_path)
        provider = FakeLifecycleProvider(count=3)
        adm = Admission(
            slots=RunningSlotScheduler(maximum=3),  # running_concurrency == n -> no FIFO contention
            qps=QpsRateLimiter(qps=None, inflight_cap=2),
        )
        results = _run_concurrent(3, adm=adm, config=config, provider=provider)
        assert len(results) == 3
        # At least one runner blocked on the inflight fuse (cap=2, 3 concurrent
        # resume calls; each holds the permit for the ~0.02s provider.resume).
        assert max(_inflight(sr) for sr in results) > 0.001, "inflight fuse should block the 3rd resume"
        # Rate pacing bypassed (qps=None -> time_wait is a no-op).
        assert all(_rate_pacing(sr) == 0.0 for sr in results)
        # No FIFO contention (running_concurrency == n, single slice each), but
        # slots.acquire still runs -> tolerate its sub-ms measurement overhead.
        assert all(sr.capacity_wait_sec < 0.001 for sr in results)
        for sr in results:
            _assert_invariants(sr)

    def test_D_both_knobs_plumbed_and_rate_pacing_nonzero(self, tmp_path):
        """D: qps + inflight_cap both set -> admission constructed with both;
        no crash; rate_pacing flows (2nd/3rd resume pay the 1/qps spacing,
        interval=0.05s >> jitter). Structural: all four wait fields present on
        every StepResult; the shared limiter snapshot reports both knobs."""
        config = _config(tmp_path)
        provider = FakeLifecycleProvider(count=3)
        qps = QpsRateLimiter(qps=20.0, inflight_cap=2)
        adm = Admission(slots=RunningSlotScheduler(maximum=3), qps=qps)
        results = _run_concurrent(3, adm=adm, config=config, provider=provider)
        assert len(results) == 3
        # Rate pacing: the 1st resume pays 0 (deadline ~= construction); the 2nd
        # and 3rd pay the 1/qps spacing (interval 0.05s, >> scheduling jitter).
        assert max(_rate_pacing(sr) for sr in results) > 0.001, "rate pacing should delay 2nd/3rd resume"
        # Structural: every StepResult carries the four wait-component fields.
        for sr in results:
            assert hasattr(sr, "resume_rate_pacing_wait_sec")
            assert hasattr(sr, "pause_rate_pacing_wait_sec")
            assert hasattr(sr, "resume_inflight_wait_sec")
            assert hasattr(sr, "natural_delay_sec")
            assert hasattr(sr, "capacity_wait_sec")
            _assert_invariants(sr)
        # The shared limiter reports both knobs (decoupled construction: both active).
        snap = qps.snapshot()
        assert snap["qps"] == 20.0
        assert snap["inflight_cap"] == 2

    def test_E_oversub_capacity_contention(self, tmp_path):
        """E: qps=None, inflight=None, 2 runners @ running_concurrency=1 -> FIFO
        oversub: the 2nd runner blocks on the running slot until the 1st's slice
        completes -> max(capacity_wait)>0. rate_pacing=0, inflight=0."""
        config = _config(tmp_path)
        provider = FakeLifecycleProvider(count=2)
        adm = Admission(slots=RunningSlotScheduler(maximum=1), qps=None)
        results = _run_concurrent(2, adm=adm, config=config, provider=provider)
        assert len(results) == 2
        # One runner acquired immediately (capacity_wait ~ 0); the other blocked
        # until the first's slice completed (~0.04s resume+exec+pause).
        assert max(sr.capacity_wait_sec for sr in results) > 0.001, "FIFO should block the 2nd runner"
        assert all(_rate_pacing(sr) == 0.0 for sr in results)
        assert all(_inflight(sr) == 0.0 for sr in results)
        assert all(sr.natural_delay_sec == 0.0 for sr in results)
        for sr in results:
            _assert_invariants(sr)
