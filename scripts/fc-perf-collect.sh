#!/usr/bin/env bash
# fc-perf-collect.sh
# Collect Firecracker-process perf metrics for a pinned FC PID, ARM vs x86.
#   - usr/sys/guest time : pidstat -u  (%usr %system %guest; %guest is filled
#                          for a VMM like firecracker since it runs the KVM guest).
#   - on-CPU             : perf stat -e cycles,instructions -p PID  ("oncputime
#                          直接采集 cycles" -- cycles + instructions give CPI =
#                          on-CPU cost/instruction; no BCC cpudist).
#   - off-CPU            : perf record sched:sched_stat_sleep +
#                          sched:sched_switch + sched:sched_process_exit, -a -g,
#                          perf inject -s (synthesize sleep period), then fold to
#                          an off-CPU flamegraph via stackcollapse.pl/flamegraph.pl.
#                          Needs /proc/sys/kernel/sched_schedstats=1 (set best-effort).
#   - topdown L1         : x86 -> AMDuProfPcm -m topdown -c core=<CORES> -p PID
#                          (PCM topdown, PID attach -- unlike the old AMDuProfCLI
#                          `assess` config, PCM takes -p PID directly).
#                          arm -> devkit tuner top-down --cpu <CORES>
#                          (system-wide on the pin cores; FC is pinned there).
#                          Both emit the same four L1 buckets (retiring /
#                          frontend / backend / bad-spec) -> cross-ISA comparable.
#
# Arch is auto-detected (uname -m) so the SAME script deploys to both the ARM
# and the x86 box unchanged; it just picks the right topdown collector.
#
# The bench's `[aenv-pin] PINNED OK pid=...` log (from --create-only) gives the
# PID to pass here. Pin is already verified before this runs.
#
# Usage:
#   ./fc-perf-collect.sh -p <PID> -d <SECONDS> -o <OUTDIR> [-a arm|x86] [-c <CORES>]
# Examples (2vCPU pin on cores 2,3, capture 60s):
#   x86: ./fc-perf-collect.sh -p $(pgrep -fn firecracker) -d 60 -o ./fc-perf-run1 -c '2,,3'
#   arm: ./fc-perf-collect.sh -p $(pgrep -fn firecracker) -d 60 -o ./fc-perf-run1 -c '2-3'
# -c CORES: pin-core spec in the topdown tool's native format -- x86 AMDuProfPcm
#   takes a comma list like `2,,3` (script wraps it as core=<CORES>); arm devkit
#   takes a range like `2-3` (passed to --cpu). Omit to collect without core filter.
# Off-CPU flamegraph needs stackcollapse.pl + flamegraph.pl in CWD (github.com/
# brendangregg/FlameGraph); without them the raw perf.data is still produced.
set -euo pipefail

PID=""; DURATION=30; OUTDIR="./fc-perf"; ARCH=""; CORES=""
while getopts "p:d:o:a:c:h" opt; do
  case $opt in
    p) PID=$OPTARG ;;
    d) DURATION=$OPTARG ;;
    o) OUTDIR=$OPTARG ;;
    a) ARCH=$OPTARG ;;
    c) CORES=$OPTARG ;;
    h) sed -n '2,30p' "$0"; exit 0 ;;
    *) exit 2 ;;
  esac
done

[[ -n "$PID" && "$PID" =~ ^[0-9]+$ ]] || { echo "ERR: -p PID (integer) required"; exit 2; }
[[ "$DURATION" =~ ^[0-9]+$ ]] || { echo "ERR: -d seconds (integer) required"; exit 2; }

if [[ -z "$ARCH" ]]; then
  case "$(uname -m)" in
    x86_64)        ARCH=x86 ;;
    aarch64|arm64) ARCH=arm ;;
    *) echo "ERR: unknown arch $(uname -m); pass -a arm|x86"; exit 2 ;;
  esac
fi

mkdir -p "$OUTDIR"
echo "[fc-perf] arch=$ARCH pid=$PID cores=${CORES:-<none>} duration=${DURATION}s outdir=$OUTDIR"

# ----------------------------------------------------------- 1. usr/sys/guest
# pidstat -u reports %usr %system %guest %gnice %wait %CPU. %guest is the
# on-CPU time spent in the KVM guest -- exactly the VMM guest-time metric.
# `1 $DURATION` = sample every 1s for DURATION s, then print the average row.
if command -v pidstat >/dev/null 2>&1; then
  pidstat -u -p "$PID" 1 "$DURATION" >"$OUTDIR/pidstat_cpu.txt" 2>&1 &
else
  echo "[fc-perf] WARN: pidstat not found -> usr/sys/guest skipped (pkg: sysstat)" >&2
fi

# ----------------------------------------------------------- 2. on-CPU cycles
# "oncputime 直接采集 cycles": perf stat -p PID counting cycles + instructions
# (CPI = cycles/instructions = on-CPU cost per instruction). No BCC cpudist.
if command -v perf >/dev/null 2>&1; then
  perf stat -e cycles,instructions -p "$PID" -- sleep "$DURATION" \
    >"$OUTDIR/perf_oncpu.txt" 2>&1 &
else
  echo "[fc-perf] WARN: perf not found -> on-CPU cycles skipped" >&2
fi

# ----------------------------------------------------------- 3. off-CPU flamegraph
# sched:sched_stat_sleep carries the off-CPU sleep duration as :period, but only
# after `perf inject -s` synthesizes it from sched_switch state. Enable
# sched_schedstats so the sleep events carry real durations. Record is system-wide
# (-a) with -g callgraph; filter to the FC PID in the flamegraph fold step if
# needed (perf script post-processing). Runs as a background subshell so it
# overlaps the other collectors; total wall ~= DURATION.
if command -v perf >/dev/null 2>&1; then
  (
    set +e
    echo 1 > /proc/sys/kernel/sched_schedstats 2>/dev/null
    perf record -e sched:sched_stat_sleep -e sched:sched_switch -e sched:sched_process_exit \
      -a -g -o "$OUTDIR/perf.data.raw" sleep "$DURATION"
    perf inject -v -s -i "$OUTDIR/perf.data.raw" -o "$OUTDIR/perf.data"
    if [[ -x ./stackcollapse.pl && -x ./flamegraph.pl ]]; then
      perf script -F comm,pid,tid,cpu,time,period,event,ip,sym,dso,trace -i "$OUTDIR/perf.data" \
        | awk 'NF > 4 { exec = $1; period_ms = int($5 / 1000000) }
               NF > 1 && NF <= 4 && period_ms > 0 { print $2 }
               NF < 2 && period_ms > 0 { printf "%s\n%d\n\n", exec, period_ms }' \
        | ./stackcollapse.pl \
        | ./flamegraph.pl --countname=ms --title="Off-CPU Time Flame Graph" --colors=io \
          > "$OUTDIR/offcpu.svg"
    else
      echo "stackcollapse.pl/flamegraph.pl not in CWD -> raw perf.data left; get them from https://github.com/brendangregg/FlameGraph"
    fi
  ) >"$OUTDIR/offcpu.log" 2>&1 &
else
  echo "[fc-perf] WARN: perf not found -> off-CPU skipped" >&2
fi

# ----------------------------------------------------------- 4. topdown L1
case "$ARCH" in
  x86)
    # AMD uProf PCM topdown. PCM (AMDuProfPcm) is distinct from AMDuProfCLI and
    # takes -p PID directly for -m topdown (the CLI `assess` config did not).
    # -c core=<list> filters to the pin cores; -d seconds. Output is stdout.
    # Install: sudo dpkg -i amduprof_*.deb (ships AMDuProfPcm in /opt/AMDuProf_*/bin).
    if command -v AMDuProfPcm >/dev/null 2>&1; then
      if [[ -n "$CORES" ]]; then
        AMDuProfPcm -m topdown -c "core=$CORES" -p "$PID" -d "$DURATION" \
          >"$OUTDIR/uprof_topdown.txt" 2>&1 &
      else
        AMDuProfPcm -m topdown -p "$PID" -d "$DURATION" \
          >"$OUTDIR/uprof_topdown.txt" 2>&1 &
      fi
    else
      echo "[fc-perf] ERR: AMDuProfPcm not in PATH; install the AMD uProf .deb (PCM CLI)" >&2
    fi
    ;;
  arm)
    # ARM topdown via devkit tuner top-down, system-wide on the pin cores (the FC
    # is already pinned there). --cpu takes a range (e.g. 2-3); -d seconds, -i
    # sample interval. Same four L1 buckets as x86 uProf above.
    if command -v devkit >/dev/null 2>&1; then
      if [[ -n "$CORES" ]]; then
        devkit tuner top-down -d "$DURATION" -i 1 --cpu "$CORES" \
          >"$OUTDIR/devkit_topdown.txt" 2>&1 &
      else
        devkit tuner top-down -d "$DURATION" -i 1 \
          >"$OUTDIR/devkit_topdown.txt" 2>&1 &
      fi
    else
      echo "[fc-perf] ERR: devkit not in PATH; set your ARM topdown tool" >&2
    fi
    ;;
esac

wait || true
echo "[fc-perf] done. outputs:"
ls -1 "$OUTDIR"
