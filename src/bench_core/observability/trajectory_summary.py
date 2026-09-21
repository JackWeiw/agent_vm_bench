"""Shared per-trajectory aggregation over a loaded event list.

Two tiers:

- :func:`trajectory_summaries` -- the RAW per-run layer, keyed by
  ``(trajectory_id, sandbox_index, round_id)``. One record per *run* (one
  sandbox's execution of one trajectory in one round). Sums, ``avg_slice``,
  ``elapsed_sec`` and ``requested_delay_sec`` are computed WITHIN that one run;
  this is the kernel-emit-raw-facts layer that downstream consumers (the oversub
  driver's ``trajectory-detail.csv``, the obs sheet) expand into per-run rows.
- :func:`aggregate_trajectories` -- the per-trajectory aggregate across runs.
  Durations/counts -> median (p50; p95/mean carried); ``success_rate`` -> mean;
  ``n_runs``/``n_success``/``n_failed`` -> sum. Carries TWO parallel sets
  (``all_runs`` + ``successful_runs``) so a reader is never misled by short
  failed runs dragging the latency median down: the headline ``elapsed_sec`` is
  the success-only median, falling back to the all-runs median (+ ``all_failed``
  flag) when no run succeeded.

Union-keyed over ``step`` + ``trajectory_create`` + ``trajectory_kill`` +
``trajectory_failed`` events so a run that failed at create (zero step events)
still appears -- otherwise the worst runs vanish from the comparison.

Record *assembly* (the per-step dict shape) stays in the replay runner; this
module only reads the loaded dicts. Sums are raw floats -- callers round at the
render boundary (the existing avg_slice==0.7 assertion depends on no
pre-rounding here).

Backward compat: when one run exists per tid (pool >= N, or round_count=1 with
a unique sandbox per trajectory), the per-run record IS the per-tid view and the
aggregate of one equals the run (median of one == the value), so pool>=N
behaviour is unchanged in magnitude.
"""
from __future__ import annotations

from bench_core.utils import calc_percentiles

# The per-step duration fields summed per run. slice_total_sec is the invariant
# total (resume+exec+pause); interaction_total_sec adds delay + capacity_wait +
# resume rate-pacing (>= slice, since resume rate-pacing is pre-lease and
# excluded from slice_total). The wait sums isolate the four independent
# non-productive components: slot_contention (the natural_delay + capacity_wait
# composite, kept for parse-compat) plus its split (natural_delay /
# capacity_wait), rate_pacing (the 1/qps shaping, split across
# resume_rate_pacing_wait + pause_rate_pacing_wait), and inflight (the fuse
# block, across resume_inflight + pause_inflight). Exported so obs_xlsx imports
# it back instead of keeping a local copy (DRY).
SEG_KEYS = (
    "slice_total_sec",
    "exec_sec",
    "resume_sec",
    "pause_sec",
    "interaction_total_sec",
    "slot_contention_wait_sec",
    "natural_delay_sec",
    "capacity_wait_sec",
    "rate_pacing_wait_sec",
    "inflight_wait_sec",
    "resume_rate_pacing_wait_sec",
    "pause_rate_pacing_wait_sec",
    "resume_inflight_wait_sec",
    "pause_inflight_wait_sec",
    "running_slot_held_sec",
)

# Top-level numeric fields on a per-run record that get the median/p95/mean
# treatment in the per-tid aggregate (everything except the SEG_KEYS, which are
# nested under ``sums`` and aggregated into ``time_breakdown_sec``).
_RUN_METRIC_FIELDS = (
    "elapsed_sec",
    "avg_slice",
    "requested_delay_sec",
    "create_sec",
    "kill_sec",
    "n_steps",
    "n_failed",
    "n_timeout",
)


def _pstats(vals: list[float]) -> dict:
    """``{p50, p95, avg, n}`` over a list of floats (all 0 / n=0 when empty)."""
    pc = calc_percentiles(vals)
    return {"p50": round(pc["p50"], 6), "p95": round(pc["p95"], 6), "avg": round(pc["avg"], 6), "n": len(vals)}


def _run_set(runs: list[dict]) -> dict:
    """Per-run metric medians for one run subset (all_runs or successful_runs).

    ``time_breakdown_sec`` carries the median of each SEG_KEY (the per-step
    duration sums within a run) so a reader can attribute where each run's
    ``elapsed_sec`` went -- exec / lifecycle-API / inter-step delay / create+kill
    / queueing waits -- without re-walking the series.
    """
    out = {f: _pstats([float(r[f]) for r in runs if r.get(f) is not None]) for f in _RUN_METRIC_FIELDS}
    out["time_breakdown_sec"] = {k: _pstats([float(r["sums"].get(k, 0.0)) for r in runs]) for k in SEG_KEYS}
    return out


def _sort_key(x) -> tuple:
    """Sort None last without comparing None to int (avoids TypeError)."""
    return (x is None, x)


def trajectory_summaries(events: list[dict]) -> list[dict]:
    """Group step + trajectory_create/kill/failed events by ``(trajectory_id, sandbox_index, round_id)``.

    Returns one dict per RUN, sorted by (trajectory_id, sandbox_index, round_id).
    Each dict: ``{trajectory_id, sandbox_index, round_id, n_steps, n_failed,
    n_timeout, success_rate, sums, avg_slice, elapsed_sec, requested_delay_sec,
    create_sec, kill_sec, create_error_type, kill_error_type, create_error,
    kill_error}``.

    - **Sums include failed steps at 0.0** (honest cost attribution; a failed
      slice did no work but still counts as an attempted step so avg_slice
      reflects per-attempt cost). ``n_failed``/``n_timeout`` count failures
      separately.
    - ``success_rate`` is ``None`` when ``n_steps==0`` (no steps attempted,
      distinct from ``0.0`` = all steps failed); guards ZeroDivisionError. A run
      is treated as *successful* by :func:`aggregate_trajectories` when its
      ``success_rate`` is not None and ``>= 1.0`` -- this covers create-failed
      runs (``n_steps==0`` -> ``None``) without a separate flag.
    - ``elapsed_sec`` prefers the wall-clock span from the first real
      ``resume_start`` to the last real ``pause_end`` (+ create+kill cost); it
      falls back to ``slice_total + requested_delay + create+kill`` when the
      series lacks real timestamps (exec-only / synthetic). Real stamps are
      ``time.time()`` epochs (~1.8e9); a failed step's zeroed sentinels (0.0)
      are dropped so they cannot collapse the span to ~1.8e9s.
    - ``create_sec``/``kill_sec`` sum within the run (a single create/kill per
      run in normal flow); ``create_error``/``kill_error`` carry the short error
      STRING from a ``trajectory_create(success=False)``/``trajectory_kill(success=False)``
      event (the only failure signal the series carries).
    """
    runs: dict[tuple, dict] = {}
    # Per-run timestamp accumulators kept in a side dict so the slot stays clean
    # (min resume_start / max pause_end / summed inter-step delay / cursor for
    # the gap = step[i].resume_start - step[i-1].pause_end).
    stamps: dict[tuple, dict] = {}

    def _slot(tid: str, sandbox, round_id) -> dict:
        key = (tid, sandbox, round_id)
        runs.setdefault(
            key,
            {
                "trajectory_id": tid,
                "sandbox_index": sandbox,
                "round_id": round_id,
                "n_steps": 0,
                "n_failed": 0,
                "n_timeout": 0,
                "sums": {k: 0.0 for k in SEG_KEYS},
                "create_sec": 0.0,
                "kill_sec": 0.0,
                "create_error_type": None,
                "kill_error_type": None,
                "create_error": None,
                "kill_error": None,
            },
        )
        stamps.setdefault(key, {"min_rs": float("inf"), "max_pe": 0.0, "req_delay": 0.0, "last_pe": 0.0})
        return runs[key]

    for ev in events:
        evtype = ev.get("event")
        if evtype == "step":
            tid = ev.get("trajectory_id") or ""
            sandbox = ev.get("sandbox_index")
            round_id = ev.get("round_id")
            acc = _slot(tid, sandbox, round_id)
            acc["n_steps"] += 1
            if ev.get("slice_failed"):
                acc["n_failed"] += 1
            if ev.get("timed_out"):
                acc["n_timeout"] += 1
            sums = acc["sums"]
            for k in SEG_KEYS:
                v = ev.get(k)
                if v is not None:
                    sums[k] += float(v)
            # Wall-span accumulators (mirrors trajectory_export._build_record's
            # elapsed logic, computed here so the per-run record is the single
            # source of truth for downstream aggregate + render).
            st = stamps[(tid, sandbox, round_id)]
            rs = ev.get("resume_start")
            if rs is not None:
                rsf = float(rs)
                if rsf > 0:
                    if rsf < st["min_rs"]:
                        st["min_rs"] = rsf
                    if st["last_pe"] > 0:
                        st["req_delay"] += max(0.0, rsf - st["last_pe"])
            pe = ev.get("pause_end")
            if pe is not None:
                pef = float(pe)
                if pef > 0:
                    if pef > st["max_pe"]:
                        st["max_pe"] = pef
                    st["last_pe"] = pef
        elif evtype in ("trajectory_create", "trajectory_kill"):
            tid = ev.get("trajectory_id")
            if not tid:
                continue
            sandbox = ev.get("sandbox_index")
            round_id = ev.get("round_id")
            acc = _slot(tid, sandbox, round_id)
            if evtype == "trajectory_create":
                v = ev.get("create_sec")
                if v is not None:
                    acc["create_sec"] += float(v)
                if ev.get("success") is False:
                    acc["create_error_type"] = ev.get("error_type")
                    acc["create_error"] = ev.get("error")
            else:  # trajectory_kill
                v = ev.get("kill_sec")
                if v is not None:
                    acc["kill_sec"] += float(v)
                if ev.get("success") is False:
                    acc["kill_error_type"] = ev.get("error_type")
                    acc["kill_error"] = ev.get("error")
        elif evtype == "trajectory_failed":
            tid = ev.get("trajectory_id")
            if not tid:
                continue
            sandbox = ev.get("sandbox_index")
            round_id = ev.get("round_id")
            acc = _slot(tid, sandbox, round_id)
            v = ev.get("create_sec")
            if v is not None:
                acc["create_sec"] += float(v)
            v = ev.get("kill_sec")
            if v is not None:
                acc["kill_sec"] += float(v)

    out = []
    for key in sorted(runs, key=lambda k: (str(k[0]), _sort_key(k[1]), _sort_key(k[2]))):
        acc = runs[key]
        st = stamps[key]
        n = acc["n_steps"]
        slice_sum = acc["sums"]["slice_total_sec"]
        acc["avg_slice"] = slice_sum / n if n else 0.0
        n_success = n - acc["n_failed"]
        acc["success_rate"] = round(n_success / n, 6) if n else None
        create_kill = acc["create_sec"] + acc["kill_sec"]
        if st["min_rs"] != float("inf") and st["max_pe"] > 0:
            elapsed = st["max_pe"] - st["min_rs"] + create_kill
        else:
            elapsed = slice_sum + st["req_delay"] + create_kill
        acc["elapsed_sec"] = round(elapsed, 6)
        acc["requested_delay_sec"] = round(st["req_delay"], 6)
        out.append(acc)
    return out


def aggregate_trajectories(per_run: list[dict]) -> list[dict]:
    """Roll per-run records up to one per-trajectory aggregate record.

    Returns one dict per trajectory_id (sorted), each carrying:
      ``{trajectory_id, n_runs, n_success, n_failed, success_rate, elapsed_sec,
      all_failed, n_steps_max, all_runs, successful_runs}``.

    - ``n_runs`` / ``n_success`` / ``n_failed`` are counts OF runs (summed, not
      medians); ``n_failed`` here = runs that did not fully succeed (distinct
      from the per-run ``n_failed`` step count).
    - ``success_rate`` is the MEAN of the per-run success rates (the fraction of
      runs that succeeded); ``None`` when no run has steps.
    - ``elapsed_sec`` (headline) = ``successful_runs.elapsed_sec.p50``, falling
      back to ``all_runs.elapsed_sec.p50`` + ``all_failed=True`` when no run
      succeeded -- the trajectory latency a reader wants, never dragged down by
      short failed runs.
    - ``all_runs`` / ``successful_runs`` each carry ``{p50, p95, avg, n}`` for
      every metric field + a ``time_breakdown_sec`` sub-dict of per-SEG_KEY
      medians. ``successful_runs`` is ``{}`` (empty) when no run succeeded.
    - ``n_steps_max`` = the trajectory's full step count (the max across runs;
      a failed run truncates early so the max is the true length).

    When ``n_runs==1`` (pool >= N, or round_count=1 with one sandbox per
    trajectory), every median is the single run's value and the two sets
    coincide, so behaviour is unchanged in magnitude vs the old per-tid sums.
    """
    by_tid: dict[str, list[dict]] = {}
    for r in per_run:
        by_tid.setdefault(r["trajectory_id"], []).append(r)

    out: list[dict] = []
    for tid in sorted(by_tid):
        runs = by_tid[tid]
        n_runs = len(runs)
        success_runs = [r for r in runs if r["success_rate"] is not None and r["success_rate"] >= 1.0]
        n_success = len(success_runs)
        n_failed = n_runs - n_success
        sr_vals = [r["success_rate"] for r in runs if r["success_rate"] is not None]
        success_rate = round(sum(sr_vals) / len(sr_vals), 6) if sr_vals else None

        all_set = _run_set(runs)
        success_set = _run_set(success_runs) if success_runs else {}
        all_failed = n_success == 0
        headline = success_set["elapsed_sec"]["p50"] if success_runs else all_set["elapsed_sec"]["p50"]
        n_steps_max = max((r["n_steps"] for r in runs), default=0)

        out.append(
            {
                "trajectory_id": tid,
                "n_runs": n_runs,
                "n_success": n_success,
                "n_failed": n_failed,
                "success_rate": success_rate,
                "elapsed_sec": headline,
                "all_failed": all_failed,
                "n_steps_max": n_steps_max,
                "all_runs": all_set,
                "successful_runs": success_set,
            }
        )
    return out
