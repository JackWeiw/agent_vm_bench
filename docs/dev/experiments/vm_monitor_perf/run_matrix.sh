#!/usr/bin/env bash
# A/B matrix: pre-change tree vs post-change tree, measured by the same harness.
# CPU is time.process_time() of the monitor process, so co-tenancy on this shared
# host does not distort the comparison; wall time is context only.
set -u
HERE=/home/w00943531/workspace/perf
OLD=/home/w00943531/workspace/avbperf
NEW=/home/w00943531/workspace/avbperf-new
OUT=/var/tmp/vmperf/ab_matrix.jsonl
: > "$OUT"

run() {  # label repo shape n mem throttle [extra...]
  echo "== $* ==" >&2
  python3 "$HERE/ab.py" --repo "$2" --shape "$3" --n "$4" --mem-mb "$5" --throttle "$6" "${@:7}" >> "$OUT"
}

# 1) Light fleet: 3840 VMs, no memory mapping -- the pure discovery + jiffies path.
run light-old  "$OLD" light 3840 0 on
run light-new  "$NEW" light 3840 0 on

# 2) Heavy 4 KiB-backed: 192 x 1 GiB (worst case for the page-table walk).
run heavy4k-old     "$OLD" heavy 192 1024 off
run heavy4k-new-off "$NEW" heavy 192 1024 off
run heavy4k-new-on  "$NEW" heavy 192 1024 on

# 3) Heavy THP-backed: same fleet with 2 MiB pages -- what a real guest RAM
#    mapping looks like under THP=always.
run heavythp-old     "$OLD" heavy 192 1024 off --thp
run heavythp-new-off "$NEW" heavy 192 1024 off --thp
run heavythp-new-on  "$NEW" heavy 192 1024 on  --thp

# 4) Wide heavy fleet: 384 x 256 MiB 4 KiB-backed.
run wide4k-old     "$OLD" heavy 384 256 off
run wide4k-new-off "$NEW" heavy 384 256 off
run wide4k-new-on  "$NEW" heavy 384 256 on

echo "done -> $OUT" >&2
