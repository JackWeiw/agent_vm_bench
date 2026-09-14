#!/usr/bin/env bash
# fc-perf-collect.sh
# Collect Firecracker-process perf metrics for a pinned FC PID, ARM vs x86.
#   - onCPU/offCPU time   : BCC cpudist (on-CPU run-length hist) + offcputime
#                           (off-CPU time + stacks); perf stat fallback
#                           (task-clock / ctx-switches) if no BCC.
#   - usr/sys/guest time  : pidstat -u  (%usr %system %guest; %guest is filled
#                           for a VMM like firecracker since it runs the KVM guest).
#   - topdown L1          : x86 -> AMD uProf `profile --config assess`, system-wide
#                           on the pinned cores (-a -c <CORES>); the FC is already
#                           pinned there so system-wide == FC's topdown. (-p PID
#                           attach is unreliable for the `assess` config; uProf
#                           notes -p is "CpuProfiler specific reports" only.)
#                           arm -> devkit topdown (user-supplied, see ARM block).
#
# Arch is auto-detected (uname -m) so the SAME script deploys to both the ARM
# and the x86 box unchanged; it just picks the right topdown collector. The
# two topdown backends emit the same four L1 buckets (retiring /
# frontend-bound / backend-bound / bad-speculation) -> cross-ISA comparable.
#
# The bench's `[aenv-pin] PINNED OK pid=...` log (from --create-only) gives the
# PID to pass here. Pin is already verified before this runs.
#
# Usage:
#   ./fc-perf-collect.sh -p <PID> -d <SECONDS> -o <OUTDIR> [-a arm|x86] [-c <CORES>]
# Example (match the 2vCPU pin CPUs 2,3, capture for 60s):
#   ./fc-perf-collect.sh -p $(pgrep -fn firecracker) -d 60 -o ./fc-perf-run1 -c 2,3
# -c CORES: comma list of the FC pin CPUs (x86 topdown collects system-wide on
#   these; required for a clean L1 topdown on x86 -- omit and the script falls
#   back to -p PID, which uProf may reject for the `assess` config).
set -euo pipefail

PID=""; DURATION=30; OUTDIR="./fc-perf"; ARCH=""; CORES=""
while getopts "p:d:o:a:c:h" opt; do
  case $opt in
    p) PID=$OPTARG ;;
    d) DURATION=$OPTARG ;;
    o) OUTDIR=$OPTARG ;;
    a) ARCH=$OPTARG ;;
    c) CORES=$OPTARG ;;
    h) sed -n '2,25p' "$0"; exit 0 ;;
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

# ----------------------------------------------------------- 2. onCPU/offCPU
# BCC on/off-CPU: BCC 0.31 (Debian 13) dropped the single `onoffcpu` tool, so
# use the two successors it ships: cpudist (on-CPU run-length histogram) +
# offcputime (off-CPU time, with stack traces), both as `-p PID`. Debian/Ubuntu
# ships them as `cpudist-bpfcc` / `offcputime-bpfcc`. Duration is driven by
# `timeout -s INT $DURATION`, NOT a positional arg: cpudist's positional is
# (interval [count]), so `cpudist $DURATION` would print every DURATION s
# forever and never exit -> `wait` hangs. timeout sends SIGINT at DURATION,
# which BCC catches to flush the histogram then exit -- uniform across both
# tools regardless of each tool's positional-arg semantics. NOTE: BCC prints
# its startup banner to stderr (unbuffered) but the histogram to stdout, which
# Python block-buffers when redirected to a file -> on SIGINT exit the
# histogram sits in the 4KB buffer and never lands in the file. PYTHONUNBUFFERED=1
# forces stdout unbuffered so the SIGINT-triggered print flushes immediately.
# ponytail: single-tool on+off is gone -> two tools, two files. If BCC is
# absent, fall back to perf stat (task-clock = on-CPU s; context-switches =
# off-CPU event count; off-CPU time ~ wall*threads - task-clock).
export PYTHONUNBUFFERED=1
ONOFF_OK=0
if command -v cpudist-bpfcc >/dev/null 2>&1; then
  timeout -s INT "$DURATION" cpudist-bpfcc -p "$PID" >"$OUTDIR/cpudist.txt" 2>&1 &
  ONOFF_OK=1
elif command -v cpudist >/dev/null 2>&1; then
  timeout -s INT "$DURATION" cpudist -p "$PID" >"$OUTDIR/cpudist.txt" 2>&1 &
  ONOFF_OK=1
fi
if command -v offcputime-bpfcc >/dev/null 2>&1; then
  timeout -s INT "$DURATION" offcputime-bpfcc -p "$PID" >"$OUTDIR/offcputime.txt" 2>&1 &
  ONOFF_OK=1
elif command -v offcputime >/dev/null 2>&1; then
  timeout -s INT "$DURATION" offcputime -p "$PID" >"$OUTDIR/offcputime.txt" 2>&1 &
  ONOFF_OK=1
fi
if [[ "$ONOFF_OK" -eq 0 ]]; then
  if command -v perf >/dev/null 2>&1; then
    perf stat -e task-clock,context-switches,cpu-migrations -p "$PID" -- sleep "$DURATION" \
      >"$OUTDIR/perf_oncpu.txt" 2>&1 &
    echo "[fc-perf] NOTE: bcc cpudist/offcputime missing -> perf stat fallback (on-CPU only, no distribution)" >&2
  else
    echo "[fc-perf] WARN: neither bcc nor perf found -> on/off-CPU skipped" >&2
  fi
fi

# ----------------------------------------------------------- 3. topdown L1
case "$ARCH" in
  x86)
    # AMD uProf topdown (frontend/backend-bound, bad-speculation, retiring).
    # v5.2 .deb installs the CLI as `AMDuProfCLI` under a versioned dir:
    # /opt/AMDuProf_5.2-606/bin/AMDuProfCLI. Install + symlink:
    #   sudo dpkg -i amduprof_*.deb
    #   sudo ln -sf /opt/AMDuProf_5.2-606/bin/AMDuProfCLI /usr/local/bin/AMDuProfCLI
    # `profile` = collect + report in one shot (vs `collect`, which needs a
    # separate `report` pass). `--config assess` is AMD's top-down breakdown
    # (the "Assess Performance" config: the 8 PMU events listed by
    # `AMDuProfCLI info --list collect-configs` are exactly the L1 four-bucket
    # set). `-d` seconds; `-o` output dir; `--stdout` echoes the report into
    # uprof.log so the four L1 buckets are visible immediately. Requires
    # perf_event_paranoid = 0/-1. The Power Profiler driver warning at install
    # time is irrelevant (power metrics only).
    #
    # Collection target: the FC is already pinned to the bench's pin CPUs, so
    # system-wide on those cores (`-a -c "$CORES"`) == the FC's own topdown --
    # and `-p PID` attach is unreliable for `assess` anyway (profile help notes
    # -p is "CpuProfiler specific reports" only). Pass `-c` the same core list
    # the bench pinned. If `-c` is omitted, fall back to `-p $PID` and warn.
    if command -v AMDuProfCLI >/dev/null 2>&1; then
      if [[ -n "$CORES" ]]; then
        AMDuProfCLI profile --config assess -a -c "$CORES" -d "$DURATION" \
          -o "$OUTDIR/uprof" --stdout >"$OUTDIR/uprof.log" 2>&1 &
      else
        echo "[fc-perf] NOTE: -c CORES not set -> falling back to -p $PID (uProf may reject -p for the assess config); pass -c <pin cores> for a clean L1 topdown" >&2
        AMDuProfCLI profile --config assess -p "$PID" -d "$DURATION" \
          -o "$OUTDIR/uprof" --stdout >"$OUTDIR/uprof.log" 2>&1 &
      fi
    else
      echo "[fc-perf] ERR: AMDuProfCLI not in PATH; install the .deb and symlink /opt/AMDuProf_5.2-606/bin/AMDuProfCLI into /usr/local/bin" >&2
    fi
    ;;
  arm)
    # ARM topdown via devkit (user-supplied). If your devkit flags differ from
    # `-p PID -d DURATION`, edit the command below -- the rest of the script is
    # arch-agnostic. Same four L1 buckets as x86 uProf above.
    if command -v devkit >/dev/null 2>&1; then
      devkit topdown -p "$PID" -d "$DURATION" >"$OUTDIR/devkit_topdown.txt" 2>&1 &
    else
      echo "[fc-perf] ERR: devkit not in PATH; set your ARM topdown tool" >&2
    fi
    ;;
esac

wait || true
echo "[fc-perf] done. outputs:"
ls -1 "$OUTDIR"
