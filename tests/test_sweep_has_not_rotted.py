"""r116:`backlog_verify_sweep` 不能烂掉 —— 但这道判据**只**盯它还活着

## 先说清楚这道判据不管什么

它**不**验「每条已完成的活的 verify 还跑得通」。那件事要 104 秒(实测),
而 `test_baseline` 的 600 秒是不可提升的红线(实测 `promotable` 为假,
`devloop accept` 直接拒绝),390–535 秒的实测区间加上 104 秒最坏能到 639 秒 ——
放进去就是一条**偶发红**的门禁,而偶发红的门禁比没有更坏。

并行化能把 104 秒压到 30–40 秒,但**实测它会说谎**:4 线程 1 条误报、8 线程
2 条误报(CPU 争抢让部分 verify 撞上 30 秒上限)。6 线程那次一致是运气。
**一个会说谎的检测器比一个慢的检测器贵得多**(r101)。

所以定成:verify 跑不跑得通**仍然手动跑**(协议第 12.2 节写了触发条件:
`test_baseline` 超过 480 秒那一轮必须手动跑一遍),而这道判据只挡
**更可能发生的那件事** —— 脚本被改坏、路径失效、解析逻辑坏掉,然后它安静地
什么都不报。

r107 那条轮次索引就是在「不在门禁里的检查」上烂掉的:sweep 从 r111 立到
r116 都没人跑过它一次,也没人知道它还能不能用。

## 为什么不用**子进程**去调那个脚本

首版想的是 `subprocess.run([sys.executable, "devloop/backlog_verify_sweep.py",
"--help"]` —— 那是真正验「脚本能起来」。但那样连 `--help` 都要 import 整个
`arl_lite.devloop.queue`,慢且脆(导入失败会被报成「脚本坏了」,而不是
「某个依赖坏了」)。

这里改成 **import 它的 `entries()` 并在真实 `backlog.md` 上跑一遍解析**:
拿到的是「解析这条路还通不通」这个真问题,而不是「进程起没起来」那个假问题。
起进程那一半由 `test_baseline` 自己覆盖(pytest 会收集这个文件本身)。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SWEEP = REPO / "devloop" / "backlog_verify_sweep.py"


def test_the_sweep_script_is_there_and_parses_the_real_backlog():
    """sweep 脚本还在,而且还解析得出真实 backlog 里那批已完成的条目

    断言拆成三段,因为它们各自会坏在不同的原因上,混成一条报错会让人
    跑去查错的地方:
      - 文件没了 / 语法错      → 脚本烂了
      - `entries` 这个入口没了 → 有人重命名或删了它
      - 解析不出足够多的条目   → `_BACKLOG_LINE` 或完成标记判定坏了
    """
    assert SWEEP.exists(), (
        f"{SWEEP} 不见了 —— sweep 是「已完成的活的验收命令还跑不跑得通」"
        "唯一的检查,它一没,那件事就彻底没人管了"
    )
    src = SWEEP.read_text(encoding="utf-8")
    try:
        ast.parse(src)
    except SyntaxError as e:
        raise AssertionError(f"sweep 脚本语法错误({e})") from None

    assert "def entries(" in src, (
        "sweep 里没有 `entries()` —— 它是判据要用的入口。"
        "如果被重命名了,这里要跟着改(别顺手把判据删掉)"
    )

    sys.path.insert(0, str(REPO / "devloop"))
    from backlog_verify_sweep import entries

    all_entries = entries()
    assert all_entries, (
        "sweep 从真实 backlog.md 里一条都解析不出来 —— "
        "范围不对,它现在的输出不能拿来判断任何事"
    )
    done = [e for e in all_entries if e["done"]]
    assert len(done) >= 30, (
        f"sweep 只认出 {len(done)} 条已完成的条目(实测 42)。"
        "要么解析范围变了,要么完成标记判定坏了 —— "
        "不管哪种,sweep 现在的输出都是错的,而它**不会报错**"
    )


def test_the_protocol_says_when_the_sweep_must_be_run_by_hand():
    """协议里必须写清「什么时候必须手动跑 sweep」

    一条「不在门禁里」的检查,唯一防它腐烂的办法是**写明什么时候要跑**。
    没有这条规则,「偶尔想起来跑一下」和「永远不跑」在行为上没区别。

    为什么要钉一个**具体数字**(480 秒):它是协议里唯一的触发条件,
    而 `test_baseline` 的实测区间是 390–535 秒。写「跑得慢的时候」这种
    模糊话,和没写一样。

    ## 为什么不能直接写 `assert "480" in doc`

    首版就是这么写的,然后自查发现:**`480` 在文档别处也出现过**(L1818 那条
    round-index 记录里写着「认领才 480s」)。也就是说把那整条规则删掉,
    这道判据**照样绿** —— 判据自己坏掉比判据太松更危险。

    所以改成行内关联:`480` 必须和「必须手动跑」出现在**同一行**里,
    而且那条规则**有且只有一条**。别的上下文里的 480 顶不了这个位置。
    """
    doc = (REPO / "docs" / "devloop-protocol.md").read_text(encoding="utf-8")
    assert "backlog_verify_sweep" in doc, (
        "协议文档里一次都没提 `backlog_verify_sweep` —— "
        "那它就是一个没人知道它存在的脚本"
    )

    rule_lines = [ln for ln in doc.splitlines() if "必须手动跑" in ln]
    assert len(rule_lines) == 1, (
        f"协议里带「必须手动跑」的规则有 {len(rule_lines)} 条(应为 1)。"
        "0 条 = 触发条件被删了;多于 1 条 = 有互相矛盾或重复的版本,"
        "跑哪一条全凭临场发挥"
    )
    assert "480" in rule_lines[0], (
        f"那条规则里没有具体秒数:{rule_lines[0]!r}。"
        "「test_baseline 跑得慢的时候」这种模糊话和没写一样 —— "
        "实测区间是 390–535 秒,得有能把人逼到真去跑的数字"
    )
