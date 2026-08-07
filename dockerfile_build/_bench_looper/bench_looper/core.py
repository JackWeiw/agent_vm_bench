#!/usr/bin/env python3
"""Bench looper core: shared loop control, timing, and JSON results.

Owns everything scenario-agnostic: argument plumbing, the warmup + round
loop, per-iteration JSONL append, summary.json with per-step percentiles,
and the exit-code policy (a single failed iteration does not abort the run;
the process exits non-zero only if any iteration failed).

A scenario plugin implements BenchScenario.run_warmup / run_one_round and
returns an IterationResult. The verify mechanics differ per scenario and
live in the plugins; this core never inspects them.
"""

import json
import logging
import math
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("bench_looper")

DEFAULT_LOOPS = 20000
DEFAULT_RESULTS_DIR = "/opt/bench-looper/results"
RESULTS_DIR_ENV = "BENCH_RESULTS_DIR"
RUN_ID_ENV = "BENCH_RUN_ID"


@dataclass
class IterationResult:
    """Outcome of one benchmark round, reported by a scenario plugin.

    step_times values are seconds (the core converts to ms on write).
    """

    round_id: int
    success: bool
    step_times: Dict[str, float] = field(default_factory=dict)
    verify_success: bool = True
    compile_only: bool = False
    failed_step: Optional[str] = None
    error_type: Optional[str] = None  # "timeout" | "exit_code" | "exception"
    error_message: Optional[str] = None
    timed_out: bool = False


class BenchScenario:
    """Interface implemented by the browser / coding-go / coding-ts plugins."""

    name: str = ""

    def run_warmup(self) -> None:
        """Optional one-time warmup (excluded from results)."""
        return None

    def run_one_round(self, round_id: int) -> IterationResult:  # pragma: no cover - interface
        raise NotImplementedError


def load_operations(filename: str) -> dict:
    """Load a baked operations config shipped with the package."""
    path = Path(__file__).parent / "operations" / filename
    return json.loads(path.read_text(encoding="utf-8"))


def run_shell(command: str, timeout: float, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Run a shell command via bash, mirroring the host's E2B commands.run.

    Returns (exit_code, stdout, stderr). Exit 124 marks a timeout (the same
    sentinel the GNU `timeout` coreutils uses), so plugins can classify it as
    "timeout" without parsing stderr.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout
        err = exc.stderr
        out = out.decode(errors="replace") if isinstance(out, bytes) else (out or "")
        err = err.decode(errors="replace") if isinstance(err, bytes) else (err or "")
        return 124, out, err


def percentile(sorted_values: List[float], pct: float) -> float:
    """Nearest-rank percentile (pct in [0,100]) of an ascending-sorted list."""
    if not sorted_values:
        return 0.0
    rank = max(0, math.ceil(len(sorted_values) * pct / 100.0) - 1)
    return sorted_values[min(rank, len(sorted_values) - 1)]


def _error_type(exit_code: int) -> str:
    return "timeout" if exit_code == 124 else "exit_code"


def _error_detail(exit_code: int, stdout: str, stderr: str) -> str:
    parts = [f"exit_code={exit_code}"]
    if stderr:
        parts.append(f"stderr={stderr[:200]}")
    if stdout:
        parts.append(f"stdout={stdout[:200]}")
    return " | ".join(parts)


class BenchLooper:
    """Drive warmup + the round loop and write JSON results.

    Results layout (mirrors document-bench):
        <results_dir>/<scenario>/<run_id>/
            iterations.jsonl   one JSON object per round
            summary.json       aggregate counts + per-step percentiles
    """

    def __init__(
        self,
        scenario: BenchScenario,
        *,
        loops: int,
        duration: Optional[float],
        warmup: bool,
        results_dir: str,
        run_id: Optional[str],
    ):
        self.scenario = scenario
        self.loops = loops
        self.duration = duration
        self.warmup = warmup
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.run_dir = Path(results_dir) / scenario.name / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = self.run_dir / "iterations.jsonl"
        self._jsonl = self._jsonl_path.open("a", encoding="utf-8")
        self._start = time.perf_counter()
        self._failures = 0
        self._successes = 0
        self._records: List[dict] = []

    def run(self) -> int:
        """Run warmup + loops, write summary, return process exit code."""
        if self.warmup:
            try:
                logger.info("[%s] warmup starting", self.scenario.name)
                self.scenario.run_warmup()
                logger.info("[%s] warmup done", self.scenario.name)
            except Exception as exc:  # warmup failure is non-fatal
                logger.warning("[%s] warmup failed: %s", self.scenario.name, exc)

        completed = 0
        for i in range(self.loops):
            if self.duration is not None and (time.perf_counter() - self._start) >= self.duration:
                logger.info("[%s] duration reached, stopping at iteration %d", self.scenario.name, i)
                break
            self._run_one(i)
            completed = i + 1

        self._jsonl.close()
        self._write_summary()
        logger.info(
            "[%s] done: %d/%d ok (%d failed) -> %s",
            self.scenario.name,
            self._successes,
            completed,
            self._failures,
            self.run_dir,
        )
        return 1 if self._failures else 0

    def _run_one(self, round_id: int) -> None:
        wall_start = time.perf_counter()
        try:
            res = self.scenario.run_one_round(round_id)
        except Exception as exc:  # a plugin bug must not kill the whole run
            res = IterationResult(
                round_id=round_id,
                success=False,
                failed_step="exception",
                error_type="exception",
                error_message=str(exc)[:800],
            )
        wall = time.perf_counter() - wall_start

        record = {
            "iteration": round_id,
            "scenario": self.scenario.name,
            "round": round_id,
            "total_ms": round(wall * 1000, 3),
            "success": res.success,
            "failed_step": res.failed_step,
            "error_type": res.error_type,
            "error_message": res.error_message,
            "timed_out": res.timed_out,
            "verify_success": res.verify_success,
            "compile_only": res.compile_only,
            "steps": {k: round(v * 1000, 3) for k, v in res.step_times.items()},
        }
        self._jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._jsonl.flush()
        self._records.append(record)
        if res.success:
            self._successes += 1
        else:
            self._failures += 1
            logger.warning(
                "[%s] iteration %d failed at %s: %s",
                self.scenario.name,
                round_id,
                res.failed_step,
                (res.error_message or "")[:200],
            )

    def _write_summary(self) -> None:
        total_duration = time.perf_counter() - self._start
        per_step: Dict[str, List[float]] = {}
        for record in self._records:
            for step, ms in record["steps"].items():
                per_step.setdefault(step, []).append(ms)
        step_stats = {}
        for step, vals in per_step.items():
            ordered = sorted(vals)
            step_stats[step] = {
                "count": len(ordered),
                "avg_ms": round(sum(ordered) / len(ordered), 3),
                "p50_ms": round(percentile(ordered, 50), 3),
                "p95_ms": round(percentile(ordered, 95), 3),
                "p99_ms": round(percentile(ordered, 99), 3),
            }
        failure_hist: Dict[str, int] = {}
        for record in self._records:
            if not record["success"]:
                key = f"{record['failed_step']}:{record['error_type']}"
                failure_hist[key] = failure_hist.get(key, 0) + 1
        total = len(self._records)
        summary = {
            "scenario": self.scenario.name,
            "run_id": self.run_id,
            "loops_requested": self.loops,
            "loops_completed": total,
            "success_count": self._successes,
            "failure_count": self._failures,
            "success_rate": round(self._successes / total, 4) if total else 0.0,
            "total_duration_s": round(total_duration, 3),
            "steps": step_stats,
            "failures": [
                {
                    "failed_step": key.split(":", 1)[0],
                    "error_type": key.split(":", 1)[1] if ":" in key else "",
                    "count": count,
                }
                for key, count in sorted(failure_hist.items())
            ],
        }
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
