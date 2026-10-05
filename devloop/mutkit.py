"""变异测试的全局互斥锁(r113 新增)

## 为什么需要它

变异测试的做法是「读备份 → 施加变异 → 跑判据 → 写回备份」。这套做法在
**单个进程内**是对的,但它跨进程**不安全**:谁拿到的是「备份」,取决于它
读文件的那一刻文件长什么样,而不是取决于谁真正改的。

r113 实测了两个后果,都能**确定性**复现(两个进程,时序固定):

**一、文件被留在变异态。** A 先启动(备份 = 干净内容,施加变异,准备还原),
B 在 A 还没还原时启动 —— B 的「备份」取到的**是 A 的变异内容**。A 先还原
(写回干净内容),B 后还原(把 A 的那份变异内容写回去)。最终文件**不是原始
内容**,而是一个从未有人打算保留的中间状态。从 git 看就是一处莫名其妙的
源码改动。

**二、更糟:造出假结论,而且事后看不出任何异常。** 同一场竞态里,A 的还原
发生在 B 的判据**跑完之前**。B 的判据于是对着**没被变异的源码**跑了一遍,
把一个**本来生效的**变异报成 `SURVIVED`。r113 实测:`判据看到的源码里还有
变异吗: False -> mutcheck 会报 SURVIVED(假结论)`,而事后文件干干净净。

第二条比第一条危险得多。第一条会让人去查 git;第二条**什么痕迹都不留**,
而一个变异测试工具报出来的 `survived` 恰恰是人最不会去怀疑的东西 ——
「假结论比没有结论更贵」(r103),「探针自己坏掉比探针漏报更贵」(r110)。

r112 就撞到过第一种形状:一轮结束时 `arl_lite/devloop/queue.py` 的模块
docstring 停在了某条变异的内容上,而那个进程自己报的退出码是 0。**那一
次没能归因**(当时并没有第二个 mutcheck 在跑),所以本模块不是来解释那件事
的,是来把这一类事情从「要靠归因」变成「压根发生不了」。

## 为什么是**全局一把锁**,而不是按目标文件加锁

变异测试是**一次跑一个**的手工动作,没有并发跑两个 mutcheck 的正当理由。
按目标文件加锁会允许「两个 mutcheck 改同一个文件」之外的并发 —— 那正是
本模块要防的那种交错的一半,而且会让锁集合本身变复杂。全局一把锁的代价是
「不能并行跑两个 mutcheck」,那个代价本来就不存在。

## 为什么是**锁**,而不是「更聪明的还原」

r113 试过另一条看起来更温和的路:**条件还原** —— 写回备份之前先看一眼
文件现在是不是还等于「我写下去的那个变异」,不是就别写(免得踩掉别人的)。

实测**它修不了这个竞态**。原因是备份本身可能是脏的:后启动的进程在
「前一个进程还没还原」的窗口里读文件,读到的是**已被变异的版本**,于是
它的备份里就带着那处变异。于是 A 还原时发现文件不是自己的变异(不敢动),
B 到点还原时把**带变异的那份备份**写了回去 —— 照样留下变异。

负控制里就是这么发现的:把还原改成条件式的之后,判据**照样绿**。

所以能修它的只有一件事:**让两个进程根本不要同时改**。锁做的是这个,
条件还原做不了 —— 后者是在错误发生之后补救,而错误发生的那一刻备份
就已经错了。

## 为什么不自己写 flock

`arl_lite/devloop/lock.py` 里已经有 `file_lock`:它处理了 fcntl / msvcrt
两套实现,并且在**两个都没有**的平台上退化成空操作同时显式告警
(`HAVE_LOCKING`)。再写一份就是 r28 / r109 修过的那个病(同一段逻辑两处
各写一份然后漂),而且退化策略会不一致。这里直接调它。

## 拿不到锁时怎么办:立刻退出并说明原因

`file_lock(timeout=0.0)` 等价于非阻塞 —— 拿不到就抛 `LockTimeout`。本模块
把它转成一条明确的错误并以**非零码**退出,而不是傻等。

理由:等锁这件事在本仓库没有正当场景,而「一个人以为变异测试卡住了」和
「变异测试悄悄把自己跑出的结论弄错」是同一类失败的两种口味,都要选响的
那一种。锁是 `flock`,进程一死内核就放,**不会留下需要清理的僵尸锁**。

## 用法

    if __name__ == "__main__":
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import mutkit
        raise SystemExit(mutkit.locked(main))

锁文件是 `devloop/mutcheck.lock` —— `.gitignore` 里的 `devloop/*.lock`
已经覆盖它,不用再往版本库里加东西。
"""
from __future__ import annotations

import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
REPO = _HERE.parent

# `python3 devloop/mutcheck_rNNN.py` 时 sys.path[0] 就是 devloop/,
# 但这些脚本也可能被当模块 import 进来(r113 自己就那么干过一次),
# 所以显式补上仓库根 —— 要 import arl_lite 得靠它。
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from arl_lite.devloop.lock import LockTimeout, file_lock  # noqa: E402

# 被 `file_lock` 保护的目标。锁文件由 `lock_path_for` 推成
# `devloop/mutcheck.lock` —— 这里刻意**不叫** `.mutcheck.lock`:
# `.gitignore` 里写的是 `devloop/*.lock`,用一个不带前导点的名字,
# 免得哪天有人以为前导点会被 gitignore 的 `*` 吃掉而多写一条规则。
#
# ## 为什么可以用环境变量改
#
# 判据(`tests/test_mutcheck_sweep_lock.py`)要验「锁空着时会放行」,而
# **变异测试自己正持着这把锁** —— 于是判据只要在 mutcheck 里跑,那次
# 断言就必然失败,判据 2 变成一条永远红的门禁,于是下一个人会把它拆掉。
# 环境变量让判据指向临时目录里的另一个目标,和「谁正在跑」彻底解耦。
SWEEP_TARGET = pathlib.Path(
    os.environ.get("ARL_MUTCHECK_LOCK") or (_HERE / "mutcheck"))


def locked(fn):
    """跑 `fn()`(零参、返回 int),全程持有一把跨进程互斥锁。

    Args:
        fn: 通常是脚本的 `main()`。

    Returns:
        `fn()` 的返回值原样透出,好让 `SystemExit(mutkit.locked(main))`
        拿到退出码。

    Raises:
        SystemExit: 锁被别的变异测试占着时,以非零码退出并说明原因。
    """
    try:
        with file_lock(SWEEP_TARGET, timeout=0.0,
                       owner=f"mutcheck pid={os.getpid()}"):
            return fn()
    except LockTimeout:
        print(
            "mutkit: 已经有另一个变异测试在跑,本次不跑。\n"
            f"  锁:{SWEEP_TARGET}.lock\n"
            "  变异测试会就地改仓库文件,两个同时跑会互相覆盖对方的备份,\n"
            "  结果既可能把仓库留在变异态,也可能把生效的变异误报成存活。\n"
            "  等前一个跑完再跑这一个。",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
