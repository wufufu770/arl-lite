"""arl_lite.devloop.lock — 跨进程文件锁(纯 stdlib)

## 为什么需要

devloop 的队列和状态都是 `load() → 改 → save()`。`save()` 走的是
`tmp + os.replace`,那保证了**文件不会写坏**,但保证不了**读-改-写序列
整体原子**。

实测两个 agent 各领一条活、各自保存:

    A 领到 task-0,B 领到 task-1
    A.save() → B.save()
    最终: task-0 退回 pending,A 的认领被静默覆盖

A 正在干的活重新变回可领状态,第三个 agent 会重复领走它,两边同时改
同一处代码。**"多 agent 同时推进"这条需求就卡在这儿**。

## 为什么不用 fcntl 一家

`fcntl` 是 POSIX only。arl-lite 虽然主要在 Linux 跑,但 Windows 上
`import fcntl` 直接 ImportError,所以做双路:mtimeoutlib 没有,就用
`msvcrt.locking`。两个都没有的平台(比如某些 WASM 环境)退化成
"不加锁 + 明确告警",**不假装自己是安全的**。

## 用法

    with file_lock(path):
        data = json.loads(path.read_text())
        ...                     # 临界区
        path.write_text(json.dumps(data))

锁文件是 `<target>.lock`,和目标文件同目录。锁本身是**协作式**的 ——
只有都走这个模块的代码才会互斥,直接 `open(path,'w')` 绕不过去。
所以所有写 queue.json / state.json 的路径都必须走这里。
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("arl_lite.devloop.lock")

# 默认等多久。短:拿不到锁通常意味着另一个 agent 正在改,
# 而它的临界区只有一次 json 读写,几毫秒就该结束。
DEFAULT_TIMEOUT = 5.0

# 重试间隔
_POLL = 0.005

try:  # POSIX
    import fcntl
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover — 非 POSIX 平台
    fcntl = None
    _HAVE_FCNTL = False

try:  # Windows
    import msvcrt
    _HAVE_MSVCRT = True
except ImportError:
    msvcrt = None
    _HAVE_MSVCRT = False

# 两个都没有 → 退化成无锁。**必须显式告警**,不能让人以为受保护。
HAVE_LOCKING = _HAVE_FCNTL or _HAVE_MSVCRT

if not HAVE_LOCKING:  # pragma: no cover
    log.warning(
        "devloop.lock: 本平台既无 fcntl 也无 msvcrt,文件锁退化为空操作。"
        "多 agent 并发会丢更新(参见本模块 docstring 的实测)。"
    )


class LockTimeout(RuntimeError):
    """在超时内没拿到锁"""


def lock_path_for(target: Path) -> Path:
    """目标文件对应的锁文件路径"""
    return target.with_name(target.name + ".lock")


@contextmanager
def file_lock(target: Path, timeout: float = DEFAULT_TIMEOUT,
              owner: str = ""):
    """给 target 加一把排他锁。

    Args:
        target: 被保护的文件路径(锁的是 `<target>.lock`)
        timeout: 等锁的最长秒数;超时抛 LockTimeout
        owner: 持锁者标识,只进日志,便于排查是谁卡住了

    Yields:
        None —— 临界区由 with 块界定
    """
    target = Path(target)
    if not HAVE_LOCKING:  # pragma: no cover
        yield
        return

    lp = lock_path_for(target)
    lp.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(lp), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                _try_lock(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"拿不到 {lp} 的锁(等 {timeout}s)。"
                        f"{('持锁者 ' + owner) if owner else '可能有别的 agent 正在写'}"
                    ) from None
                time.sleep(_POLL)

        # 写进锁文件,`fuser`/lsof 之类能直接看出是谁占着
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()} {owner}\n".encode("utf-8"), 0)
        except OSError:
            pass  # 写不进去不影响加锁本身

        yield
    finally:
        if acquired:
            try:
                _unlock(fd)
            except OSError:  # pragma: no cover
                log.debug("unlock %s failed", lp)
        os.close(fd)


def _try_lock(fd: int) -> None:
    if _HAVE_FCNTL:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif _HAVE_MSVCRT:  # pragma: no cover — 仅 Windows
        # msvcrt.locking 锁 1 字节,文件至少得有 1 字节才能锁
        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _unlock(fd: int) -> None:
    if _HAVE_FCNTL:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif _HAVE_MSVCRT:  # pragma: no cover — 仅 Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def is_locked(target: Path) -> bool:
    """试探性地看锁是否被占着(不改变锁状态)

    给 `devloop status` 用:有人正在写队列时提示一下,
    免得看到中间态还以为坏了。
    """
    if not HAVE_LOCKING:  # pragma: no cover
        return False
    lp = lock_path_for(Path(target))
    if not lp.exists():
        return False
    fd = os.open(str(lp), os.O_RDWR)
    try:
        _try_lock(fd)
        _unlock(fd)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def lock_holder(target: Path) -> str:
    """读锁文件里记的持锁者(pid + 名字);没锁着就返回空串"""
    lp = lock_path_for(Path(target))
    try:
        return lp.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
