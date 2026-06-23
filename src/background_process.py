"""后台进程管理器 — 负责守护子进程的生命周期。

生命周期与 WxautoPlatformAdapter.connect() → disconnect() 一致：
    await manager.start_all()   在 connect() 末尾调用
    await manager.stop_all()    在 disconnect() 开头调用

子进程通过 asyncio.create_subprocess_exec（非 shell=True）启动，
日志可转发至 Python logger（log_path=None）或追加写入文件（log_path=<路径>）。
崩溃后按 RestartPolicy 和指数退避自动重启，上限 60s。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class RestartPolicy(str, Enum):
    NEVER = "never"
    ON_FAILURE = "on_failure"   # 仅非零退出时重启
    ALWAYS = "always"           # 任何退出都重启


@dataclass
class ProcessSpec:
    """子进程声明（只读配置），在 start_all() 前通过 register() 传入。"""

    name: str
    """唯一标识符，用于日志和 /procstat 显示。"""

    command: list[str]
    """argv 列表，禁止 shell=True（防注入）。示例：["python", "server.py", "--port", "8080"]"""

    log_enabled: bool = True
    """False → 丢弃所有 stdout/stderr 输出。"""

    log_path: Optional[str] = None
    """None → 逐行转发至 Python logger（DEBUG 级别）；
    提供路径 → 以追加模式写文件，带时间戳前缀。"""

    restart_policy: RestartPolicy = RestartPolicy.ON_FAILURE

    restart_max_retries: int = 5
    """最大自动重启次数；0 = 无限重试。"""

    restart_base_delay: float = 2.0
    """指数退避基准秒数；实际延迟 = min(base * 2^retry, 60)。"""

    env_extra: Optional[dict[str, str]] = None
    """合并到父进程环境（不替换整个 env，以保留 PATH 等关键变量）。"""

    cwd: Optional[str] = None
    """工作目录；None → 继承调用进程的当前目录。"""

    stop_timeout: float = 5.0
    """terminate() 后等待进程自然退出的最长秒数，超时则 kill()。"""


@dataclass
class ProcessStats:
    """运行时快照（只读），由 get_stats() 返回。"""

    name: str
    state: str              # "running" / "stopped" / "failed"
    pid: Optional[int]
    restart_count: int
    uptime_seconds: float
    last_exit_code: Optional[int]

    @property
    def uptime_str(self) -> str:
        s = int(self.uptime_seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m{s % 60}s"
        return f"{s // 3600}h{(s % 3600) // 60}m"


@dataclass
class _ProcessHandle:
    """单个进程的运行时状态（内部使用）。"""

    spec: ProcessSpec

    process: Optional[asyncio.subprocess.Process] = field(default=None, init=False, repr=False)
    monitor_task: Optional[asyncio.Task] = field(default=None, init=False, repr=False)
    state: str = field(default="stopped", init=False)
    restart_count: int = field(default=0, init=False)
    start_time: float = field(default_factory=time.monotonic, init=False)
    last_exit_code: Optional[int] = field(default=None, init=False)
    # stop_all() 将此置 True，monitor loop 读到后不再重启
    _stopping: bool = field(default=False, init=False, repr=False)


class BackgroundProcessManager:
    """管理一组守护子进程的生命周期。

    典型用法（在 adapter 内）：

        self._proc_manager = BackgroundProcessManager()
        self._proc_manager.register(ProcessSpec(
            name="my-daemon",
            command=["python", "daemon.py"],
            log_path="logs/daemon.log",
        ))
        # connect() 末尾：
        await self._proc_manager.start_all()
        # disconnect() 开头：
        await self._proc_manager.stop_all()
    """

    def __init__(self) -> None:
        self._handles: dict[str, _ProcessHandle] = {}

    # ── 公开 API ──────────────────────────────────────────────────────────────

    def register(self, spec: ProcessSpec) -> None:
        """注册一个子进程规格，必须在 start_all() 前调用。name 重复时抛 ValueError。"""
        if spec.name in self._handles:
            raise ValueError(f"[bg-proc] 进程名重复: {spec.name!r}")
        self._handles[spec.name] = _ProcessHandle(spec=spec)
        logger.debug("[bg-proc] 已注册进程规格: %s", spec.name)

    async def start_all(self) -> None:
        """为每个已注册进程启动独立的监控 asyncio.Task。"""
        for handle in self._handles.values():
            handle._stopping = False
            handle.monitor_task = asyncio.create_task(
                self._monitor(handle),
                name=f"bg-proc:{handle.spec.name}",
            )
        logger.debug("[bg-proc] 已启动 %d 个后台进程监控", len(self._handles))

    async def stop_all(self) -> None:
        """终止所有运行中的子进程并取消监控任务。"""
        # 先标记所有 handle 为停止中，防止 monitor loop 重启
        for handle in self._handles.values():
            handle._stopping = True

        # 并发发送 terminate 信号
        for handle in self._handles.values():
            await self._terminate_handle(handle)

        # 等待所有监控任务退出
        for handle in self._handles.values():
            task = handle.monitor_task
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            handle.monitor_task = None

        logger.info("[bg-proc] 所有后台进程已停止")

    async def restart(self, name: str) -> None:
        """终止指定进程；如果策略允许，monitor loop 会自动重启它。"""
        handle = self._handles.get(name)
        if handle is None:
            raise KeyError(f"[bg-proc] 未知进程: {name!r}")
        proc = handle.process
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                logger.exception("[bg-proc] terminate 失败: name=%s", name)

    def get_stats(self) -> list[ProcessStats]:
        """返回所有注册进程的当前状态快照列表。"""
        now = time.monotonic()
        result: list[ProcessStats] = []
        for handle in self._handles.values():
            uptime = (now - handle.start_time) if handle.state == "running" else 0.0
            pid = handle.process.pid if (handle.process and handle.state == "running") else None
            result.append(ProcessStats(
                name=handle.spec.name,
                state=handle.state,
                pid=pid,
                restart_count=handle.restart_count,
                uptime_seconds=uptime,
                last_exit_code=handle.last_exit_code,
            ))
        return result

    # ── 内部实现 ──────────────────────────────────────────────────────────────

    async def _monitor(self, handle: _ProcessHandle) -> None:
        """单个进程的完整监控循环：启动 → 等待退出 → 按策略重启。"""
        spec = handle.spec
        retry = 0

        while not handle._stopping:
            # 合并环境变量（保留父进程 PATH 等，只追加/覆盖 env_extra 中的键）
            env = dict(os.environ)
            if spec.env_extra:
                env.update(spec.env_extra)

            stdout = asyncio.subprocess.DEVNULL
            stderr = asyncio.subprocess.DEVNULL
            if spec.log_enabled:
                stdout = asyncio.subprocess.PIPE
                stderr = asyncio.subprocess.STDOUT  # stdout+stderr 合并，便于追查时序

            # 启动子进程
            try:
                proc = await asyncio.create_subprocess_exec(
                    *spec.command,
                    stdout=stdout,
                    stderr=stderr,
                    env=env,
                    cwd=spec.cwd,
                )
            except FileNotFoundError:
                logger.debug(
                    "[bg-proc] 命令不存在，不再重试: name=%s command=%s",
                    spec.name, spec.command[0],
                )
                handle.state = "failed"
                return
            except Exception:
                print("[bg-proc] 进程启动失败: name=%s", spec.name)
                handle.state = "failed"
                return

            handle.process = proc
            handle.state = "running"
            handle.start_time = time.monotonic()
            logger.info("[bg-proc] 进程已启动: name=%s pid=%d", spec.name, proc.pid)

            # 并发消费日志和等待进程退出
            drain_task: Optional[asyncio.Task] = None
            if spec.log_enabled:
                drain_task = asyncio.create_task(
                    self._drain_logs(proc, spec),
                    name=f"bg-proc-log:{spec.name}",
                )

            try:
                exit_code = await proc.wait()
            except asyncio.CancelledError:
                # stop_all() 取消了 monitor task；优雅终止进程后再传播
                try:
                    proc.terminate()
                except Exception:
                    pass
                raise

            # 等待日志消费完毕
            if drain_task:
                drain_task.cancel()
                try:
                    await drain_task
                except (asyncio.CancelledError, Exception):
                    pass

            handle.last_exit_code = exit_code
            handle.state = "stopped"
            logger.info(
                "[bg-proc] 进程已退出: name=%s pid=%d exit_code=%d",
                spec.name, proc.pid, exit_code,
            )

            if handle._stopping:
                break

            # ── 重启判断 ──────────────────────────────────────────────────────
            if spec.restart_policy == RestartPolicy.NEVER:
                break

            if spec.restart_policy == RestartPolicy.ON_FAILURE and exit_code == 0:
                logger.debug("[bg-proc] 正常退出（exit_code=0），不重启: name=%s", spec.name)
                break

            if spec.restart_max_retries > 0 and retry >= spec.restart_max_retries:
                logger.error(
                    "[bg-proc] 已达最大重试次数（%d），停止监控: name=%s",
                    spec.restart_max_retries, spec.name,
                )
                handle.state = "failed"
                break

            delay = min(spec.restart_base_delay * (2 ** retry), 60.0)
            retry += 1
            handle.restart_count += 1
            logger.warning(
                "[bg-proc] %.1fs 后重启（第 %d 次）: name=%s",
                delay, handle.restart_count, spec.name,
            )
            await asyncio.sleep(delay)

    async def _drain_logs(
        self, proc: asyncio.subprocess.Process, spec: ProcessSpec
    ) -> None:
        """逐行读取子进程 stdout，写入日志文件或转发至 Python logger。"""
        if proc.stdout is None:
            return

        log_file = None
        if spec.log_path:
            try:
                Path(spec.log_path).parent.mkdir(parents=True, exist_ok=True)
                # buffering=1 → 行缓冲，保证每行立即落盘
                log_file = open(spec.log_path, "a", encoding="utf-8", buffering=1)
            except Exception:
                logger.exception("[bg-proc] 无法打开日志文件: path=%s name=%s", spec.log_path, spec.name)

        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if log_file:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    log_file.write(f"[{ts}] {line}\n")
                else:
                    logger.debug("[bg-proc][%s] %s", spec.name, line)
        finally:
            if log_file:
                log_file.close()

    async def _terminate_handle(self, handle: _ProcessHandle) -> None:
        """对单个进程发 terminate()，超时后 kill()。"""
        proc = handle.process
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except Exception:
            logger.exception("[bg-proc] terminate 异常: name=%s", handle.spec.name)
        try:
            await asyncio.wait_for(proc.wait(), timeout=handle.spec.stop_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[bg-proc] 进程未在 %.1fs 内退出，强制 kill: name=%s pid=%d",
                handle.spec.stop_timeout, handle.spec.name, proc.pid,
            )
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
