"""
沙箱管理模块

负责E2B沙箱的创建、健康检查、批量控制和关闭
保留沙箱句柄供后续任务执行使用
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Tuple, Optional
from threading import Event

try:
    from e2b import Sandbox
except ImportError:
    # Mock for development/testing without E2B SDK
    class Sandbox:
        @staticmethod
        def create(template, timeout=86400):
            class MockSandbox:
                class MockCommands:
                    def run(self, cmd, timeout=60, user="root"):
                        class Result:
                            exit_code = 0
                        return Result()
                commands = MockCommands()

                def close(self):
                    pass
            return MockSandbox()

from .config import Config
from .schemas import SandboxState, SandboxStatus


class SandboxManager:
    """沙箱生命周期管理"""

    def __init__(self, config: Config, stop_event: Event):
        self.config = config
        self.stop_event = stop_event
        self.sandbox_states: Dict[int, SandboxState] = {}

    def create_all(self) -> Dict[int, SandboxState]:
        """批量创建沙箱

        根据batch配置决定策略：
        - 有batch_size：分批创建，避免资源突增
        - 无配置：全并发创建，测试极限性能

        返回: {sandbox_id: SandboxState}
        """
        if self.config.batch_size and self.config.batch_size > 0:
            return self._create_batched()
        else:
            return self._create_concurrent()

    def _create_batched(self) -> Dict[int, SandboxState]:
        """分批创建沙箱"""
        total = self.config.total_count
        batch_size = self.config.batch_size
        batch_count = self.config.batch_count

        print(f"\n{'='*60}")
        print(f"Batched Sandbox Creation")
        print(f"  Total: {total} sandboxes")
        print(f"  Batches: {batch_count} x {batch_size}")
        print(f"  Interval: {self.config.batch_interval}s")
        print(f"{'='*60}")

        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                print("Stop event detected, aborting creation")
                break

            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)

            print(f"\n[Batch {batch_id}/{batch_count-1}] Creating sandboxes {start_idx+1}-{end_idx}")

            # 并发创建当前批次
            batch_states = self._create_batch_concurrent(batch_id, start_idx, end_idx)
            self.sandbox_states.update(batch_states)

            # 批次间等待（最后一批不等待）
            if batch_id < batch_count - 1 and self.config.batch_interval:
                print(f"Waiting {self.config.batch_interval}s before next batch...")
                time.sleep(self.config.batch_interval)

        return self.sandbox_states

    def _create_batch_concurrent(self, batch_id: int, start: int, end: int) -> Dict[int, SandboxState]:
        """并发创建一个批次的沙箱"""
        states: Dict[int, SandboxState] = {}

        with ThreadPoolExecutor(max_workers=end - start) as executor:
            futures = {}

            for i in range(start, end):
                sandbox_id = i + 1
                state = SandboxState(sandbox_id=sandbox_id, batch_id=batch_id)
                self.sandbox_states[sandbox_id] = state
                future = executor.submit(self._create_single, state)
                futures[future] = sandbox_id

            for future in as_completed(futures):
                sandbox_id = futures[future]
                state = self.sandbox_states[sandbox_id]

                try:
                    success, elapsed, error = future.result()
                    if success:
                        state.creation_metrics.status = SandboxStatus.ACTIVE
                        state.creation_metrics.elapsed = elapsed
                        print(f"[Sandbox{sandbox_id}] Created in {elapsed:.1f}s")
                    else:
                        state.creation_metrics.status = SandboxStatus.FAILED
                        state.creation_metrics.error_msg = error
                        print(f"[Sandbox{sandbox_id}] Failed: {error[:80]}")
                except Exception as e:
                    state.creation_metrics.status = SandboxStatus.FAILED
                    state.creation_metrics.error_msg = str(e)
                    print(f"[Sandbox{sandbox_id}] Exception: {str(e)[:80]}")

        return {i + 1: self.sandbox_states[i + 1] for i in range(start, end)}

    def _create_concurrent(self) -> Dict[int, SandboxState]:
        """全并发创建所有沙箱"""
        total = self.config.total_count

        print(f"\n{'='*60}")
        print(f"Concurrent Sandbox Creation")
        print(f"  Total: {total} sandboxes (full concurrent)")
        print(f"{'='*60}")

        return self._create_batch_concurrent(batch_id=0, start=0, end=total)

    def _create_single(self, state: SandboxState) -> Tuple[bool, float, str]:
        """创建单个沙箱

        关键：保留沙箱句柄到 state.sandbox_obj

        返回: (success, elapsed_seconds, error_message)
        """
        state.creation_metrics.status = SandboxStatus.CREATING
        state.creation_metrics.submit_time = time.time()

        try:
            sbx = Sandbox.create(
                self.config.template,
                timeout=self.config.create_timeout
            )
            # 保留沙箱句柄
            state.sandbox_obj = sbx
            state.creation_metrics.ready_time = time.time()
            elapsed = state.creation_metrics.ready_time - state.creation_metrics.submit_time
            return True, elapsed, ""
        except Exception as e:
            state.creation_metrics.ready_time = time.time()
            return False, 0.0, str(e)

    def check_alive(self, state: SandboxState) -> bool:
        """检查沙箱是否存活"""
        sbx = state.sandbox_obj
        if not sbx or not state.is_alive:
            return False
        try:
            result = sbx.commands.run("echo alive", timeout=10, user="root")
            return result.exit_code == 0
        except Exception:
            return False

    def close_all(self) -> None:
        """关闭所有沙箱"""
        print("\nClosing all sandboxes...")
        closed_count = 0
        for state in self.sandbox_states.values():
            if state.sandbox_obj:
                try:
                    state.sandbox_obj.close()
                    state.creation_metrics.status = SandboxStatus.CLOSED
                    closed_count += 1
                except Exception as e:
                    print(f"[Sandbox{state.sandbox_id}] Close error: {str(e)[:50]}")
        print(f"Closed {closed_count} sandboxes")