"""r36:`backlog.md` 里的 verify 必须是命令 —— 散文型验收让完成度无法证伪

## 为什么这条待办存在

`devloop/backlog.md` 是人写的长期燃料。r34 实测过它的判据能力:

    散文 verify 'docs/ 里有一节说明 confidence 决定…'   -> False
    命令 verify 'python3 -m pytest tests/…py'          -> True

散文 verify 恒为 False,于是人写待办的"做没做"**机器判不了**。
r22 那条假指控之所以能挂在一条其实做完了的条目上过 12 轮,判据缺位
是原因之一 —— 记着"完成"的字段是空的,而"验收"是散文,两边都判不了。

r35 加了 `Queue.verify_result()` 三态判定,并让收尾闸门只在
`fail`(确认是命令、确认跑得起来、确认没过)时拦截。`unknown`(散文)
不拦 —— 拦了就等于所有人写的待办都永久卡死,那道闸门活不过三轮。

**但那只是兜底。** 更好的做法是让散文根本进不了 backlog:人写待办时
就把"怎么做"写进 `detail`,把"做完没有"写成一条命令。

## 这一轮的处置

`backlog.md` 原有 3 条散文 verify,全部改写成命令,散文原意**整段搬进
detail**,一条信息都没丢:

| 条目 | 原 verify(散文) | 改成 |
|---|---|---|
| `confidence-status` | 「HTML 里 discard 的不出现在主表(可 grep 断言)…」 | `python3 -m pytest tests/test_confidence_reporting.py -q` |
| `confidence` | 「造 60 条关联,置信度最高的那条排在最后…」 | 同上 |
| `confidence-risk` | 「docs/ 里有一节说明…两者正交」 | `test -f docs/… && grep -q 正交 docs/…` |

## 为什么最后一条测试是自检

本文件前面几条断言的是"`verify` 不是散文"。如果哪天有人把
`verify_result` 改坏成"什么都返回 pass",那几条会**照样全绿** ——
它们检查的东西已经不存在了,自然检查不到。

所以最后一条拿已知样本做对照:散文必须被认成 `unknown`,命令必须被
真的执行。判据本身坏掉时,这条会先红。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arl_lite.devloop.queue import Queue


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "pyproject.toml").is_file() and (cand / "arl_lite").is_dir():
            return cand
    raise RuntimeError("找不到项目根")


REPO = _repo_root()
BACKLOG = REPO / "devloop" / "backlog.md"


def _backlog_lines() -> list[tuple[str, str]]:
    """用引擎**自己的**解析器读 backlog,返回 [(标题, verify)]。

    不自己写正则:引擎用的正则是 `_BACKLOG_LINE`,两条路径一旦分家,
    这个测试就在检查一份引擎根本不读的文件。
    """
    text = BACKLOG.read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        m = Queue._BACKLOG_LINE.match(line)
        if m:
            out.append((m.group(3).strip(), m.group(5).strip()))
    return out


# ── backlog 里的每一条 ──

def test_backlog_has_items_to_check():
    """先确认这个文件真的读到了东西

    没有这条,下面所有"没有散文 verify"的断言都可以靠"读到 0 行"
    恒真通过 —— 那正是 r22 记过的恒真陷阱。
    """
    lines = _backlog_lines()
    assert len(lines) >= 10, (
        f"只读到 {len(lines)} 条 backlog,解析器多半和引擎脱节了"
    )


def _is_commandable(verify: str) -> tuple[bool, str]:
    """verify 是不是"机器能跑的检查命令"。**只看形状,不执行。**

    ## 为什么不执行

    第一版这里调 `Queue.verify_result()`,而它是**真的跑一遍**的。
    后果实测到了:`backlog.md` 里那条待办
    「人写待办的 verify 必须可被机器核验」,它的 verify 恰恰是
    `python3 -m pytest tests/test_backlog_verify_is_commandable.py`
    —— 也就是**本文件自己**。于是:跑本文件 → 跑 verify → 再跑本文件
    → 再跑 verify……本文件耗时 43 秒,超过 `verify_result` 的 30 秒
    超时,于是它自己的 verify 被判成 `unknown`,本文件立刻红。

    一个检查"别人能不能跑"的测试,把自己也拿去跑了 —— 自指。

    判据改成两件事的合取,都不执行:
      1. 第一个词在 `VERIFY_RUNNERS` 白名单里(r35 定的)
      2. `bash -n` 判它是合法 shell 语法

    "真的能跑通"是**门禁**的事(收尾闸门会跑),这里不重复。
    """
    import subprocess
    if not verify.strip():
        return False, "verify 是空的"
    first = verify.strip().split(None, 1)[0]
    if first not in Queue.VERIFY_RUNNERS:
        return False, f"首词 {first!r} 不在白名单 {list(Queue.VERIFY_RUNNERS)}"
    r = subprocess.run(["bash", "-n", "-c", verify],
                       cwd=REPO, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False, f"shell 语法错: {r.stderr.strip()[:80]}"
    return True, ""


def test_every_backlog_verify_is_a_runnable_command():
    """backlog 里不允许出现散文 verify

    verify 的职责是回答"这条做完了吗"。散文回答的是"该怎么做"——
    两件事。散文放在 verify 里,机器永远判不了,于是这条待办的完成度
    只能靠人的记忆,而人的记忆已经在 r22 上误判过一次。
    """
    bad = []
    for title, verify in _backlog_lines():
        ok, why = _is_commandable(verify)
        if not ok:
            bad.append((title, verify, why))
    assert not bad, (
        "这些条目的 verify 不是可执行的检查命令:\n  "
        + "\n  ".join(f"{t} -> {v[:60]}  ({w})" for t, v, w in bad)
        + "\n把散文搬进 detail 段,verify 段写一条能跑的命令"
    )


def test_fallback_verifies_are_commands_too():
    """`_FALLBACK` 三条长期演进项的 verify 同样必须是命令

    它们决定"要不要重提一条",判据和 backlog 同源。若这里写散文,
    播种会**永远**认为它们到期(散文恒 False),每轮重提一遍。
    """
    assert Queue._FALLBACK, "_FALLBACK 空了,这条测试没有意义"
    bad = [(s["id"], s["verify"], why) for s in Queue._FALLBACK
           for ok, why in [_is_commandable(s["verify"])] if not ok]
    assert not bad, (
        "长期演进项的 verify 不是命令,会被永远当成已到期:" + str(bad)
    )


# ── 自检:判据本身坏掉时,这里要先红 ──

@pytest.mark.parametrize("verify,expect", [
    # 真实出现在 backlog 里的三种散文形态(r35 实测的退出码 126/127/2)
    ("docs/ 里有一节说明 confidence 与 risk 两者正交", Queue.VERIFY_UNKNOWN),
    ("`arl-lite report --html` 产出的 HTML 里,discard 不出现在主表", Queue.VERIFY_UNKNOWN),
    ("造 60 条关联,置信度最高的那条排在最后,断言它出现在报告里", Queue.VERIFY_UNKNOWN),
    # 真实出现在 backlog 里的命令形态
    ("test -f pyproject.toml", Queue.VERIFY_PASS),
    ("test -f 绝对不存在的文件", Queue.VERIFY_FAIL),
    ("", Queue.VERIFY_UNKNOWN),
])
def test_the_detector_still_distinguishes_prose_from_commands(verify, expect):
    """判据自检:上面几条全绿,前提是 `verify_result` 还分得清

    没有这条,把 `verify_result` 改成"永远 pass"会让本文件前四条
    一起变绿 —— 它们检查的"不是散文"将不再有任何意义。
    """
    got = Queue.verify_result(verify)
    assert got == expect, (
        f"{verify[:50]!r} 判成了 {got!r},期望 {expect!r}。"
        f"判据坏了,本文件其余测试的绿灯都是假的"
    )


@pytest.mark.parametrize("verify,ok,why_substr", [
    # 正常命令
    ("python3 -m pytest tests/test_x.py -q", True, ""),
    ("test -f pyproject.toml", True, ""),
    # 首词不在白名单(真实出现过的三种散文形态)
    ("docs/ 里有一节说明 confidence 与 risk 两者正交", False, "不在白名单"),
    ("`arl-lite report --html` 产出的 HTML 里,discard 不出现", False, "不在白名单"),
    ("造 60 条关联,置信度最高的那条排在最后", False, "不在白名单"),
    # 首词在白名单、但语法坏 —— 这一条是被变异测试逼出来的:
    # 去掉 `bash -n` 之后本文件照样全绿,因为眼下没有 verify 语法坏。
    # 而它是真会发生的:`python3 -m pytest tests/foo(1).py` 能过白名单,
    # 到运行时才炸。
    ("python3 -m pytest tests/foo(1).py", False, "语法错"),
    # 空
    ("", False, "空的"),
    ("   ", False, "空的"),
])
def test_is_commandable_helper_itself(verify, ok, why_substr):
    """`_is_commandable` 这个判据本身要被测,不然它就是没被验证的分支

    本文件其余测试都用它当前提。前提坏掉时它们不会变红 ——
    前提失效表现为"检查的东西不存在了",而检查不到不存在的东西。
    """
    got_ok, why = _is_commandable(verify)
    assert got_ok is ok, f"{verify!r} 判成了 {got_ok}({why}),期望 {ok}"
    if why_substr:
        assert why_substr in why, f"理由是 {why!r},不含 {why_substr!r}"


def test_a_completed_backlog_line_is_not_seeded_again(tmp_path):
    """detail 段开头打了 ✅ 的行,**不得**被重新播成新活

    r36 查实的第 5 处「看着有用其实没用」:`backlog.md` 的表头写着
    「播种按 id 跳过已完成的条目,所以下面这些"已完成"的行不会再被
    捡回来」,而实际跳过的判据只有**队列里**的 `done` 记录。✅ 标记
    纯属摆设 —— 实测同样两行,一行打 ✅ 一行不打,都照样被播出来。

    平时看不出来,是因为所有条目都恰好在队列里留了 done 记录。
    但有一类走不到队列:r35 那两条是 r34 写进 backlog.md、r35 直接
    实现并提交的(提交 d59dcb1),**从未被播种进队列**。人这边的 ✅
    记着"做完了",引擎这边完全不知道,会把它们当两件新活重排。

    而"已完成的工作被无限重排"正是第 12 轮清过的那场灾难。
    """
    d = tmp_path
    (d / "backlog.md").write_text(
        "# backlog\n"
        "- [P1] change : 已做完的活 | ✅ r99 完成 · 提交 abc123 | test -f pyproject.toml\n"
        "- [P1] change : 还没做的活 | 细节 | test -f pyproject.toml\n",
        encoding="utf-8")

    q = Queue(d / "queue.json")
    added = q.seed_if_empty()

    titles = [i.title for i in Queue(d / "queue.json").load()]
    assert titles == ["还没做的活"], (
        f"打 ✅ 的行被重新播成了新活:{titles}"
    )
    assert added == 1, f"只应有 1 条真活被播出来,实际 {added}"


def test_an_unmarked_backlog_line_is_still_seeded(tmp_path):
    """没打 ✅ 的行必须照常播 —— 这是上面那条的反证

    如果实现变成"一律不播",上面那条会绿而队列永远空着,
    不变式 #4(#2 轮起就有测试守着)也就跟着破了。
    """
    d = tmp_path
    (d / "backlog.md").write_text(
        "# backlog\n- [P1] change : 活甲 | 细节 | test -f pyproject.toml\n",
        encoding="utf-8")

    assert Queue(d / "queue.json").seed_if_empty() == 1


def test_detector_whitelist_is_not_widened_silently():
    """白名单只能显式增长,且必须写在源码里

    r35 定的规矩:散文和命令的边界没有可靠正则能划,所以用白名单
    (`python3` / `test` / `true`)。往里加东西是可以的,但必须是
    有意识的一行改动 —— 这条测试让"悄悄放宽"变成一件看得见的事。
    """
    src = ast.parse(
        (REPO / "arl_lite" / "devloop" / "queue.py").read_text(encoding="utf-8")
    )
    runners: list[str] = []
    for node in ast.walk(src):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "VERIFY_RUNNERS" for t in node.targets)):
            runners = [e.value for e in node.value.elts]
    assert runners == ["python3", "test", "true"], (
        f"VERIFY_RUNNERS 被改成了 {runners} —— "
        f"放宽白名单会让散文重新获得'看起来像命令'的资格"
    )
