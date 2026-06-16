"""
E2B Sandbox Bench - E2B沙箱批量性能测试套件

功能：
- 批量创建E2B沙箱，收集启动性能（时间、成功率、P50/P95/P99延迟）
- 执行浏览器任务，收集执行性能（延迟、吞吐量）
- 监控沙箱存活情况
- 支持分批启动和随机任务间隔
- 实时统计快照 + 最终报告

使用示例：
    python -m e2b_bench --config config/default.yaml
    python -m e2b_bench --config config/default.yaml --total 50 --duration 300
"""

__version__ = "1.0.0"

__all__ = [
    'Config',
    'SandboxState',
    'SandboxStatus',
    'BrowserMetrics',
    'run_benchmark',
]