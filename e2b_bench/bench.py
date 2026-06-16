#!/usr/bin/env python3
"""
E2B Sandbox Bench - 主入口

整合所有组件，运行测试流程：
创建沙箱 → 启动统计 → 启动任务 → 运行时长 → 停止 → 报告
"""

import time
import argparse
import threading

from .config import Config
from .sandbox_manager import SandboxManager
from .task_runner import TaskManager
from .stats_collector import StatsCollector
from .schemas import SandboxStatus


def run_benchmark(config: Config) -> dict:
    """运行E2B沙箱性能测试

    Args:
        config: 测试配置对象

    Returns:
        {'report': str, 'filepath': str}
    """
    # 1. 设置E2B环境变量
    config.setup_e2b_env()

    print("=" * 80)
    print("E2B Sandbox Bench - Batch Performance Test")
    print("=" * 80)
    print(f"  Template: {config.template}")
    print(f"  Total:    {config.total_count} sandboxes")
    if config.batch_size:
        print(f"  Batch:    {config.batch_count} batches x {config.batch_size} (interval {config.batch_interval}s)")
    else:
        print(f"  Batch:    Full concurrent creation")
    print(f"  Duration: {config.test_duration}s")
    print("=" * 80)

    # 停止信号
    stop_event = threading.Event()

    # 2. 创建沙箱
    print("\n[Phase 1] Creating sandboxes...")
    sandbox_manager = SandboxManager(config, stop_event)
    sandbox_states = sandbox_manager.create_all()

    created_count = sum(
        1 for s in sandbox_states.values()
        if s.creation_metrics.status == SandboxStatus.ACTIVE
    )
    if created_count == 0:
        print("No sandboxes created successfully, exiting.")
        return {}

    print(f"\nSuccessfully created: {created_count}/{config.total_count} sandboxes")

    # 3. 启动统计收集
    print("\n[Phase 2] Starting stats collector...")
    stats_collector = StatsCollector(config, sandbox_states)
    stats_collector.start()

    # 4. 启动任务执行
    print("\n[Phase 3] Starting browser tasks...")
    task_manager = TaskManager(config, sandbox_states, stop_event)
    task_manager.start_all()

    # 5. 运行指定时长
    print(f"\n[Phase 4] Running for {config.test_duration} seconds...")
    try:
        time.sleep(config.test_duration)
    except KeyboardInterrupt:
        print("\nUser interrupt, stopping...")

    # 6. 停止所有组件
    print("\n[Phase 5] Stopping...")
    stop_event.set()
    task_manager.wait_all(timeout=5)
    stats_collector.stop()
    sandbox_manager.close_all()

    time.sleep(0.5)  # 让守护线程完成最后的输出

    # 7. 生成并保存报告
    report = stats_collector.generate_report()
    print("\n" + report)

    filepath = stats_collector.save_report(report)
    print(f"\nReport saved to: {filepath}")

    return {'report': report, 'filepath': filepath}


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        description='E2B Sandbox Bench - E2B沙箱批量性能测试工具'
    )

    # 配置文件
    parser.add_argument('--config', type=str, default=None,
                        help='YAML configuration file path')

    # E2B环境变量
    parser.add_argument('--e2b-access-token', type=str, help='E2B access token')
    parser.add_argument('--e2b-api-key', type=str, help='E2B API key')
    parser.add_argument('--e2b-domain', type=str, help='E2B domain')
    parser.add_argument('--e2b-api-url', type=str, help='E2B API URL')
    parser.add_argument('--e2b-http-ssl', type=str, help='E2B HTTP SSL setting')

    # 沙箱配置
    parser.add_argument('--template', type=str, help='E2B template name')
    parser.add_argument('--total', type=int, help='Total sandbox count')
    parser.add_argument('--create-timeout', type=int, help='Sandbox creation timeout')

    # 批量控制
    parser.add_argument('--batch-size', type=int, help='Sandboxes per batch (None = full concurrent)')
    parser.add_argument('--batch-interval', type=int, help='Batch interval seconds')

    # 浏览器任务
    parser.add_argument('--browser-url', type=str, action='append', help='Browser URL (can specify multiple)')
    parser.add_argument('--browser-timeout', type=int, help='Browser task timeout')
    parser.add_argument('--browser-interval-min', type=float, help='Task interval minimum')
    parser.add_argument('--browser-interval-max', type=float, help='Task interval maximum')

    # 测试运行
    parser.add_argument('--duration', type=int, help='Test duration seconds')
    parser.add_argument('--stats-interval', type=int, help='Stats snapshot interval')

    # 报告
    parser.add_argument('--output-dir', type=str, help='Report output directory')
    parser.add_argument('--filename-prefix', type=str, help='Report filename prefix')

    return parser


def main() -> None:
    """命令行入口"""
    parser = build_arg_parser()
    args = parser.parse_args()

    # 加载配置
    if args.config:
        config = Config.load_from_yaml(args.config)
        config = Config.merge_with_args(config, args)
    else:
        # 无配置文件时，使用命令行参数
        config = Config.from_args(args)

    # 验证必填参数
    if not config.e2b_access_token and not args.config:
        print("Error: E2B access token is required. Use --e2b-access-token or --config")
        return

    # 运行测试
    run_benchmark(config)


if __name__ == "__main__":
    main()