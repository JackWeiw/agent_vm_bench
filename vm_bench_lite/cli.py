#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI Entry Point Module

Command-line interface with argparse and main entry point.
"""

import argparse

from .config import Config
from .coordinator import run_benchmark


def main():
    parser = argparse.ArgumentParser(description='VM Bench Lite v2')
    parser.add_argument('-n', '--vms', type=int, default=80, help='Total VM count')
    parser.add_argument('--stress-percent', type=float, default=0.5, help='Percentage of VMs to run stress_tool')
    parser.add_argument('--stress-memory', type=int, default=2048, help='Stress memory MB')
    parser.add_argument('--no-keepalive', action='store_true', help='Disable Stress keepalive')
    parser.add_argument('--batch-size', type=int, default=10, help='VMs per batch')
    parser.add_argument('--batch-interval', type=int, default=30, help='Batch interval seconds')
    parser.add_argument('--task-interval', type=float, default=1.0, help='Task interval within batch')
    parser.add_argument('--browser-interval-min', type=float, default=0.5, help='Browser task random interval minimum')
    parser.add_argument('--browser-interval-max', type=float, default=3.0, help='Browser task random interval maximum')
    parser.add_argument('--mode', choices=['cli', 'http'], default='cli', help='Interaction mode')
    parser.add_argument('--browser-mode', action='store_true', help='Enable browser testing')
    parser.add_argument('--browser-url', type=str, default='http://192.168.110.10:8080/Weibo.html', help='Browser test URL')
    parser.add_argument('--browser-use-llm', action='store_true', help='Browser task uses LLM')
    parser.add_argument('-wp', '--warmup-phase', action='store_true', help='Run warmup phase only')
    parser.add_argument('-bsp', '--browser-stress-percent', type=float, default=1.0, help='Browser benchmark VM percent')
    parser.add_argument('--warmup-url', type=str, action='append', help='Warmup page URL')
    parser.add_argument('--warmup-loops', type=int, default=1, help='Warmup loop count')
    parser.add_argument('--warmup-delay', type=int, default=2, help='Warmup page delay (seconds)')
    parser.add_argument('-t', '--duration', type=int, default=600, help='Total test duration')
    parser.add_argument('--start-ip', default='192.168.110.11', help='Starting IP')
    parser.add_argument('-u', '--username', default='root', help='SSH username')
    parser.add_argument('-p', '--password', default='openEuler12#$', help='SSH password')

    args = parser.parse_args()

    config = Config(
        total_vms=args.vms, stress_percent=args.stress_percent,
        batch_size=args.batch_size, batch_interval=args.batch_interval,
        stress_memory_mb=args.stress_memory, stress_keepalive=not args.no_keepalive,
        mode=args.mode, browser_mode=args.browser_mode,
        browser_url=args.browser_url, browser_use_llm=args.browser_use_llm,
        is_warmup_phase=args.warmup_phase,
        browser_stress_percent=args.browser_stress_percent,
        warmup_urls=args.warmup_url or [],
        warmup_loops=args.warmup_loops, warmup_delay=args.warmup_delay,
        test_duration=args.duration,
        start_ip=args.start_ip, username=args.username, password=args.password,
        task_interval=args.task_interval,
        browser_task_interval_min=args.browser_interval_min,
        browser_task_interval_max=args.browser_interval_max
    )

    run_benchmark(config)


if __name__ == "__main__":
    main()
