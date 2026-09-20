"""Report surfaces for the replay workflow.

Split out of ``replay.py``: pure presentation over ``ReplayMetrics`` -- reads
the metrics accumulators and the admission snapshot, never touches runner
control flow. ``replay.py`` imports this for the ``register_workflow`` call.
"""
from __future__ import annotations

import statistics
from typing import Any

from bench_core.observability.replay_obs import ReplayObservability
from bench_core.observability.report_helpers import (
    CODING_ERROR_DISPLAY,
    MIN_SLICE_SEC,
    TableFormatter,
    replay_pool_size,
    replay_traj_target,
)
from bench_core.schemas import REPLAY_STEP_ORDER
from bench_core.utils import (
    calc_p99,
    calc_percentiles,
    calc_tail_ratio,
    classify_tail_latency,
)
from bench_core.workflow_registry import ReportContext, ReportFormatters

from .replay_config import ReplayConfig


class ReplayReportFormatter(ReportFormatters):
    """Replay workflow report surfaces (Phase 3: ported byte-for-byte from
    ``ReportFormatter.format_replay_stats_section`` / ``format_replay_step_timing_table``
    / ``format_throughput_section`` / ``format_trajectory_summary_section`` /
    ``_format_lifecycle_overhead_by_round``).

    Inherits the command-ready defaults (replay probes via ``uname -a``). The
    replay stats section carries the lifecycle-overhead / retry / decomp sub-blocks
    (300+ lines) moved here unchanged; the byte-identical report golden is the
    safety net.
    """

    error_display_order = CODING_ERROR_DISPLAY

    def format_snapshot_line(self, snap, sandbox_states, config) -> str:
        traj_done = sum(s.replay_metrics.trajectory_completions for s in sandbox_states.values())
        total_trajs = replay_traj_target(config)
        traj = f"traj={traj_done}/{total_trajs}" if total_trajs else f"traj={traj_done}"
        return (
            f"  Replay:    {snap.task_success:3d}/{snap.task_total:3d}  "
            f"{traj}  avg={snap.recent_avg_latency:.2f}s  p99={snap.recent_p99_latency:.2f}s"
        )

    def format_stats_section(self, ctx: ReportContext) -> list[str]:
        """Format trajectory-replay task statistics section."""
        rcfg = ctx.config.workflow_config
        assert isinstance(rcfg, ReplayConfig), "replay stats require a ReplayConfig view"
        sandbox_states = ctx.sandbox_states
        all_latencies: list[float] = []
        for s in sandbox_states.values():
            all_latencies.extend(s.replay_metrics.latencies)

        total_tasks = sum(s.replay_metrics.total_tasks for s in sandbox_states.values())
        total_success = sum(s.replay_metrics.success_count for s in sandbox_states.values())
        total_failed = sum(s.replay_metrics.failed_count for s in sandbox_states.values())
        total_timeout = sum(s.replay_metrics.timeout_count for s in sandbox_states.values())
        completions = sum(s.replay_metrics.trajectory_completions for s in sandbox_states.values())
        # Delay fidelity is per-sandbox (actual/requested delay); average across sandboxes.
        fidelity_values = [s.replay_metrics.delay_fidelity for s in sandbox_states.values()]
        delay_fidelity = statistics.mean(fidelity_values) if fidelity_values else 0.0

        lines = ["\n[Replay Task Statistics]"]
        # Label+colon padded to the widest ("Trajectory Completions:") so every
        # value starts in the same column; the colon stays contiguous with the
        # label so ``"Label:"`` substring asserts keep working.
        lines.append(f"  {'Total Steps:':<24}{total_tasks}")
        lines.append(f"  {'Success:':<24}{total_success}")
        lines.append(f"  {'Failed:':<24}{total_failed} (timeout: {total_timeout})")
        lines.append(f"  {'Success Rate:':<24}{total_success / max(1, total_tasks) * 100:.1f}%")
        lines.append(f"  {'Trajectory Completions:':<24}{completions}")
        fleet = ctx.config.total_count
        if fleet:
            pool = replay_pool_size(ctx.config)
            pool_note = f"; pool {pool} distinct" if pool else ""
            lines.append(f"  {'One-pass Target:':<24}{fleet} (1 trajectory/sandbox per round{pool_note})")
        orphan_skipped = sum(s.replay_metrics.orphan_skip_count for s in sandbox_states.values())
        if orphan_skipped:
            lines.append(f"  {'Orphan Skipped:':<24}{orphan_skipped}")
        # P2 lifecycle: one-time snapshot-creation pause (separate from per-step resume_sec).
        initial_pauses = [
            s.replay_metrics.initial_pause_sec
            for s in sandbox_states.values()
            if s.replay_metrics.initial_pause_sec > 0
        ]
        if initial_pauses:
            noun = "sandbox" if len(initial_pauses) == 1 else "sandboxes"
            lines.append(
                f"  {'Initial Pause:':<24}{statistics.mean(initial_pauses):.3f}s (over {len(initial_pauses)} {noun})"
            )
        lines.append(f"  {'Delay Fidelity:':<24}{delay_fidelity:.2f}")

        if all_latencies:
            avg = statistics.mean(all_latencies)
            p99 = calc_p99(all_latencies)
            lines.append(f"  {'Avg Latency:':<24}{avg:.3f}s")
            lines.append(f"  {'P99 Latency:':<24}{p99:.3f}s")

        # P2.5 [Lifecycle Overhead] -- lifecycle + trajectory mode. Per-sample
        # overhead_i = (resume_sec_i + pause_sec_i) / slice_total_sec_i
        # (mean/P50/P95), plus an aggregate ratio. Near-zero slices
        # (slice_total_sec < MIN_SLICE_SEC) are excluded from the per-sample
        # overhead list; the duration percentile lists already exclude == 0.
        if rcfg.replay_mode in ("lifecycle", "trajectory"):
            all_resume: list[float] = []
            all_pause: list[float] = []
            all_slice: list[float] = []
            all_slot_held: list[float] = []
            all_interaction: list[float] = []
            for s in sandbox_states.values():
                all_resume.extend(s.replay_metrics.resume_secs)
                all_pause.extend(s.replay_metrics.pause_secs)
                all_slice.extend(s.replay_metrics.slice_total_secs)
                all_slot_held.extend(s.replay_metrics.running_slot_held_secs)
                all_interaction.extend(s.replay_metrics.interaction_total_secs)
            if all_slice:
                lines.append("[Lifecycle Overhead]")
                resume_stats = calc_percentiles(all_resume)
                pause_stats = calc_percentiles(all_pause)
                slice_stats = calc_percentiles(all_slice)
                n = len(all_slice)
                # Percentile lines aligned to the widest label in this group
                # ("Interaction:") so the P50= column starts in one place.
                lines.append(
                    f"  {'Resume:':<13}P50={resume_stats['p50']:.3f}s "
                    f"P95={resume_stats['p95']:.3f}s P99={resume_stats['p99']:.3f}s  (n={n})"
                )
                lines.append(
                    f"  {'Pause:':<13}P50={pause_stats['p50']:.3f}s "
                    f"P95={pause_stats['p95']:.3f}s P99={pause_stats['p99']:.3f}s  (n={n})"
                )
                lines.append(
                    f"  {'Slice:':<13}P50={slice_stats['p50']:.3f}s "
                    f"P95={slice_stats['p95']:.3f}s P99={slice_stats['p99']:.3f}s  (n={n})"
                )
                # L7: Slot held (overcommit-efficiency = lease release - acquire).
                # slot_held ≡ slice by construction (the lease spans resume->pause;
                # slot_contention_wait happens before acquire, so it is never in the
                # lease). Only render when it materially diverges from slice -- e.g.
                # trajectory-mode capacity_wait -- otherwise it restates Slice.
                if all_slot_held and ctx.admission_snapshot is not None:
                    held_diverges = any(abs(h - s) >= 0.001 for h, s in zip(all_slot_held, all_slice))
                    if held_diverges:
                        held_stats = calc_percentiles(all_slot_held)
                        lines.append(
                            f"  {'Slot held:':<13}P50={held_stats['p50']:.3f}s "
                            f"P95={held_stats['p95']:.3f}s  (n={len(all_slot_held)})"
                        )
                if all_interaction:
                    inter_stats = calc_percentiles(all_interaction)
                    lines.append(
                        f"  {'Interaction:':<13}P50={inter_stats['p50']:.3f}s "
                        f"P95={inter_stats['p95']:.3f}s  (n={len(all_interaction)})"
                    )
                # per-sample overhead, near-zero guarded
                overheads = [(r + p) / s for r, p, s in zip(all_resume, all_pause, all_slice) if s >= MIN_SLICE_SEC]
                if overheads:
                    oh_stats = calc_percentiles(overheads)
                    lines.append(
                        f"  Overhead per-sample: mean={oh_stats['avg'] * 100:.1f}% "
                        f"P50={oh_stats['p50'] * 100:.1f}% P95={oh_stats['p95'] * 100:.1f}% "
                        f"(n={len(overheads)})"
                    )
                # aggregate ratio (robust to per-sample outliers)
                agg_slice = [s for s in all_slice if s >= MIN_SLICE_SEC]
                agg_resume = [r for r, s in zip(all_resume, all_slice) if s >= MIN_SLICE_SEC]
                agg_pause = [p for p, s in zip(all_pause, all_slice) if s >= MIN_SLICE_SEC]
                if sum(agg_slice) > 0:
                    agg = (sum(agg_resume) + sum(agg_pause)) / sum(agg_slice)
                    lines.append(f"  Overhead aggregate:  {agg * 100:.1f}%")
                # known caveat footer (resume_sec includes the post-resume ready-wait;
                # resume_sec = resume_inflight_wait + resume_api + resume_ready_wait --
                # rate-pacing is PRE-lease, excluded, so it is absent from the Resume
                # decomp line; the per-phase rate-pacing percentiles live on the
                # separate "QPS pacing delay" admission-block line.)
                lines.append("  (resume_sec includes post-resume ready-wait; see Resume decomp)")

                # Phase 3.3: retry-impact sub-block. Reads the ReplayMetrics
                # accumulators (populated by the runner alongside retry_* series
                # events), NOT the series JSONL -- ReplayMetrics is the report's
                # consistent data source. retries/time lost always render; the
                # per-slice P95 line only when retries actually occurred.
                retry_count = sum(s.replay_metrics.retry_queued_count for s in sandbox_states.values())
                retry_time_lost = sum(s.replay_metrics.time_lost_to_retry_sec for s in sandbox_states.values())
                if retry_count > 0:
                    all_retries_per_slice: list[int] = []
                    for s in sandbox_states.values():
                        all_retries_per_slice.extend(s.replay_metrics.retries_per_slice)
                    rp95 = calc_percentiles(all_retries_per_slice)["p95"]
                    lines.append(
                        f"  Retry impact: retries={retry_count} "
                        f"(time lost={retry_time_lost:.3f}s) "
                        f"retries/slice P95={rp95:.2f}"
                    )
                else:
                    lines.append(f"  Retry impact: retries={retry_count} " f"(time lost={retry_time_lost:.3f}s)")

                # P2.6 decomposition sub-block: column-stable breakdown of
                # resume/pause into their segment components. Columns are NEVER
                # conditionally dropped (0.000s when inactive); only the Slot
                # contention and Admission WHOLE LINES are conditional on an
                # admission controller being present.
                all_resume_api: list[float] = []
                all_resume_ready_wait: list[float] = []
                all_resume_inflight_wait: list[float] = []
                all_resume_rate_pacing_wait: list[float] = []
                all_slot_contention: list[float] = []
                all_pause_api: list[float] = []
                all_pause_inflight_wait: list[float] = []
                all_pause_rate_pacing_wait: list[float] = []
                # Wait-decoupling components (rate pacing + inflight fuse + the
                # slot-scheduler's natural_delay/capacity splits). The per-phase
                # *_inflight_wait / *_rate_pacing_wait lists feed the Resume/Pause
                # decomp lines, aligned to the invariants (resume_sec = inflight +
                # api + ready_wait; pause_sec = rate_pacing + inflight + api); the
                # element-wise sum properties (rate_pacing_wait_secs /
                # inflight_wait_secs) feed the "QPS pacing delay" / "Inflight wait"
                # admission-block summary lines.
                all_rate_pacing_wait: list[float] = []
                all_inflight_wait: list[float] = []
                all_natural_delay: list[float] = []
                all_capacity_wait: list[float] = []
                for s in sandbox_states.values():
                    all_resume_api.extend(s.replay_metrics.resume_api_secs)
                    all_resume_ready_wait.extend(s.replay_metrics.resume_ready_wait_secs)
                    all_resume_inflight_wait.extend(s.replay_metrics.resume_inflight_wait_secs)
                    all_resume_rate_pacing_wait.extend(s.replay_metrics.resume_rate_pacing_wait_secs)
                    all_slot_contention.extend(s.replay_metrics.slot_contention_wait_secs)
                    all_pause_api.extend(s.replay_metrics.pause_api_secs)
                    all_pause_inflight_wait.extend(s.replay_metrics.pause_inflight_wait_secs)
                    all_pause_rate_pacing_wait.extend(s.replay_metrics.pause_rate_pacing_wait_secs)
                    all_rate_pacing_wait.extend(s.replay_metrics.rate_pacing_wait_secs)
                    all_inflight_wait.extend(s.replay_metrics.inflight_wait_secs)
                    all_natural_delay.extend(s.replay_metrics.natural_delay_secs)
                    all_capacity_wait.extend(s.replay_metrics.capacity_wait_secs)

                # Resume decomp line -- the three components that sum to resume_sec
                # (resume_sec = resume_inflight_wait + resume_api + resume_ready_wait).
                # Rate-pacing is PRE-lease, excluded from resume_sec, so it is NOT
                # listed here; its per-phase percentile lives on the "QPS pacing
                # delay" admission-block line. Always rendered when all_slice non-empty.
                resume_api_stats = calc_percentiles(all_resume_api)
                resume_ready_wait_stats = calc_percentiles(all_resume_ready_wait)
                resume_inflight_wait_stats = calc_percentiles(all_resume_inflight_wait)
                lines.append(
                    f"  Resume decomp: api P50={resume_api_stats['p50']:.3f}s "
                    f"P95={resume_api_stats['p95']:.3f}s | "
                    f"ready_wait P50={resume_ready_wait_stats['p50']:.3f}s | "
                    f"inflight_wait P50={resume_inflight_wait_stats['p50']:.3f}s  (n={n})"
                )

                # Pause decomp line -- the three components that sum to pause_sec
                # (pause_sec = pause_rate_pacing_wait + pause_inflight_wait + pause_api).
                # Rate-pacing is IN-lease, so it IS listed here, using pause's own
                # rate-pacing stat -- not the resume stat the pre-split code reused.
                pause_api_stats = calc_percentiles(all_pause_api)
                pause_inflight_wait_stats = calc_percentiles(all_pause_inflight_wait)
                pause_rate_pacing_wait_stats = calc_percentiles(all_pause_rate_pacing_wait)
                lines.append(
                    f"  Pause decomp:  api P50={pause_api_stats['p50']:.3f}s | "
                    f"rate_pacing P50={pause_rate_pacing_wait_stats['p50']:.3f}s | "
                    f"inflight_wait P50={pause_inflight_wait_stats['p50']:.3f}s  (n={n})"
                )

                # Conditional lines (only when admission controller was built)
                if ctx.admission_snapshot is not None:
                    slot_contention_stats = calc_percentiles(all_slot_contention)
                    lines.append(
                        f"  Slot contention: P50={slot_contention_stats['p50']:.3f}s "
                        f"P95={slot_contention_stats['p95']:.3f}s  (n={len(all_slot_contention)})  [= nat + cap]"
                    )
                    # Three independent wait components (rate pacing, inflight
                    # fuse, and the slot-scheduler's natural/capacity split).
                    # Each is shown only when its knob was active so an "off"
                    # component does not add a noise 0.000s line.
                    a = ctx.admission_snapshot
                    if a.get("qps") != "off":
                        # Per-phase split: resume rate-pacing is PRE-lease (removed
                        # from the Resume decomp line, which only carries resume_sec
                        # components), so it is surfaced here alongside pause's own
                        # rate-pacing and the element-wise sum -- the rate-pacing cost
                        # stays visible per phase without mis-attributing resume's into
                        # resume_sec. (Percentiles do not add; sum P50 != resume P50
                        # + pause P50 in general.)
                        resume_rp_stats = calc_percentiles(all_resume_rate_pacing_wait)
                        pause_rp_stats = calc_percentiles(all_pause_rate_pacing_wait)
                        sum_rp_stats = calc_percentiles(all_rate_pacing_wait)
                        lines.append(
                            f"  QPS pacing delay: resume P50={resume_rp_stats['p50']:.3f}s | "
                            f"pause P50={pause_rp_stats['p50']:.3f}s | "
                            f"sum P50={sum_rp_stats['p50']:.3f}s P95={sum_rp_stats['p95']:.3f}s  "
                            f"(n={len(all_rate_pacing_wait)})"
                        )
                    if a.get("inflight_cap") not in (None, "off"):
                        inf_stats = calc_percentiles(all_inflight_wait)
                        lines.append(
                            f"  Inflight wait:    P50={inf_stats['p50']:.3f}s "
                            f"P95={inf_stats['p95']:.3f}s  (n={len(all_inflight_wait)})"
                        )
                    if all_natural_delay and any(v > 0 for v in all_natural_delay):
                        nd_stats = calc_percentiles(all_natural_delay)
                        lines.append(f"  Natural delay:    P50={nd_stats['p50']:.3f}s  (n={len(all_natural_delay)})")
                    if all_capacity_wait and any(v > 0 for v in all_capacity_wait):
                        cw_stats = calc_percentiles(all_capacity_wait)
                        lines.append(
                            f"  Capacity wait:    P50={cw_stats['p50']:.3f}s "
                            f"P95={cw_stats['p95']:.3f}s  (n={len(all_capacity_wait)})"
                        )
                    lines.append("  Admission:")
                    rs = a.get("running_slots") or {}
                    if rs:
                        lines.append(
                            f"    Running slots: maximum={rs.get('maximum', 0)} "
                            f"active={rs.get('active', 0)} peak_active={rs.get('peak_active', 0)} "
                            f"granted={rs.get('granted', 0)} waiting={rs.get('waiting', 0)} "
                            f"avg_queue_wait={rs.get('average_queue_wait_sec', 0.0):.3f}s"
                        )
                    ql = a.get("qps_limiter")
                    # The limiter block renders when EITHER function is active
                    # (the two knobs are independent). Rate-pacing detail shows
                    # only when qps != off; inflight-fuse detail only when the
                    # cap is set.
                    if ql:
                        if a.get("qps") != "off":
                            lines.append(
                                f"    Rate pacing:   qps={ql.get('qps')} "
                                f"dispatched={ql.get('dispatched', 0)} "
                                f"avg_wait={ql.get('average_wait_sec', 0.0):.1f}s "
                                f"max_wait={ql.get('max_wait_sec', 0.0):.1f}s"
                            )
                            dbo = ql.get("dispatched_by_operation", {})
                            lines.append(
                                "    Dispatched by operation: "
                                + " ".join(
                                    f"{op}={dbo.get(op, 0)}"
                                    for op in ("resume", "pause", "cleanup", "create", "command")
                                )
                            )
                            wbo = ql.get("waiting_by_operation", {})
                            # Suppress an all-zero waiting line -- it is pure noise
                            # (no op is ever queued) and never adds information.
                            if any(wbo.get(op, 0) for op in ("resume", "pause", "cleanup", "create", "command")):
                                lines.append(
                                    "    Waiting by operation:    "
                                    + " ".join(
                                        f"{op}={wbo.get(op, 0)}"
                                        for op in ("resume", "pause", "cleanup", "create", "command")
                                    )
                                )
                        if a.get("inflight_cap") not in (None, "off"):
                            lines.append(
                                f"    Inflight fuse: cap={ql.get('inflight_cap')} "
                                f"in_flight={ql.get('in_flight', 0)} "
                                f"dispatched={ql.get('inflight_dispatched', 0)} "
                                f"avg_wait={ql.get('average_inflight_wait_sec', 0.0):.1f}s"
                            )

        return lines

    def format_throughput_section(self, ctx: ReportContext) -> list[str]:
        """[Throughput & Overcommit] -- throughput/efficiency from ReplayObservability.

        Rendered for replay workflows. Wall-gated metrics (steps_per_sec,
        effective_parallelism, exec_wall_utilization) render ``n/a (zero
        wall-clock time)`` when wall_sec is None/<=0; overcommit_ratio is
        always rendered (it does not depend on wall-clock).
        """
        obs = ReplayObservability(
            ctx.config,
            ctx.sandbox_states,
            admission_snapshot=ctx.admission_snapshot,
            wall_sec=ctx.wall_sec,
        )
        lines = ["\n[Throughput & Overcommit]"]
        na = "n/a (zero wall-clock time)"
        if ctx.wall_sec is not None and ctx.wall_sec > 0:
            lines.append(f"  {'wall_sec:':<24}{ctx.wall_sec:.1f}")
        sps = obs.steps_per_sec
        lines.append(f"  {'steps_per_sec:':<24}{f'{sps:.2f}' if sps is not None else na}")
        ep = obs.effective_parallelism
        lines.append(f"  {'effective_parallelism:':<24}{f'{ep:.2f}' if ep is not None else na}")
        eu = obs.exec_wall_utilization
        lines.append(f"  {'exec_wall_utilization:':<24}{f'{eu * 100:.1f}%' if eu is not None else na}")
        lines.append(f"  {'overcommit_ratio:':<24}{obs.overcommit_ratio:.1f}x")
        return lines

    def format_trajectory_summary_section(self, ctx: ReportContext) -> list[str]:
        """[Trajectory Summary] -- create/kill/slot-held percentiles for trajectory mode.

        Rendered only in trajectory mode AND when create_sec measurements exist
        (create_secs is populated only in trajectory mode; empty otherwise). The
        slot_held line reuses the same running_slot_held_secs list as
        [Lifecycle Overhead] but shows P99 here too.
        """
        rcfg = ctx.config.workflow_config
        assert isinstance(rcfg, ReplayConfig), "trajectory summary requires a ReplayConfig view"
        if rcfg.replay_mode != "trajectory":
            return []
        obs = ReplayObservability(
            ctx.config,
            ctx.sandbox_states,
            admission_snapshot=ctx.admission_snapshot,
            wall_sec=ctx.wall_sec,
        )
        cs = obs.create_sec_stats
        # Gate on non-empty create_secs (calc_percentiles returns all-0.0 for an empty list).
        if cs["p99"] == 0.0 and cs["max"] == 0.0:
            return []
        ks = obs.kill_sec_stats
        sh = obs.slot_held_stats
        n_create = sum(len(s.replay_metrics.create_secs) for s in ctx.sandbox_states.values())
        n_kill = sum(len(s.replay_metrics.kill_secs) for s in ctx.sandbox_states.values())
        lines = ["\n[Trajectory Summary]"]
        lines.append(f"  Create sec: P50={cs['p50']:.3f}s P95={cs['p95']:.3f}s P99={cs['p99']:.3f}s  (n={n_create})")
        lines.append(f"  Kill sec:   P50={ks['p50']:.3f}s P95={ks['p95']:.3f}s P99={ks['p99']:.3f}s  (n={n_kill})")
        if sh["max"] > 0.0:
            lines.append(f"  Slot held:  P50={sh['p50']:.3f}s P95={sh['p95']:.3f}s P99={sh['p99']:.3f}s")
        return lines

    def format_step_timing(self, ctx: ReportContext) -> list[str]:
        """Format replay per-action-type timing as a table.

        Replay's "step" axis is the recorded action's type (shell /
        str_replace_editor / bash / other), bucketed by ``classify_action``.
        """
        all_step_times: dict[str, list[float]] = {}
        for s in ctx.sandbox_states.values():
            step_times_copy = s.replay_metrics.get_step_times_copy()
            for step_name, times in step_times_copy.items():
                all_step_times.setdefault(step_name, []).extend(times)

        if not all_step_times:
            return []

        lines = ["\n[Step-Level Timing (Replay Mode)]"]
        headers = ["Action", "Count", "Avg(s)", "P50(s)", "P95(s)", "P99(s)", "Tail"]
        rows: list[list[str]] = []

        for step_name in REPLAY_STEP_ORDER:
            if step_name in all_step_times and all_step_times[step_name]:
                times = all_step_times[step_name]
                stats = calc_percentiles(times)
                tail_ratio = calc_tail_ratio(times)
                severity = classify_tail_latency(tail_ratio)
                rows.append(
                    [
                        step_name,
                        str(len(times)),
                        f"{stats['avg']:.3f}",
                        f"{stats['p50']:.3f}",
                        f"{stats['p95']:.3f}",
                        f"{stats['p99']:.3f}",
                        f"{tail_ratio:.2f}x ({severity})",
                    ]
                )

        lines.extend(TableFormatter.format_table(headers, rows))
        lines.append("\n  Tail Ratio: P99/P50 - indicates long-tail latency severity")
        lines.append("  < 1.2x: minimal | 1.2-1.5x: moderate | > 1.5x: significant")
        return lines

    def format_round_extras(self, ctx: ReportContext, active_rounds: dict[int, dict[str, Any]]) -> list[str]:
        """Per-round resume/pause/slice P50+P95 + aggregate overhead sub-table.

        The lifecycle lists are index-aligned within each round (appended in
        lockstep per slice), so the aggregate overhead ratio = (resume+pause)
        /slice is computed over matching samples, with near-zero slices
        excluded (same guard as the cumulative section).

        Carries only the per-round comparison dimension (P50/P95 + overhead +
        slot-held); the depth metrics (P99, per-sample overhead, decomp,
        interaction) stay in the cumulative [Lifecycle Overhead] section to
        avoid restating them per round. Slot held is meaningful only under an
        admission controller, so its column is conditional on admission_snapshot.
        """
        has_admission = ctx.admission_snapshot is not None
        lines = ["", "[Lifecycle Overhead by Round]"]
        headers = [
            "Round",
            "n",
            "Resume P50(s)",
            "Resume P95(s)",
            "Pause P50(s)",
            "Pause P95(s)",
            "Slice P50(s)",
            "Slice P95(s)",
        ]
        if has_admission:
            headers.append("Slot held P50(s)")
        headers.append("Overhead%")
        rows: list[list[str]] = []
        for round_id in sorted(active_rounds.keys()):
            resumes = active_rounds[round_id]["resume"]
            pauses = active_rounds[round_id]["pause"]
            slices = active_rounds[round_id]["slice"]
            if not slices:
                continue
            r_stats = calc_percentiles(resumes)
            p_stats = calc_percentiles(pauses)
            s_stats = calc_percentiles(slices)
            agg_slice = [sv for sv in slices if sv >= MIN_SLICE_SEC]
            agg_resume = [rv for rv, sv in zip(resumes, slices) if sv >= MIN_SLICE_SEC]
            agg_pause = [pv for pv, sv in zip(pauses, slices) if sv >= MIN_SLICE_SEC]
            overhead = (sum(agg_resume) + sum(agg_pause)) / sum(agg_slice) if sum(agg_slice) > 0 else 0.0
            row = [
                str(round_id),
                str(len(slices)),
                f"{r_stats['p50']:.3f}",
                f"{r_stats['p95']:.3f}",
                f"{p_stats['p50']:.3f}",
                f"{p_stats['p95']:.3f}",
                f"{s_stats['p50']:.3f}",
                f"{s_stats['p95']:.3f}",
            ]
            if has_admission:
                slot_held = active_rounds[round_id].get("slot_held", [])
                held_stats = calc_percentiles(slot_held) if slot_held else calc_percentiles([0.0])
                row.append(f"{held_stats['p50']:.3f}")
            row.append(f"{overhead * 100:.1f}")
            rows.append(row)
        if not rows:
            return []
        lines.extend(TableFormatter.format_table(headers, rows))
        return lines
