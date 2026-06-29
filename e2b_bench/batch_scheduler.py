"""
Batch Test Scheduler Module

Orchestrates batch testing with sandbox reuse strategy.
Groups tasks by (total_count, ratio) and reuses sandbox/smap_tool within groups.
"""

import os
import sys
import time
import argparse
import yaml
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from .config import Config
from .schemas import BatchTask, TaskGroup, SandboxState, SandboxStatus
from .task_generator import TaskGenerator, load_matrix_config
from .metrics_extractor import MetricsExtractor
from .report_aggregator import ReportAggregator
from .sandbox_manager import SandboxManager
from .task_runner import TaskManager
from .stats_collector import StatsCollector
from .bench import SmapToolManager, VmMonitorManager


class GroupRunner:
    """Run a single TaskGroup with sandbox reuse"""

    def __init__(self, group: TaskGroup, config: Config, batch_log_file: str):
        self.group = group
        self.config = config
        self.batch_log_file = batch_log_file

        # Runtime managers (shared within group)
        self.sandbox_manager: Optional[SandboxManager] = None
        self.smap_tool: Optional[SmapToolManager] = None
        self.sandbox_states: Dict[int, SandboxState] = {}

        # Stop event
        self.stop_event = None

    def run(self) -> List[BatchTask]:
        """
        Execute all tasks in the group

        Flow:
        1. Create sandboxes (shared)
        2. Start smap_tool (shared)
        3. Warmup (shared, once)
        4. For each task:
           - Start vm_monitor
           - Run benchmark
           - Stop vm_monitor
           - Collect results
        5. Cleanup

        Returns:
            List of completed BatchTask objects
        """
        results = []
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._log(f"\n[{timestamp}] Starting group: {self.group.group_id}")
        self._log(f"  Total count: {self.group.total_count}")
        self._log(f"  Ratio: {self.group.ratio}")
        self._log(f"  Tasks: {len(self.group.tasks)}")

        print(f"\n{'='*60}")
        print(f"Group: {self.group.group_id}")
        print(f"{'='*60}")

        try:
            # 1. Create sandboxes
            self.stop_event = threading.Event()
            self.sandbox_manager = SandboxManager(self._get_group_config(), self.stop_event)

            print(f"\n[Phase 1] Creating {self.group.total_count} sandboxes...")
            self.sandbox_states = self.sandbox_manager.create_all()

            ready_count = sum(
                1 for s in self.sandbox_states.values()
                if s.creation_metrics.status == SandboxStatus.PORT_READY
            )
            if ready_count == 0:
                self._log(f"  ERROR: No sandboxes ready")
                for task in self.group.tasks:
                    task.success = False
                    task.error_msg = "No sandboxes ready"
                return self.group.tasks

            self._log(f"  Sandboxes ready: {ready_count}")

            # 2. Start smap_tool
            if self.config.smap_tool_enabled:
                self.smap_tool = SmapToolManager(self._get_group_config())
                success = self.smap_tool.start(ready_count)
                if not success:
                    self._log(f"  WARN: smap_tool failed to start")

            # 3. Warmup (shared, once)
            if self.config.warmup_urls:
                print(f"\n[Phase 2] Running warmup...")
                task_manager = TaskManager(self._get_group_config(), self.sandbox_states, self.stop_event)
                task_manager.start_warmup()
                task_manager.wait_warmup()
                self._log(f"  Warmup completed")

            # 4. Run each task with different benchmark_percent
            for idx, task in enumerate(self.group.tasks):
                print(f"\n[Phase 3.{idx+1}] Task: {task.task_id}")
                self._log(f"\n  Task {idx+1}/{len(self.group.tasks)}: {task.task_id}")
                self._log(f"    benchmark_percent: {task.benchmark_percent}")

                self._run_single_task(task, idx)
                results.append(task)

        except Exception as e:
            self._log(f"  ERROR: Group execution failed: {e}")
            for task in self.group.tasks:
                task.success = False
                task.error_msg = str(e)

        finally:
            # 5. Cleanup
            self._cleanup()

        return results

    def _run_single_task(self, task: BatchTask, task_idx: int) -> None:
        """Run a single benchmark task (modifies task in-place)"""
        # Create result directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        result_dir = Path(self.config.output_dir) / f"{task.task_id}_{timestamp}"
        result_dir.mkdir(parents=True, exist_ok=True)
        task.result_dir = str(result_dir)

        # Update config for this task
        task_config = self._get_task_config(task)

        # Save task config to result directory for reference
        config_dict = {
            'task_id': task.task_id,
            'total_count': task.total_count,
            'benchmark_percent': task.benchmark_percent,
            'ratio': task.ratio,
            'smap_tool_enabled': self.config.smap_tool_enabled,
            'smap_tool_ratio': task.ratio if self.config.smap_tool_enabled else None,
            'test_duration': self.config.test_duration,
            'browser_urls': self.config.browser_urls,
            'warmup_urls': self.config.warmup_urls if self.config.warmup_urls else [],
        }
        # Use task_id (matrix value) as config filename
        config_file = result_dir / f"{task.task_id}.yaml"
        with open(config_file, 'w', encoding='utf-8') as f:
            yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True)
        print(f"  Task config saved to: {config_file}")

        # Start vm_monitor
        vm_monitor = None
        if self.config.vm_monitor_enabled:
            vm_monitor = VmMonitorManager(task_config)
            vm_monitor.start(task.task_id)
            time.sleep(2)  # Wait for vm_monitor to initialize

        # Create stop event for this task
        stop_event = threading.Event()

        # Start stats collector
        stats_collector = StatsCollector(task_config, self.sandbox_states)
        stats_collector.start()

        # Start task manager
        task_manager = TaskManager(task_config, self.sandbox_states, stop_event)

        # Trigger vm_monitor sampling
        if vm_monitor:
            vm_monitor.trigger_sampling()

        # Start browser tasks
        print(f"  Starting browser tasks (benchmark_percent={task.benchmark_percent})...")
        task_manager.start_all()

        # Run for duration
        print(f"  Running for {self.config.test_duration}s...")
        time.sleep(self.config.test_duration)

        # Stop
        stop_event.set()
        task_manager.wait_all(timeout=5)
        stats_collector.stop()

        # Stop vm_monitor sampling
        if vm_monitor:
            vm_monitor.stop_sampling()

        # Generate bench report
        report = stats_collector.generate_report()
        report_file = result_dir / "bench_report.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        task.report_file = str(report_file)

        # Wait for vm_monitor analysis report
        if vm_monitor:
            analysis_file = vm_monitor.wait_for_report(timeout=300)
            if analysis_file:
                task.analysis_file = analysis_file
            vm_monitor.stop()

        # Mark success
        task.success = True
        self._log(f"    Task completed successfully")

    def _get_group_config(self) -> Config:
        """Create Config for group (with group's total_count and ratio)"""
        # Create a copy of config with group's parameters
        # For simplicity, modify existing config
        group_config = Config(
            **{k: v for k, v in self.config.__dict__.items()},
        )
        group_config.total_count = self.group.total_count
        group_config.smap_tool_ratio = self.group.ratio

        return group_config

    def _get_task_config(self, task: BatchTask) -> Config:
        """Create Config for specific task"""
        task_config = self._get_group_config()
        task_config.benchmark_percent = task.benchmark_percent
        task_config.vm_monitor_duration = self.config.test_duration
        return task_config

    def _cleanup(self):
        """Cleanup: stop smap_tool, kill sandboxes"""
        print(f"\n[Cleanup] Group: {self.group.group_id}")

        if self.smap_tool:
            self.smap_tool.stop()
            self._log("  smap_tool stopped")

        if self.sandbox_manager:
            self.sandbox_manager.kill_all()
            self._log("  Sandboxes killed")

        if self.stop_event:
            self.stop_event.set()

    def _log(self, message: str):
        """Write to batch log file"""
        with open(self.batch_log_file, 'a', encoding='utf-8') as f:
            f.write(message + '\n')


class BatchScheduler:
    """Main batch test scheduler"""

    def __init__(self, matrix_path: str, template_path: str, output_dir: str = "results/e2b/batch"):
        self.matrix_path = matrix_path
        self.template_path = template_path
        self.output_dir = output_dir

        # Load configurations
        self.matrix_config = load_matrix_config(matrix_path)
        self.template_config = Config.load_from_yaml(template_path)

        # Apply output_dir override
        if output_dir:
            self.template_config.output_dir = output_dir

        # Initialize components
        self.task_generator = TaskGenerator(self.matrix_config)
        self.metrics_extractor = MetricsExtractor()
        self.report_aggregator = ReportAggregator(output_dir)

        # Batch log file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.batch_log_file = os.path.join(output_dir, f"batch_log_{timestamp}.txt")
        os.makedirs(output_dir, exist_ok=True)

    def run(self, continue_on_failure: bool = True) -> str:
        """
        Execute all batch tests

        Args:
            continue_on_failure: Continue testing if a group fails

        Returns:
            Path to summary report Excel file
        """
        print("\n" + "="*80)
        print("E2B Batch Test Scheduler")
        print("="*80)

        # Print configuration
        print(f"\nMatrix: {self.matrix_path}")
        print(f"Template: {self.template_path}")
        print(f"Output: {self.output_dir}")

        groups = self.task_generator.generate_groups()
        print(f"\nGroups: {len(groups)}")
        print(f"Total tasks: {self.task_generator.get_total_task_count()}")

        # Setup E2B environment
        self.template_config.setup_e2b_env()

        # Execute each group
        all_results: List[BatchTask] = []

        for idx, group in enumerate(groups):
            print(f"\n{'='*80}")
            print(f"Group {idx+1}/{len(groups)}: {group.group_id}")
            print(f"{'='*80}")

            runner = GroupRunner(group, self.template_config, self.batch_log_file)
            results = runner.run()
            all_results.extend(results)

            # Check for failures
            failed = [t for t in results if not t.success]
            if failed and not continue_on_failure:
                print(f"\nGroup failed, stopping (continue_on_failure=False)")
                break

        # Extract metrics
        print(f"\n{'='*80}")
        print("Extracting metrics...")
        print(f"{'='*80}")

        metrics_data = []
        for task in all_results:
            metrics = {
                'task_id': task.task_id,
                'total_count': task.total_count,
                'ratio': task.ratio,
                'benchmark_percent': task.benchmark_percent,
                'success': task.success,
                'error_msg': task.error_msg,
            }

            # Extract browser metrics
            if task.report_file:
                browser_metrics = self.metrics_extractor.extract_browser_metrics(task.report_file)
                metrics.update(browser_metrics)
                task.browser_metrics = browser_metrics

            # Extract vm_monitor metrics
            if task.analysis_file:
                vm_metrics = self.metrics_extractor.extract(task.analysis_file)
                metrics.update(vm_metrics)
                task.vm_metrics = vm_metrics

            metrics_data.append(metrics)

        # Aggregate results
        print(f"\n{'='*80}")
        print("Generating summary report...")
        print(f"{'='*80}")

        report_path = self.report_aggregator.aggregate(metrics_data)

        # Print final summary
        success_count = sum(1 for t in all_results if t.success)
        failed_count = len(all_results) - success_count

        print(f"\n{'='*80}")
        print("Batch Test Complete")
        print(f"{'='*80}")
        print(f"  Total tasks: {len(all_results)}")
        print(f"  Successful: {success_count}")
        print(f"  Failed: {failed_count}")
        print(f"  Summary report: {report_path}")

        return report_path


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser"""
    parser = argparse.ArgumentParser(
        description='E2B Batch Test Scheduler - Automated batch testing with sandbox reuse'
    )

    parser.add_argument('--matrix', required=True, help='Test matrix YAML config path')
    parser.add_argument('--template', required=True, help='Template YAML config path')
    parser.add_argument('--output-dir', default='results/e2b/batch', help='Output directory')
    parser.add_argument('--continue-on-failure', action='store_true',
                        help='Continue testing if a group fails')

    return parser


def main():
    """CLI entry point"""
    parser = build_arg_parser()
    args = parser.parse_args()

    scheduler = BatchScheduler(
        matrix_path=args.matrix,
        template_path=args.template,
        output_dir=args.output_dir
    )

    report_path = scheduler.run(continue_on_failure=args.continue_on_failure)

    print(f"\nDone. Report: {report_path}")


if __name__ == '__main__':
    main()