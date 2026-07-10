#!/usr/bin/env python3
"""
smap_bw.py — SMAP_migrate 周期带宽计算工具

每个迁移周期:
  阶段1: from 1 to 5  (迁移开始)
  阶段2: from 5 to 1  (迁移结束)
  当方向再次从 5→1 变回 1→5 时，报告上一个周期的带宽。

公式: bandwidth = sum(nr) × 2 / 1024 / Δt  GB/s

用法:
    sudo python3 smap_bw.py [选项]

选项:
    --file FILE       指定一个日志文件作为输入源，脚本从文件读取并分析。
                      不加此参数时，脚本会调用 dmesg -w 实时监控内核日志。

    --clear           开始监控前先执行 dmesg -C 清空内核日志缓冲区。
                      仅在实时监控模式下有意义，确保只采集新的事件，不被旧日志干扰。

    --timeout TIMEOUT 实时监控模式下，如果连续 TIMEOUT 秒没有新的 SMAP_migrate
                      事件，脚本自动停止。默认 10 秒。文件模式下不生效（文件读完即结束）。

    --duration SECS   从第一条 SMAP_migrate 事件开始计时，超过 SECS 秒后自动停止。
                      用于限定采集时长，与 --timeout 互补：
                      --timeout 控制"无新事件"的等待，--duration 控制"总采集时长"。

    --debug           显示每一条匹配到的原始行和解析过程，方便排查正则是否正确匹配、
                      是否有格式异常。

示例:
    # 离线分析已有文件
    python3 smap_bw.py --file dmesg_log.txt

    # 实时监控：先清空旧日志，30秒无新事件则自动退出
    sudo python3 smap_bw.py --clear --timeout 30

    # 实时监控：采集 60 秒后自动停止
    sudo python3 smap_bw.py --clear --duration 60

    # 采集 60 秒，期间 15 秒无新事件也提前退出
    sudo python3 smap_bw.py --clear --duration 60 --timeout 15

    # 实时监控 + 调试：查看每行的解析细节
    sudo python3 smap_bw.py --clear --timeout 30 --debug

    # 从管道读取
    dmesg -w | python3 smap_bw.py
"""

import argparse
import re
import subprocess
import sys
import threading
import time

G = "\033[32m"
Y = "\033[33m"
C = "\033[36m"
R = "\033[31m"
B = "\033[1m"
D = "\033[2m"
E = "\033[0m"

RE_MIGRATE = re.compile(
    r"$$\s*(\d+\.\d+)$$\s+SMAP_migrate:\s+$$(\d+)$$\s+" r"pid\s+(\d+)\s+from\s+(\d+)\s+to\s+(\d+)\s+nr\s+(\d+)"
)


def parse_line(line):
    line = line.strip()
    if not line:
        return None
    m = RE_MIGRATE.search(line)
    if m:
        return dict(
            ts=float(m.group(1)),
            seq=int(m.group(2)),
            pid=int(m.group(3)),
            frm=int(m.group(4)),
            to=int(m.group(5)),
            nr=int(m.group(6)),
        )
    # 备用
    if "SMAP_migrate" not in line:
        return None
    try:
        bracket_ts, rest = line.split("]", 1)
        ts = float(bracket_ts.lstrip("[").strip())
        parts = rest.split()
        idx = 0
        for i, p in enumerate(parts):
            if p.startswith("SMAP_migrate"):
                idx = i + 1
                break
        seq = 0
        for i in range(idx, len(parts)):
            if parts[i].startswith("["):
                seq = int(parts[i].strip("[]"))
                idx = i + 1
                break

        def find_val(kw):
            for j in range(idx, len(parts) - 1):
                if parts[j] == kw:
                    return int(parts[j + 1])
            return 0

        pid = find_val("pid")
        frm = find_val("from")
        to = find_val("to")
        nr = find_val("nr")
        if nr > 0:
            return dict(ts=ts, seq=seq, pid=pid, frm=frm, to=to, nr=nr)
    except (ValueError, IndexError):
        pass
    return None


def report_cycle(cycle_no, records):
    n = len(records)
    total_nr = sum(r["nr"] for r in records)
    t0 = records[0]["ts"]
    t1 = records[-1]["ts"]
    dt = t1 - t0
    if dt <= 0:
        dt = 0.000001

    gb = total_nr * 2 / 1024
    bw = gb / dt

    nodes = {}
    for r in records:
        key = (r["frm"], r["to"])
        nodes[key] = nodes.get(key, 0) + r["nr"]

    print(
        f"""
{B}╔══════════════════════════════════════════════╗
║           周期 {cycle_no:>3d} 迁移带宽报告            ║
╠══════════════════════════════════════════════╣{E}
  事件总数:     {n}
  起始时间:     {t0:.6f} s
  结束时间:     {t1:.6f} s
  持续时长:     {dt:.6f} s
  累计页数:     {total_nr}
  数据量:       {gb:.4f} GB
{B}  ────────────────────────────────────────────{E}
  迁移方向统计:"""
    )
    for (f, t_), nr in sorted(nodes.items()):
        print(f"    node {f} → {t_}:  {nr} pages")
    print(
        f"""{B}  ────────────────────────────────────────────{E}
{Y}{B}  迁移带宽:     {bw:.4f} GB/s{E}
{B}╚══════════════════════════════════════════════╝{E}"""
    )
    return total_nr, dt, bw


def main():
    ap = argparse.ArgumentParser(
        description="SMAP_migrate 周期带宽计算工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  sudo python3 smap_bw.py --clear --timeout 15\n"
        "  python3 smap_bw.py --file dmesg_log.txt\n"
        "  dmesg -w | python3 smap_bw.py\n",
    )
    ap.add_argument("--timeout", type=float, default=10, help="无新事件超时秒数 (默认 10)")
    ap.add_argument("--duration", type=float, default=None, help="从首条事件起采集时长上限 (秒)")
    ap.add_argument("--clear", action="store_true", help="开始前清空 dmesg 缓冲区")
    ap.add_argument("--file", type=str, default=None, help="从文件读取日志")
    ap.add_argument("--debug", action="store_true", help="显示解析过程")
    args = ap.parse_args()

    print(
        f"""
{C}{B}┌──────────────────────────────────────────────┐
│     SMAP_migrate 周期带宽计算工具            │
│     检测方向切换: 1→5 阶段 + 5→1 阶段       │
└──────────────────────────────────────────────┘{E}
"""
    )

    if args.clear:
        subprocess.run(["dmesg", "-C"], check=True, capture_output=True)
        print(f"{D}  已清空 dmesg 缓冲区{E}")

    proc = None
    if args.file:
        source = open(args.file, errors="replace")
        label = f"文件: {args.file}"
    elif not sys.stdin.isatty():
        source = sys.stdin
        label = "stdin 管道"
    else:
        proc = subprocess.Popen(
            ["dmesg", "-w"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        source = proc.stdout
        label = f"dmesg -w (超时 {args.timeout}s)"

    print(f"{D}  数据源: {label}{E}")
    if args.duration is not None:
        print(f"{D}  采集时长上限: {args.duration}s{E}")
    print()

    # ── 状态 ──────────────────────────────────────────
    collecting = False
    current_records = []
    prev_direction = None
    cycle_no = 0
    all_bw = []
    wall_start = None  # 首条事件的 wall-clock 时间
    stopped = threading.Event()
    stop_reason = [None]  # 记录停止原因
    timer_lock = threading.Lock()
    timer = [None]

    def on_timeout():
        stop_reason[0] = "timeout"
        stopped.set()

    def reset_timer():
        with timer_lock:
            if timer[0]:
                timer[0].cancel()
            t = threading.Timer(args.timeout, on_timeout)
            t.daemon = True
            t.start()
            timer[0] = t

    def cleanup():
        with timer_lock:
            if timer[0]:
                timer[0].cancel()
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        if hasattr(source, "close"):
            source.close()

    def finish_cycle():
        nonlocal cycle_no
        if not current_records:
            return
        cycle_no += 1
        t, d, b = report_cycle(cycle_no, current_records)
        all_bw.append((t, d, b))
        current_records.clear()

    need_timer = proc is not None

    try:
        if need_timer:
            reset_timer()

        for raw in source:
            if stopped.is_set():
                break

            rec = parse_line(raw)
            if rec is None:
                continue

            # ── 首条事件: 记录 wall-clock ─────────────
            if wall_start is None:
                wall_start = time.monotonic()

            # ── duration 检查 ─────────────────────────
            if args.duration is not None:
                elapsed = time.monotonic() - wall_start
                if elapsed >= args.duration:
                    stop_reason[0] = "duration"
                    # 不 break，先把当前事件处理完再退出
                    stopped.set()

            direction = (rec["frm"], rec["to"])

            if collecting and prev_direction == (5, 1) and direction == (1, 5):
                finish_cycle()

            if direction == (1, 5):
                collecting = True

            if collecting:
                current_records.append(rec)
                el = current_records[-1]["ts"] - current_records[0]["ts"]
                cum = sum(r["nr"] for r in current_records)
                if args.debug or (cycle_no == 0 and len(current_records) <= 3):
                    print(
                        "  {}[{:4d}]{} pid={:<10d} {}->{} nr={:<6d} │ cycle={:<3d} cum_nr={:<8d} dt={:.6f}s".format(
                            D, rec["seq"], E, rec["pid"], rec["frm"], rec["to"], rec["nr"], cycle_no + 1, cum, el
                        )
                    )

            prev_direction = direction

            if need_timer:
                reset_timer()

            if stopped.is_set():
                break

        else:
            if collecting:
                print(f"\n{D}  (输入结束，处理最后一个不完整周期){E}")

        if stop_reason[0] == "timeout":
            print(f"\n{Y}[TIMEOUT]{E} 超时 {args.timeout}s 无新事件")
        elif stop_reason[0] == "duration":
            wall_dur = time.monotonic() - wall_start if wall_start else 0
            print(f"\n{Y}[DURATION]{E} 已采集 {wall_dur:.1f}s，达到时长上限 {args.duration}s")

    except KeyboardInterrupt:
        print(f"\n{Y}[STOP]{E} 用户中断")
    finally:
        cleanup()

    # ── 处理最后一个周期 ──────────────────────────────
    if current_records:
        finish_cycle()

    # ── 全局汇总 ─────────────────────────────────────
    if all_bw:
        total_pages = sum(x[0] for x in all_bw)
        avg_bw = sum(x[2] for x in all_bw) / len(all_bw)
        print(
            f"""
{C}{B}┌──────────────────────────────────────────────┐
│                全局汇总                      │
├──────────────────────────────────────────────┤{E}
  周期总数:     {len(all_bw)}
  总页数:       {total_pages}
  平均带宽:     {avg_bw:.4f} GB/s
  周期带宽范围: {min(x[2] for x in all_bw):.4f} ~ {max(x[2] for x in all_bw):.4f} GB/s
{B}└──────────────────────────────────────────────┘{E}"""
        )
    else:
        print(f"\n{R}  未捕获到任何 SMAP_migrate 迁移周期。{E}")


if __name__ == "__main__":
    main()
