"""r63 变异测试:「反向规则放过散文尾巴」这条,改坏了测试真的会红吗

同 r41~r62:变异 = 把实现/文案改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮的核心:方向

r61 那条规则是**反向**的 ——「尾巴非空就放过」;r63 换成**正向**的
「尾巴里有没有一个能补上缺失参数的**值**」。方向一变,第 8 处死路
(`arl-lite devloop done-item` 后面跟的是散文)才现形。

所以 M2 是本轮最要紧的一条:它把正向规则退回成反向**并且**把文档改回
有毛病的样子 —— 也就是 r63 修之前的完整状态。判据必须能认出那个状态。

## M1 和 M2 回答的是同一个问题的两半

M1 只把文档改回坏的(规则保持正向)—— 判据红在「**发现**了死路」。
M2 规则和文档一起退回 —— 判据红在「**分类条数对不上**」。
两条缺一不可:只有 M1 的话,把规则弱化回去没人发现;只有 M2 的话,
说明不了正向规则本身有没有用。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

DOC = "docs/devloop-protocol.md"
CRIT = "tests/test_cli_advice_commandable.py"
PINNED = "tests/test_advice_unchecked_are_classified.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

# 本轮修好的文档行(原样)
_FIXED_DOC = "`arl-lite devloop done-item <id>` 直接调 `q.finish(...)` 而**不传 note**。"
_BAD_DOC = "`arl-lite devloop done-item` 直接调 `q.finish(...)` 而**不传 note**。"

# 判据里的关键片段
_VALUE_RULE = '''    return first[:1] in ('"', "'", "`", "\\x00") or _is_placeholder(first)'''
_REVERSE_RULE = "    return bool(tail)"
_ACCEPT_ANY = '''    return first[:1] in ('"', "'", "`", "\\x00") or _is_placeholder(first) or True'''
_PLACEHOLDER_BRANCH = '    if "invalid choice" in says and _is_placeholder(_complained_value(says)):'
_DEAD_CLASSES = 'DEAD_CLASSES = ("散文收尾", "没给")'
_PINNED_COUNTS = 'PINNED = {"提取截断": 8, "占位符": 2, "散文收尾": 0, "没给": 0}'
_A3 = "def test_no_advice_command_carries_a_value_argparse_rejects("

MUTANTS = [
    # ── 本轮修的那处死路 ──
    ("M1", "文档里 done-item 又不写 item_id 了 —— 判据要能发现它",
     [(DOC, _FIXED_DOC, _BAD_DOC)]),

    # 本轮最要紧的一条:规则 + 文档一起退回,也就是 r63 修之前的完整状态
    ("M2", "正向规则退回成反向(尾巴非空就放过)+ 文档改回坏的 = r63 之前",
     [(CRIT, _VALUE_RULE, _REVERSE_RULE),
      (DOC, _FIXED_DOC, _BAD_DOC)]),

    # ── 正向规则本身被削弱,但文档是好的:只有合成输入能逮住 ──
    ("M3", "正向规则被削弱成「什么都算值」(文档是好的,真实数据看不出来)",
     [(CRIT, _VALUE_RULE, _ACCEPT_ANY)]),

    # ── 打标不许变成摆设 ──
    ("M4", "占位符那一支被删 —— 全被判成提取截断,占位符这一类归零",
     [(CRIT, _PLACEHOLDER_BRANCH, "    if False:")]),

    # 首版按「被杀」写,实测**存活** —— 这是本轮最难看的一个洞:
    # 把 DEAD_CLASSES 清空,`@pytest.mark.parametrize` 一个用例都不生成,
    # 判据**静默变成空转**而不是变红。牙齿在类列表里,而牙齿是可以直接
    # 拿掉的。补了 `test_the_class_lists_themselves_have_teeth` 才杀掉。
    ("M5", "死路两类被从「必须为 0」里拿掉 —— 判据静默变成空转",
     [(PINNED, _DEAD_CLASSES, 'DEAD_CLASSES = ()')]),

    # 首版按「被杀」写,实测**存活** —— 禁用词表里没有
    # `devloop-protocol.md`,而我恰恰写的就是这个。**禁用词表这种写法
    # 本身就是洞**:下一个人会写一个没进表的名字。改成盯不变量
    # (归类里不许出现任何含命令文本/路径形状的字面量),外加一条更硬的:
    # `_why_unchecked` 的签名里压根不许多出来源标签。
    ("M6", "归类逻辑里按来源文件硬编码(首版靠禁用词表,实测漏了)",
     [(CRIT, '''    says = _argparse_says(cmd)
    if "invalid choice" in says and _is_placeholder(_complained_value(says)):''',
       '''    says = _argparse_says(cmd)
    if "devloop-protocol.md" in cmd:
        return "提取截断"
    if "invalid choice" in says and _is_placeholder(_complained_value(says)):''')]),

    # ── 钉死的条数不许被悄悄改成「和现状一致」 ──
    ("M7", "钉死的条数被改成 0 —— 判据立刻对任何提取器都通过",
     [(PINNED, _PINNED_COUNTS, 'PINNED = {"提取截断": 0, "占位符": 0, "散文收尾": 0, "没给": 0}')]),
]

# 覆盖变异:同时改判据和文案,回答「哪条判据在守这里」。
COVERAGE_MUTANTS = [
    # 期望存活:钉数那条 + 合成输入那条都 skip 掉,规则和文档一起退回 ——
    # 也就是说**打标这套判据是「反向规则」的独占防线**,拿掉它没人拦。
    # 记着:这不代表规则本身没用(M3 证明它有用),而是说明「分类 + 钉数」
    # 是把规则长期锁住的那一层,和规则本身是两道。
    # 期望**被杀**,但我按「存活」写的 —— 又一次把守卫认错了。
    # 杀它的是 `test_dead_classes_are_reachable_at_all`(拿合成 tail
    # 断言 `_why_unchecked` 返回「散文收尾」),不是我以为的钉数那条。
    # 也就是说「反向规则」这道防线的守卫是**合成输入**,不是仓库数据 ——
    # 仓库数据只验「有没有死路」,验不了「规则的方向对不对」。
    # 这条比原判断更有用:规则方向必须靠合成输入钉,靠数据钉不住。
    ("C-pin", "skip 掉钉数与合成值形状两条,规则+文档一起退回",
     [(PINNED, "def test_the_counts_are_pinned(",
             "@pytest.mark.skip\ndef test_the_counts_are_pinned("),
      (PINNED, "def test_value_shaped_tail_is_a_positive_test(",
             "@pytest.mark.skip\ndef test_value_shaped_tail_is_a_positive_test("),
      (CRIT, _VALUE_RULE, _REVERSE_RULE),
      (DOC, _FIXED_DOC, _BAD_DOC)], False),

    # 期望**被杀**,而且杀它的不是 A3(A3 已 skip),是打标文件里那条
    # `len(DEAD) == 0`。也就是说仓库内容这一层,除了 A3 还有第二道。
    ("C-deepeq", "skip 掉 A3,同时把文档改回坏的",
     [(CRIT, _A3, "@pytest.mark.skip\n" + _A3),
      (DOC, _FIXED_DOC, _BAD_DOC)], False),
]

TOUCHED = {DOC, CRIT, PINNED}


def run_tests() -> tuple[bool, bool]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", PINNED, CRIT, "-q", "-p", "no:warnings"],
        capture_output=True, text=True, env=env, timeout=900)
    broken = any(s in p.stdout for s in _BROKEN_SOURCE)
    return p.returncode == 0, broken


def apply(edits) -> bool:
    for path, old, new in edits:
        s = open(path, encoding="utf-8").read()
        if old not in s:
            return False
        open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
    return True


def revert():
    for path in TOUCHED:
        bak = path + ".mutbak"
        if os.path.exists(bak):
            shutil.copy(bak, path)


def sweep(mutants, expect_default: bool) -> list[str]:
    survived = []
    for entry in mutants:
        mid, desc, edits = entry[0], entry[1], entry[2]
        expect_survival = entry[3] if len(entry) > 3 else expect_default
        if not apply(edits):
            print(f"[{mid}] !! 变异没打上(原串不匹配) —— 变异本身失效了")
            survived.append(mid + "(没打上)")
            continue
        passed, broken = run_tests()
        revert()
        if broken:
            print(f"[{mid}] !! 变异把源文件写成了语法错误 —— 这种红不算杀死。{desc}")
            survived.append(mid + "(假杀:语法错误)")
            continue
        killed = not passed
        ok = (not killed) if expect_survival else killed
        print(f"[{mid}] {('杀死' if killed else '*** 存活 ***')}  {desc}")
        if not ok:
            survived.append(mid)
    return survived


def main() -> int:
    for path in TOUCHED:
        shutil.copy(path, path + ".mutbak")
    pristine = {p: open(p, "rb").read() for p in TOUCHED}
    bad = []
    try:
        if not run_tests()[0]:
            print("[对照] 基线就红,测不了变异")
            return 1
        print("[对照] 未变异时全通过 —— 符合预期\n")
        print("── 方向 / 打标不许成摆设 / 仓库内容:存活 = 判据有洞 ──")
        bad = sweep(MUTANTS, expect_default=False)
        print("\n── 同时改判据和文档:回答「哪条判据在守这里」 ──")
        bad += sweep(COVERAGE_MUTANTS, expect_default=True)
    finally:
        revert()
        for p in TOUCHED:
            if os.path.exists(p + ".mutbak"):
                os.remove(p + ".mutbak")
        left = [p for p in sorted(TOUCHED)
                if open(p, "rb").read() != pristine[p]]
        if left:
            print(f"!! 复原后这些文件的内容变了:{left}")
            return 1
    print()
    if bad:
        print(f"!! {len(bad)} 条变异不符合预期: {bad}")
        return 1
    print(f"{len(MUTANTS)} 个实现变异结果均符合预期,"
          f"{len(COVERAGE_MUTANTS)} 个覆盖变异结果均符合预期")
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
