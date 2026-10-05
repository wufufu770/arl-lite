"""变异测试的互斥锁:证明它挡住了**真实存在**的竞态,而自己没坏掉

## 问题的形状

变异测试的做法是「读备份 → 施加变异 → 跑判据 → 写回备份」。单个进程内
这是对的;跨进程不安全 —— 谁拿到的是「备份」,取决于它读文件那一刻文件
长什么样,而不是取决于谁真正改的。

r113 实测了两个后果,都能确定性复现(两个进程,时序固定):

1. **文件被留在变异态。** A 备份到干净内容并施加变异;B 在 A 还没还原时
   启动,备份取到的**是 A 的变异内容**。A 先还原,B 后还原 —— 最终文件不是
   原始内容,而是一个没人打算保留的中间状态。从 git 看就是一处莫名改动。
2. **造出假结论,而且事后没有任何痕迹。** A 的还原发生在 B 的判据**跑完
   之前**,B 的判据于是对着**未被变异的源码**跑了一遍,把一个本来生效的
   变异报成 `SURVIVED`。实测输出:`判据看到的源码里还有变异吗: False ->
   mutcheck 会报 SURVIVED`,而事后文件干干净净。

第 2 条比第 1 条危险得多:第 1 条会让人去查 git,第 2 条什么都不留,而
变异测试报出来的 `survived` 恰恰是人最不会怀疑的东西。

## 三条判据各钉一个方向

1. **每个会写仓库文件的 mutcheck 脚本都必须取锁**(结构,AST)
2. **锁真的挡住了并发**(行为,实测:同进程 + 跨进程各一次)
3. **竞态是真的**(负控制:把锁拿掉,两个进程确实会交错)

## 第 3 条为什么必须有

只有判据 2 的话,「锁有效」这个结论可能是**空的**——锁根本没被用上,
判据 2 照样全绿。r110 记过同一种病:探针自己坏掉比探据漏报更贵。这里
判据 3 用**同一个时序**跑一遍无锁版本,把「真的会交错」当成被断言的事实,
判据 2 的结论才有对照物。

判据 3 只碰临时目录里的文件,不碰仓库。
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parents[1]
DEV = REPO / "devloop"
MUTCHECKS = sorted(DEV.glob("mutcheck_*.py"))

# 无锁竞态的复现脚本。两个进程跑它,按参数错开时序,文件会被留在变异态。
# `MINE` 是本进程写下去的内容,`restore` 是无条件写回自己的备份 ——
# **没有任何校验**,这正是要修的那一处。
RACER = """
import pathlib, sys, time
p = pathlib.Path(sys.argv[1]); delay = float(sys.argv[2]); marker = sys.argv[3]
backup = p.read_bytes()
p.write_bytes(backup + marker.encode())
time.sleep(delay)
p.write_bytes(backup)
"""


def _writes_repo_files(tree: ast.AST) -> bool:
    """这个脚本会不会写仓库里的文件"""
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("write_text", "write_bytes")):
            return True
    return False


def _takes_sweep_lock(tree: ast.AST) -> bool:
    """有没有调 `mutkit.locked(...)`"""
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "locked"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "mutkit"):
            return True
    return False


def _classify() -> tuple[list[str], list[str]]:
    """返回 (会写文件且取了锁, 会写文件但没取锁)"""
    assert MUTCHECKS, (
        "devloop/ 下一个 mutcheck_*.py 都没有 —— 判据遍历器一次都没跑,"
        "下面所有检查都是恒真的。先确认 glob 的路径对不对"
    )
    locked: list[str] = []
    bare: list[str] = []
    for f in MUTCHECKS:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        if not _writes_repo_files(tree):
            continue
        (locked if _takes_sweep_lock(tree) else bare).append(f.name)
    return locked, bare


def test_every_mutcheck_that_writes_files_takes_the_sweep_lock():
    """每个**会写仓库文件**的变异测试脚本都必须持锁

    只查「写文件的」那批:`mutcheck_r41` 到 `mutcheck_r64` 那 24 个是纯
    分析脚本,一个 `write_*` 都没有,给它们套锁是给不存在的问题加仪式。

    ## 为什么不按「是不是 mutcheck」一刀切

    一刀切的话,判据就变成了「每个文件里都得有 `mutkit.locked` 这串
    字」—— 于是 (a) 给 24 个纯分析脚本白白套一层,(b) 下一个人新写一个
    纯分析脚本会被判据红,只能去加一句没用的调用。判据一旦开始逼人写
    废话,它就没人看了。

    ## 前置条件也断言掉

    `locked` 和 `bare` 的数量本身就是结论的一部分。如果哪天判据因为
    脚本搬家而遍历到 0 个文件,`assert not bare` 会**绿**——那正是
    「一条永远绿的判据等于没有判据」。所以先钉住「至少有 30 个脚本会写
    文件」这个实测值(现在是 46)。
    """
    locked, bare = _classify()
    total = len(locked) + len(bare)
    # 这里的 30 是**实测 46 的下限**,不是精确值 —— 写死精确值会变成
    # 「写死总数 = 自造一条永远红的门禁」(r110 刚把一个漂移的总数去掉)。
    #
    # 它的真实边界是实测出来的,不是猜的:r113 试过把属性名改坏**一个**
    # (`write_bytez`),`total` 只从 46 掉到 45,这条照样绿。所以它挡的是
    # **范围崩塌**,不是「掉一个脚本」。一个脚本从「会写文件」变成
    # 「不写文件」(比如有人删了 finally 里的还原)会让它退出检查范围 ——
    # 而那恰恰是更危险的改动。这个缺口已知,没在本轮补。
    assert total >= 30, (
        f"只认出 {total} 个会写文件的脚本(实测 46)。"
        "要么判据的遍历范围变了,要么脚本被搬走了 —— "
        "不管哪种,这条判据现在验不到东西,别信它的绿"
    )
    assert locked, (
        "一个会写文件的 mutcheck 都没取锁,这条判据此刻全红。"
        "先跑 devloop/mutkit.py 的说明:竞态能留下假结论(见本文件 docstring)"
    )
    assert not bare, (
        "这些变异测试会就地改仓库文件,却没取锁:\n  "
        + "\n  ".join(sorted(bare))
        + "\n  入口应该写成 `raise SystemExit(mutkit.locked(main))`,"
        "并让脚本目录在 sys.path 上。"
    )


def test_the_lock_actually_refuses_a_second_holder(monkeypatch, tmp_path):
    """锁真的挡人 —— 不是装饰

    分两次验,因为这是两种不同的失败:

    - **同进程**再拿一次:`flock` 锁的是**打开的文件描述**而不是进程,
      所以同进程里另开一个 fd 再 `LOCK_EX` 是会冲突的。这条快、确定,
      但它验不到跨进程。
    - **跨进程**:另起一个进程持着锁,本进程必须被拒。这才是真正要挡的
      那种并发 —— 判据 1 说的就是它。

    顺带验**放锁之后能再拿到**:一个只会拒不会放的锁会把仓库永久锁死,
    那比不加锁更坏。

    ## 为什么把锁指向临时目录

    「锁空着的时候要放行」这个断言,**在变异测试里跑时必然失败** ——
    因为变异测试自己正持着这把锁(`devloop/mutcheck_r113.py` 就是
    `mutkit.locked(_run_all)`)。首版直接用 `mutkit.SWEEP_TARGET`,
    结果判据 2 在任何 mutcheck 里都变成红的 —— **一条永远红的门禁等于
    没有门禁**,下一个人会把它拆掉,于是真正该验的东西反而没人验了。

    所以走 `ARL_MUTCHECK_LOCK` 指向临时目录里的另一个目标,把「锁在谁手里」
    和「本进程是谁」彻底解耦。这不是为了绕过失败,是因为**这个断言本来
    就不该依赖全局状态**。
    """
    import importlib

    monkeypatch.setenv("ARL_MUTCHECK_LOCK", str(tmp_path / "probe"))
    sys.path.insert(0, str(DEV))
    import mutkit
    mutkit = importlib.reload(mutkit)
    from arl_lite.devloop.lock import LockTimeout, file_lock

    # ── 同进程:第二次必须被拒 ──
    #
    # 写成「记一个布尔再断言」而不是 `except LockTimeout: pass` + 外层
    # 断言,是因为后者会被 `test_swallowed_exceptions_are_accounted_for.py`
    # 逮住(单语句 `pass` 的 handler 就是它认的那种吞异常)。**与其把它
    # 登记成「已知且可接受」,不如把代码改成不留吞异常的样子** ——
    # 登记表每多一条,下一个读它的人就多一件要判断的事。
    with file_lock(mutkit.SWEEP_TARGET, timeout=0.0, owner="判据自己"):
        second_refused = False
        try:
            with file_lock(mutkit.SWEEP_TARGET, timeout=0.0, owner="第二个"):
                second_refused = False
        except LockTimeout:
            second_refused = True
        assert second_refused, (
            "同进程里第二个持有者拿到了锁 —— 这把锁不是排他的,"
            "判据 1 那句「必须持锁」就毫无意义"
        )

    # ── mutkit.locked 的对外行为 ──
    # 锁空着的时候:放行,且**原样透出返回值**(退出码靠它)
    assert mutkit.locked(lambda: 7) == 7, (
        "mutkit.locked 没把 fn() 的返回值透出来 —— "
        "那样 `SystemExit(mutkit.locked(main))` 退出码会变成「打印一个对象」"
    )

    # 锁被占着的时候:必须以非零码退出并说明原因
    import contextlib, io
    with file_lock(mutkit.SWEEP_TARGET, timeout=0.0, owner="判据占位"):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                mutkit.locked(lambda: pytest.fail("不该跑到这里"))
            except SystemExit as e:
                code = e.code
            else:
                code = 0
    assert code == 2, (
        f"锁被占着时 mutkit.locked 的退出码是 {code!r},预期 2。"
        "非 2 会让「变异测试被跳过」和「变异测试跑完了」在 CI 里分不开"
    )
    assert "已经有另一个变异测试在跑" in buf.getvalue(), (
        f"被拒时没有说清原因,stderr 是:{buf.getvalue()[:200]!r}"
    )

    # ── 跨进程:另起一个持锁进程 ──
    holder_src = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(DEV)!r})\n"
        "import mutkit\n"
        "from arl_lite.devloop.lock import file_lock\n"
        "with file_lock(mutkit.SWEEP_TARGET, timeout=0.0, owner='holder'):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(30)\n"
    )
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "holder.py"
        script.write_text(holder_src, encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-B", str(script)],
            stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout is not None
            line = proc.stdout.readline()
            assert line.strip() == "held", (
                f"持锁进程没拿到锁,没法验跨进程互斥(它说了 {line!r})")

            # 锁在别的进程手里 —— mutkit.locked 必须拒
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                try:
                    mutkit.locked(lambda: pytest.fail("不该跑到这里"))
                except SystemExit as e:
                    code = e.code
                else:
                    code = 0
            assert code == 2, (
                f"别的进程持着锁,本进程却拿到了(退出码 {code!r})。"
                "判据 1 说脚本会持锁,可这把锁根本不拦跨进程"
            )
        finally:
            proc.kill()
            proc.wait(timeout=10)


def test_without_the_lock_the_race_is_real():
    """负控制:把锁拿掉,两个进程**确实**会交错

    没有这一条,上面那条判据的「锁有效」可能是句空话 —— 锁根本没被用上,
    它照样全绿。而这一条是 r113 那个结论的**唯一**对照物:没有它,
    「加锁」就只是照着直觉做的一件事。

    ## 时序是被刻意排成会出事的那个

    A 先启动:备份到**干净内容**,施加变异,0.15 秒后还原(干净)。
    B 在 0.05 秒后启动 —— A 还没还原,于是 B 的「备份」取到的**是 A 的
    变异内容**;B 3 秒后还原,把那一份脏内容写了回去。

    谁最后还原谁说了算,所以**必须**是「备份干净的那个先还原」。写成
    「A 后启动、B 后还原」看起来对称,实测反而是干净的 —— r113 第一次
    就排错了这个序,得到 `最终内容: V0`,差点据此下结论说竞态不存在。

    只碰临时目录,不改仓库任何一个文件。
    """
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "racer.py"
        script.write_text(RACER, encoding="utf-8")
        target = Path(td) / "f.txt"
        target.write_text("V0\n", encoding="utf-8")

        a = subprocess.Popen(
            [sys.executable, "-B", str(script), str(target), "0.15", "<<A>>"])
        import time
        time.sleep(0.05)
        b = subprocess.Popen(
            [sys.executable, "-B", str(script), str(target), "3", "<<B>>"])
        a.wait(timeout=30)
        b.wait(timeout=30)

        left = target.read_bytes()
        assert left != b"V0\n", (
            "两个无锁进程跑完,文件居然是干净的 —— 那说明这个时序排不出竞态,"
            "上面那条判据验的「锁挡住了什么」就没有对照物。"
            "先别急着改判据,把时序调回能交错的那个再跑"
        )
        assert left == b"V0\n<<A>>", (
            f"留下的内容是 {left!r},预期 b'V0\\n<<A>>' —— "
            "即 A 的变异内容被 B 的还原写了回去。"
            "留成别的形状说明我对竞态的理解和实际不符,这条判据的说明要跟着改"
        )
