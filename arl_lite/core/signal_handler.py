"""arl_lite.core.signal_handler

SIGTERM/SIGINT 优雅停止(抄自 ARL celerytask.py):
  signal → mark_stopped() 写回任务状态 → 通知 module 停止 → cleanup

纪律:任务被信号中断时,必须把状态从 RUNNING → STOPPED,end_time 也要写,
否则数据库里会留个孤儿 RUNNING 记录,运维起来就抓瞎。

80 行内实现,Phase 1 必带(生产化分水岭)。
"""
from __future__ import annotations

import signal
import sys
import atexit
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .task_runner import TaskRunner

log = logging.getLogger("arl_lite.signal")


class GracefulShutdown:
    """SIGTERM 优雅停止

    使用方式:
        runner = TaskRunner(workspace_id=1)
        shutdown = GracefulShutdown(runner)
        # 跑任务...
        await runner.run_task(target=..., modules=...)
    """

    def __init__(self, task_runner: "TaskRunner"):
        self.task_runner = task_runner
        self.shutdown_requested = False
        self._installed = False

    def install(self) -> None:
        """注册信号处理(可重入,只生效一次)"""
        if self._installed:
            return
        try:
            signal.signal(signal.SIGTERM, self._handle)
            signal.signal(signal.SIGINT, self._handle)
            atexit.register(self._cleanup)
            self._installed = True
            log.debug("signal handlers installed")
        except ValueError as e:
            # "signal only works in main thread of the main interpreter"
            log.debug(f"signal handler install skipped: {e}")

    def uninstall(self) -> None:
        """注销信号处理(测试用)"""
        if not self._installed:
            return
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        self._installed = False

    def _handle(self, signum: int, frame) -> None:
        """信号处理入口"""
        if self.shutdown_requested:
            # 第二次信号,强制退出(运维真有需要的话)
            log.warning(f"force exit on second signal {signum}")
            sys.exit(1)

        sig_name = signal.Signals(signum).name
        log.warning(f"caught signal {sig_name}, graceful shutdown...")
        self.shutdown_requested = True

        # 关键步骤 1:回写任务状态(抄 ARL)
        try:
            self.task_runner.mark_stopped()
        except Exception as e:
            log.error(f"mark_stopped failed: {e}")

        # 关键步骤 2:通知 module 停止(子模块轮询 should_stop)
        try:
            self.task_runner.request_stop_all()
        except Exception as e:
            log.error(f"request_stop_all failed: {e}")

    def _cleanup(self) -> None:
        """atexit 清理(DB 连接、临时文件等)"""
        try:
            self.task_runner.cleanup()
        except Exception as e:
            log.error(f"cleanup failed: {e}")
