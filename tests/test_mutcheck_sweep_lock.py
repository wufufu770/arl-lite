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

# 无锁竞态的复现脚本。**用哨兵文件显式协调,不用 sleep。**
#
# `MINE` 是本进程写下去的内容,`restore` 是无条件写回自己的备份 ——
# **没有任何校验**,这正是要修的那一处。
#
# ## 为什么这里一个 sleep 都没有
#
# 首版用 `time.sleep(0.05)` 错开两个进程的启动。那是**靠时序碰运气**:
# 在机器忙的时候(比如刚跑完一轮变异测试、后台还有子进程),B 可能在 A
# 还原**之后**才启动,竞态就排不出来,判据于是红 —— 而红的和被测代码
# 毫无关系。**一条会因为无关原因变红的判据,比没有判据更坏**:它会让人
# 以为是锁坏了,然后去「修」一个没坏的东西(r101:判不准的检测器比没有更危险)。
#
# 改成:每一步都等一个**可观察的条件**,而不是等一段时间。于是顺序是
# 被证明出来的,不是被祈祷出来的。
RACER = """
import pathlib, sys, time

target = pathlib.Path(sys.argv[1])
role = sys.argv[2]
marker = sys.argv[3]
sentinel = pathlib.Path(sys.argv[4])


def seen(tag):
    sentinel.mkdir(parents=True, exist_ok=True)
    (sentinel / tag).write_text("x")


def until(tag, limit=30.0):
    end = time.monotonic() + limit
    while not (sentinel / tag).exists():
        if time.monotonic() > end:
            raise SystemExit(f"等 {tag} 等超时了(limit={limit}s)")
        time.sleep(0.01)


# 每一处写回都是**无条件**的:写什么就是什么,没有「文件现在是不是还等于
# 我写下去的」这种校验。这正是要修的那一处。
if role == "first":
    backup = target.read_bytes()
    target.write_bytes(backup + marker.encode())
    seen("first-mutated")
    until("second-mutated")        # 等后一个也变异完
    target.write_bytes(backup)     # 无条件还原:干净内容
    seen("first-restored")
else:
    until("first-mutated")
    backup = target.read_bytes()   # 备份到的是**脏内容**,而它并不知道
    target.write_bytes(backup + marker.encode())
    seen("second-mutated")
    until("first-restored")        # 等先一个把干净内容写回去
    target.write_bytes(backup)     # 无条件还原:把那份脏备份写回去
    seen("second-restored")
"""


def _locked_name(tree: ast.AST) -> str | None:
    """入口那个 `mutkit.locked(<名字>)` 里的 `<名字>`;没有就返回 None

    ## 为什么用 AST 而不是 `if "mutkit.locked(" in src`

    首版是源码子串匹配,当场出了两个错:

    - `mutcheck_r113.py` 的 **docstring 里写着 `mutkit.locked(main)`**,
      于是它被算成「锁了 main」,而它实际锁的是 `_run_all`。
    - 统计口径因此虚高。

    「文档里提到一个调用」和「代码里真的调了它」是两件事,而这条判据的全部
    意义就在于分这两件事。源码里也可能**恰好**有一行代码长得像
    `mutkit.locked(...)` 但在注释里 —— 子串分不出来,AST 分得出来。
    """
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "locked"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "mutkit"
                and len(n.args) == 1
                and isinstance(n.args[0], ast.Name)):
            return n.args[0].id
    return None


def _classify() -> tuple[list[tuple[str, str]], list[str]]:
    """返回 (取锁的 [(文件名, 被锁的函数名)], 没取锁的 [文件名])"""
    assert MUTCHECKS, (
        "devloop/ 下一个 mutcheck_*.py 都没有 —— 判据遍历器一次都没跑,"
        "下面所有检查都是恒真的。先确认 glob 的路径对不对"
    )
    locked: list[tuple[str, str]] = []
    bare: list[str] = []
    for f in MUTCHECKS:
        name = _locked_name(ast.parse(f.read_text(encoding="utf-8")))
        (locked.append((f.name, name)) if name else bare.append(f.name))
    return locked, bare


def test_every_mutcheck_script_takes_the_sweep_lock():
    """**每一个**变异测试脚本都必须持锁 —— 没有豁免

    ## r114 更正:上一版有个「会写文件才要求持锁」的豁免,而它是错的

    上一版(r113)判据只查「会写仓库文件的脚本」,理由是实测「70 个脚本里
    46 个会写文件,r41–r64 那 24 个是纯分析,一个 `write_*` 都没有」。

    **那个实测是错的,而且错得很有说服力**:判定函数只认
    `write_text` 和 `write_bytes` 两个方法名。而 r41–r64 用的是

        open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
        shutil.copy(bak, path)

    —— 一样在改仓库源码,只是走的不是那两个方法。逐个查了它们的变异目标:
    `arl_lite/core/monitor.py`、`arl_lite/core/watcher.py`、`arl_lite/cli.py`、
    `arl_lite/ai/commands.py`、`tests/test_cli_advice_commandable.py`……

    后果:那 24 个脚本**确实会就地改仓库文件,却因为分类器看不见而被
    豁免在锁外面** —— 正是本文件要防的那件事。r114 把它们全补上了锁,
    现在 71 个脚本一个不落。

    ## 为什么豁免整个删掉,而不是把检测面修宽

    修宽之后实测:**71 个脚本全部会写,一个只读的都没有**。豁免成了死代码 ——
    而死代码在这类判据里比没有更坏,因为它会让人以为「还有一批脚本是不
    需要管的」,下一个人会拿它当先例。

    ## 顺带记一个检测器陷阱

    把 `Path.replace` 算进「会写」之后,**71 个脚本全部命中** —— 因为静态
    分不清 `s.replace(old, new, 1)`(纯字符串替换)和 `Path.replace(...)`
    (换文件)。分不清的检测器会匹配一切,**判不准的检测器比没有更危险**
    (r101)。所以判据里只保留**模块限定、无歧义**的途径。

    ## 前置条件也断言掉

    如果哪天 glob 路径变了导致遍历到 0 个文件,`assert not bare` 会**绿**。
    所以先钉住「至少有 60 个脚本」(实测 71)。
    """
    locked, bare = _classify()
    assert len(locked) >= 60, (
        f"只认出 {len(locked)} 个取锁的脚本(实测 71)。"
        "要么判据的遍历范围变了,要么脚本被搬走了 —— "
        "不管哪种,这条判据现在验不到东西,别信它的绿"
    )
    assert not bare, (
        "这些变异测试没有持锁:\n  "
        + "\n  ".join(sorted(bare))
        + "\n  入口应该写成 `raise SystemExit(mutkit.locked(main))`,"
        "并让脚本目录在 sys.path 上。\n"
        "  (r113 漏掉的 24 个:分类器只认 write_text/write_bytes,"
        "看不见 `open(path, \"w\")` 和 `shutil.copy` 那种写法。)"
    )


def test_the_locked_entry_point_exists_and_takes_no_arguments():
    """被锁的那个函数必须**真实存在**,而且**零必填参数**

    `mutkit.locked(fn)` 的全部契约就是 `fn()` 这一句调用。于是它对 `fn`
    有两个要求:是模块级可调用的、零必填位置参数。

    ## 这条是实测出来的,不是想出来的

    r114 批量统一入口形状时,改造脚本把 47 个脚本的锁**统一成了
    `mutkit.locked(_run_all)`** —— 而那些脚本里根本没有 `_run_all` 这个
    函数(r113 自己才有)。改造脚本的复核只查了「能 parse」「写调用数没变」
    「`main` 还在」,**没查「锁的那个名字是不是就是那个 `main`」**,于是
    47 个脚本一起改成了会在运行时 `NameError` 的样子,当场没发现。

    所以这里不只是查签名,而是**真的把模块 import 进来**:
    名字不存在、模块 import 就炸、函数需要参数,三种都会红。

    只 import 不调用 —— 模块级代码只做定义,不碰仓库。
    """
    import importlib.util
    import inspect

    locked, _ = _classify()
    bad: list[str] = []
    for fname, fn_name in locked:
        f = DEV / fname
        try:
            spec = importlib.util.spec_from_file_location(f"probe_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
        except Exception as e:
            bad.append(f"{fname}: import 就炸了 —— {type(e).__name__}: {e}")
            continue
        fn = getattr(mod, fn_name, None)
        if fn is None:
            bad.append(f"{fname}: 锁了 `{fn_name}`,但模块里没有这个函数")
            continue
        required = [p.name for p in inspect.signature(fn).parameters.values()
                    if p.default is inspect.Parameter.empty
                    and p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                   inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        if required:
            bad.append(f"{fname}: `{fn_name}` 需要必填位置参数 {required},"
                       "而 `mutkit.locked` 是按 `fn()` 调的")
    assert not bad, (
        "这些脚本的入口对不上 `mutkit.locked(fn)` 的契约(`fn()`):\n  "
        + "\n  ".join(bad)
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

    ## 时序是被**证明**出来的,不是被祈祷出来的

    首版这里用 `time.sleep(0.05)` 错开两个进程。那是碰运气:机器一忙,
    B 可能在 A 还原之后才启动,竞态排不出来,这条判据就红 —— 而红的和
    被测代码无关。**这种判据比没有判据更坏**(r101)。

    现在 A 先启动(备份到干净内容、施加变异),B **等看到 A 的变异**才启动,
    于是 B 的备份**必然**是脏的;A 等 B 还原完再把干净内容写回去,B 最后
    把那份脏备份写回去 —— 最终文件不是原始内容。

    谁最后还原谁说了算,所以**必须**是「备份干净的那个先还原」。写成
    「A 后启动、B 后还原」看起来对称,实测反而是干净的 —— r113 第一次
    就排错了这个序,得到 `最终内容: V0`,差点据此下结论说竞态不存在。

    只碰临时目录,不改仓库任何一个文件。
    """
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "racer.py"
        script.write_text(RACER, encoding="utf-8")
        root = Path(td)
        target = root / "f.txt"
        target.write_text("V0\n", encoding="utf-8")
        sentinel = root / "sentinel"

        def spawn(role, marker):
            return subprocess.Popen(
                [sys.executable, "-B", str(script), str(target), role,
                 marker, str(sentinel)])

        # 先起的那个:备份拿到的是**干净内容**,并且等后一个还原完才写回
        a = spawn("first", "<<A>>")
        # 后起的那个:等看到 A 的变异才动手 —— 它的备份**必然**是脏的
        b = spawn("second", "<<B>>")
        a.wait(timeout=60)
        b.wait(timeout=60)

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
