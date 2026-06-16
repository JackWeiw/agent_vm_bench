"""
任务执行模块

负责浏览器任务的执行、结果收集和异常处理
每个沙箱一个独立线程
"""

import time
import random
import threading
from typing import Tuple, List, Dict

from .config import Config
from .schemas import SandboxState, SandboxStatus


class BrowserTaskRunner(threading.Thread):
    """浏览器任务执行器（每个沙箱一个独立线程）"""

    def __init__(
        self,
        state: SandboxState,
        config: Config,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.consecutive_errors = 0

    def run(self) -> None:
        """任务执行主循环"""
        # 等待沙箱端口就绪
        while not self.stop_event.is_set():
            if self.state.creation_metrics.status == SandboxStatus.PORT_READY:
                break
            if self.state.creation_metrics.status in (SandboxStatus.FAILED, SandboxStatus.PORT_FAILED, SandboxStatus.OFFLINE, SandboxStatus.KILLED):
                print(f"[Sandbox{self.state.sandbox_id}] Cannot start tasks: {self.state.creation_metrics.status.value}")
                return
            time.sleep(0.5)

        # 执行浏览器任务循环
        while not self.stop_event.is_set():
            if not self.state.is_alive:
                print(f"[Sandbox{self.state.sandbox_id}] Sandbox offline, stopping tasks")
                break

            # 执行单个浏览器任务
            success, latency = self._run_single_task()

            # 更新指标
            timeout = latency > self.config.browser_timeout
            self.state.browser_metrics.add(latency, success and not timeout, timeout)
            self.state.last_task_time = time.time()

            # 错误处理
            if success and not timeout:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    print(f"[Sandbox{self.state.sandbox_id}] Marked offline (3 consecutive failures)")
                    break

            # 随机间隔，避免请求突增
            sleep_time = random.uniform(
                self.config.browser_interval_min,
                self.config.browser_interval_max
            )
            time.sleep(sleep_time)

        print(f"[Sandbox{self.state.sandbox_id}] Task runner ended")

    def _run_single_task(self) -> Tuple[bool, float]:
        """执行单个浏览器任务

        使用 state.sandbox_obj 句柄执行命令

        返回: (success, latency_seconds)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0

        # 获取当前URL（轮询方式）
        url_idx = self.state.browser_metrics.total_tasks % len(self.config.browser_urls)
        url = self.config.browser_urls[url_idx]

        # 构建浏览器命令
        cmd = f"openclaw browser --browser-profile openclaw open '{url}'"

        start_time = time.perf_counter()
        try:
            result = sbx.commands.run(
                cmd,
                timeout=self.config.browser_timeout + 30,
                user="root"
            )
            elapsed = time.perf_counter() - start_time

            success = result.exit_code == 0
            return success, elapsed
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            print(f"[Sandbox{self.state.sandbox_id}] Task error: {str(e)[:50]}")
            return False, elapsed


class TaskManager:
    """任务管理器 - 管理所有沙箱的任务执行线程"""

    def __init__(
        self,
        config: Config,
        sandbox_states: Dict[int, SandboxState],
        stop_event: threading.Event,
    ):
        self.config = config
        self.sandbox_states = sandbox_states
        self.stop_event = stop_event
        self.runners: List[BrowserTaskRunner] = []

    def start_all(self) -> None:
        """启动所有PORT_READY沙箱的任务执行线程"""
        active_count = 0
        for state in self.sandbox_states.values():
            if state.creation_metrics.status == SandboxStatus.PORT_READY:
                runner = BrowserTaskRunner(state, self.config, self.stop_event)
                self.runners.append(runner)
                runner.start()
                active_count += 1

        print(f"\nStarted {active_count} task runners")

    def wait_all(self, timeout: float = 5.0) -> None:
        """等待所有任务线程结束"""
        for runner in self.runners:
            runner.join(timeout=timeout)