"""r85:`--log-level` 必须是真参数,不是 `getattr(..., logging.INFO)` 兜底的摆设

## 实测的退化路径(不是推测)

r85 动手前先量了一次,量到的是这条:**`--log-level` 没有 `choices`**,
消费点是一句兜底

    level=getattr(logging, args.log_level.upper(), logging.INFO)

`workspace create` 在一个全新 HOME 里会真的打日志(`Storage` 初始化时),
所以这条路径能看出级别到底生效没有:

    --log-level DEBUG    → [DEBUG] schema initialized at ... / [INFO] workspace ... created
    --log-level INFO     → [INFO] workspace 'default' created
    --log-level WARNING  → (无日志)
    --log-level bogus    → [INFO] workspace 'default' created   ← 与 INFO **逐字相同**
                           rc=0,一个字都没提示

也就是说:`--log-level bogus` 与 `--log-level INFO` **完全无法区分**。
用户打错字想开 DEBUG 排查问题,却静默拿到 INFO。**静默降级比降级本身更坏** ——
他不会知道自己错过了什么,只会在排查半天之后得出「这工具没打日志」。

`bogus` / `TRACE` / `10` / 空串实测都是 rc=0 + 与 INFO 相同输出。

## 探针本身栽过一次,值得记

第一版探针用的是 `monitor changes`,它在没工作区时只打印一句提示、
**根本不产生日志记录**,于是八种取值输出全同、rc 全 0。看着像完美复现,
实际上只证明了「没日志可看」。

换成真会打日志的路径(`workspace create` + 全新 HOME)才测出真正的差别。
跟 r84 那条是同一个病:**探针必须先证明自己看到了该看的东西**。

## 判据是双向的

  - 垃圾值必须**报错并列出合法值**(r71 的教训:错误必须给出路,
    只说「invalid」而不说合法的是什么,等于让用户去猜)
  - 合法值必须**真的生效** —— 只验 rc 会漏掉原 bug 的形状:
    「值被接受了但静默无效」是同一个病。DEBUG 必须真的打出 DEBUG 记录,
    INFO 必须不打。
  - 小写必须仍然合法(`type=str.upper` 排在 `choices` 前面才做得到;
    队列项专门警告过别把 `--log-level debug` 弄坏)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VALID = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# 会打出日志的路径:Storage 初始化时至少有 log.debug + log.info 两条。
# 换一条不打日志的命令,这个判据就恒红了 —— 那是探针的错,不是实现的错。
LOGGING_CMD = ["workspace", "create", "probe-ws"]


def _run(args, home: str):
    env = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", *args],
        capture_output=True, text=True, cwd=REPO, env=env, timeout=120,
    )


def _logs_for(level: str | None) -> tuple[int, str]:
    """`(退出码, 合并后的完整输出)`。每条命令都用全新 HOME,不截断输出。"""
    with tempfile.TemporaryDirectory() as home:
        args = list(LOGGING_CMD)
        if level is not None:
            args = ["--log-level", level, *args]
        r = _run(args, home)
        return r.returncode, r.stdout + r.stderr


GARBAGE = ["bogus", "TRACE", "10", "warning ", "not-a-level"]


def test_every_garbage_level_is_rejected():
    """垃圾值必须 rc=2,不能静默降级"""
    bad = {}
    for v in GARBAGE:
        rc, out = _logs_for(v)
        if rc != 2:
            bad[v] = f"rc={rc}"
    assert not bad, (
        f"这些 --log-level 值被静默接受了:{bad}\n"
        "用户打错字想开 DEBUG 排查问题,却拿到 INFO 且没有任何提示 —— "
        "静默降级比降级本身更坏")


def test_rejection_lists_every_valid_value():
    """报错必须**列出全部合法值**,不能只说 invalid

    r71 立过的规矩:错误必须给出路。只说「invalid choice」而不列合法值,
    等于让用户去猜或者去翻文档。
    """
    for v in GARBAGE:
        rc, out = _logs_for(v)
        assert rc == 2, f"{v!r} 应当被拒,实得 rc={rc}"
        missing = [lv for lv in VALID if lv not in out]
        assert not missing, (
            f"{v!r} 的报错里没提到这些合法值:{missing}\n"
            f"实际输出:\n{out}")


def test_debug_really_emits_debug_records():
    """合法值必须**真的生效** —— 只验 rc 会漏掉原 bug 的形状

    原 bug 不是「垃圾值被拒」,是「值被接受但静默无效」。所以这里验的是
    级别确实改变了日志,而不是只验命令跑通了。
    """
    rc, out = _logs_for("DEBUG")
    assert rc == 0, f"--log-level DEBUG 应当正常工作,实得 rc={rc}"
    assert "[DEBUG]" in out, (
        f"给了 DEBUG 却没有任何 DEBUG 记录 —— 级别没生效,"
        f"那正是原 bug 的形状。实际输出:\n{out}")


def test_info_does_not_emit_debug_records():
    """反向不变量:INFO 不打 DEBUG。两边一起验,「级别没生效」和「永远打 DEBUG」都跑不掉"""
    rc, out = _logs_for("INFO")
    assert rc == 0, f"--log-level INFO 应当正常工作,实得 rc={rc}"
    assert "[INFO]" in out, f"INFO 级别连 INFO 记录都没有:\n{out}"
    assert "[DEBUG]" not in out, (
        f"给了 INFO 却打出了 DEBUG 记录 —— 级别没生效:\n{out}")


def test_lowercase_still_works():
    """小写必须仍然合法 —— 队列项专门警告过别把 `--log-level debug` 弄坏

    `type=str.upper` 排在 `choices` 前面才做得到:argparse 先跑 type
    再查 choices,小写被规范化成大写之后才校验。
    """
    for v in ("debug", "info", "Warning", "eRrOr"):
        rc, out = _logs_for(v)
        assert rc == 0, f"--log-level {v} 应当合法,实得 rc={rc}\n{out}"


def test_no_level_at_all_still_uses_info():
    """不给 --log-level 时默认 INFO —— 别把默认行为改掉了"""
    rc, out = _logs_for(None)
    assert rc == 0, f"不传 --log-level 应当正常工作,实得 rc={rc}\n{out}"
    assert "[INFO]" in out, f"默认级别应当是 INFO:\n{out}"
    assert "[DEBUG]" not in out, f"默认级别不该打 DEBUG:\n{out}"


def test_the_argument_really_declares_its_choices():
    """结构检查:`choices` 必须真的挂在参数上

    用 AST 判,不靠文本子串 —— r80 那条教训。属性真写在 `add_argument` 的
    关键字参数里才算数,写在别处的同名 `choices` 喂不饱它。
    """
    import ast

    tree = ast.parse(open(os.path.join(REPO, "arl_lite", "cli.py"),
                           encoding="utf-8").read())
    found = None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        if not any(isinstance(a, ast.Constant) and a.value == "--log-level"
                   for a in node.args):
            continue
        kws = {k.arg: k.value for k in node.keywords}
        found = kws
    assert found is not None, "cli.py 里找不到 --log-level 的 add_argument"
    assert "choices" in found, (
        "--log-level 没有 choices —— 垃圾值只能靠 getattr 兜底静默降级")
    listed = [e.value for e in found["choices"].elts]
    assert sorted(listed) == sorted(VALID), (
        f"--log-level 的合法值是 {listed},与判据期望的 {VALID} 不一致 —— "
        "两边必须对得上,否则报错里列的值和真正接受的值会不一样")
    assert "type" in found, (
        "--log-level 没有 type=str.upper —— 小写会变成非法值,"
        "把 `--log-level debug` 弄坏")


def test_the_fallback_that_silently_degraded_is_gone():
    """那句 `getattr(logging, ..., logging.INFO)` 兜底必须已经拆掉

    光加 `choices` 而留着兜底,等于给一个不知道自己在干嘛的人留了把钥匙。

    ## 这条判据的第一版**是空转的**,而且是靠 M5 才发现的

    第一版用正则 `getattr\\(\\s*logging\\s*,[^)]*logging\\.INFO` 去扫。
    真实那行是:

        level=getattr(logging, args.log_level.upper(), logging.INFO),

    `[^)]*` 跨不过 `upper()` 里那个 `)`,所以**一条都匹配不到**,判据绿了 ——
    绿不是因为兜底拆了,是因为正则写错了。而 M5 变异的锚点
    `        level=logging.INFO,` 在实现里找不到,才让我回头看这一条。

    跟 r84 那条是同一个病的第 5 次:**看着像检查,其实不是**。
    所以改成 AST 判定:去 `logging.basicConfig(...)` 的 `level=` 实参上看,
    它是 `getattr` 调用就说明兜底还在。
    """
    import ast

    tree = ast.parse(open(os.path.join(REPO, "arl_lite", "cli.py"),
                           encoding="utf-8").read())
    levels = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "basicConfig"):
            continue
        for kw in node.keywords:
            if kw.arg == "level":
                levels.append(kw.value)
    assert levels, "cli.py 里找不到 logging.basicConfig(level=...)"
    bad = [ast.unparse(v) for v in levels
           if isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
           and v.func.id == "getattr"]
    assert not bad, (
        f"level= 还是个带兜底的 getattr:{bad}\n"
        "有了 choices 之后 argparse 已保证值合法,兜底只会掩盖「值没对上」")
