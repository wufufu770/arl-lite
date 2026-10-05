"""r61 变异测试:判据从「两类」升级到「三类」之后,每一条新分支真有人守吗

同 r41~r60:变异 = 把实现/文案改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮的形状和前几轮不同:变的不是产品,是**判据自己**

r60 立的判据能跑,但它有三个缺陷会把「能跑的建议」静默判成「查不动」
(shlex 反引号、rstrip 剥占位符、闭引号粘末位 token)。这轮的变异大半打在
判据文件上,问的是:这些修复各自有没有人在守。

## M3/M8 是这一轮的关键

r61 之后仓库里**一条死路都没有**(DEAD=0),所以 A3 在当前状态下是**空转**的:
把 `invalid choice` 那一支整个删掉、把注释尾巴那条规则整个删掉,A3 照样绿。
真正兜住它们的是 `test_verdict_classifies_each_argparse_complaint` ——
拿合成输入直接钉分类器的每一类。这正是「恒真测试比没有测试更糟」的反面:
如果没补这组单测,这一轮的判据看着在守,其实已经不守了。

## M11 是设计性存活

`used`(尾巴起点)用原 token 长度算,只影响**给人看的诊断信息**,
不影响任何判定(判定只看尾巴空不空、是不是注释)。所以它没有守卫是正常的,
如实记着:它是可读性修复,不是判定修复。

## 覆盖变异量出来的两条「谁在守」

* `C-value` / `C-value-logic-only` 都**存活**:skip 掉 A3 之后,`--scale 0.2`
  回到 docstring 没有任何声音。分类器单测救不了 —— 它拿的是合成输入,压根不读仓库。
  结论:**A3 是仓库内容的独占防线,合成单测只守分类逻辑,两者缺一不可**。
  留着 `C-value-logic-only`(只 skip A3)这一条,就是为了把这件事钉住。
* `C-backtick` **被杀**,但不是被那条指名道姓的回归判据(它已 skip),而是被
  `test_criterion_actually_checks_enough_commands` 的提取条数下限。反退化下限
  不只防「什么都不产出」,也防「提取器变窄」。

顺带记一个本轮自己写错的断言:`UNCHECKED + CLEAN == ADVICE` 漏了 DEAD 一类,
歪打正着当成了死路后盾,害我以为 A3 之外还有别的守卫。名字承诺的语义必须和
承载的语义对得上(r35 那条),断言也一样。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

CLI = "arl_lite/cli.py"
PERF = "arl_lite/core/perf_bench.py"
TEST = "tests/test_cli_advice_commandable.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

# ── 本轮修好的 3 处(原样) ──
_FIXED_SCALE = "arl-lite perf-bench --scale small"
_FIXED_RUN = "\"[i] no correlations found (target too clean? run 'arl-lite run -t <target>' first)\""
_FIXED_LICENSE = '"Modified by contributors of arl-lite"'

# ── 判据里的关键片段 ──
_BACKTICK = 'seg = seg.replace("`", " ")'
_QUOTE_CUT = "cuts = [i for i in (seg.find(c) for c in \"'\\\"\") if i > 0]"
_USED = 'used = len("arl-lite") + 1'
_PLACEHOLDER = 'if t[:1] not in "<{":'
_SHAPE = 'if keep and not keep[0][:1].isalpha() and not keep[0].startswith("-"):'
_INVALID_CHOICE = 'if "invalid choice" in says:'
_COMMENT_TAIL = 'if not tail or tail.startswith("#"):'
_A3 = "def test_no_advice_command_carries_a_value_argparse_rejects("
_VERDICT_TESTS = "def test_verdict_classifies_each_argparse_complaint("

MUTANTS = [
    # ── 把本轮修的死路原样放回去:必须被杀 ──
    ("M1", "perf-bench 的 --scale 0.2 又回来了 —— A3 唯一的活",
     [(PERF, _FIXED_SCALE, "arl-lite perf-bench --scale 0.2")]),

    ("M2", "「没关联分析」的建议又写回光秃秃的 arl-lite run(必填 -t)",
     [(CLI, _FIXED_RUN,
       '"[i] no correlations found (target too clean? run \'arl-lite run\' first)"')]),

    # ── 分类器的两条新分支:当前 DEAD=0,A3 空转,靠合成输入单测兜 ──
    ("M3", "把 `invalid choice` 那一支整个删掉 —— 值错又混回「查不动」",
     [(TEST, _INVALID_CHOICE, 'if "invalid choice" in says and False:')]),

    ("M8", "把「尾巴是注释也算没给」这条删掉 —— 注释被当成参数值",
     [(TEST, _COMMENT_TAIL, "if not tail:")]),

    # ── 提取器三个缺陷,各自有指名道姓的回归判据 ──
    ("M4", "shlex 反引号缺陷回来了 —— <name> 被粘掉,gate <name> 记成 gate",
     [(TEST, _BACKTICK, 'seg = seg')]),

    ("M5", "rstrip 又剥占位符 —— <provider> 变成 <provider,flag 后「没有值」",
     [(TEST, _PLACEHOLDER, 'if True:')]),

    ("M6", "闭引号切分没了 —— 凭空造出 arl-lite run first",
     [(TEST, _QUOTE_CUT, "cuts = []")]),

    ("M7", "首 token 形状规则没了 —— 「等价于安装后的 `arl-lite`:」被当命令",
     [(TEST, _SHAPE, "if keep:")]),

    # 期望**存活**,而且这是**对**的:见文件头 M11 段。
    # 尾巴起点只影响给人看的诊断信息,判定只看「空不空 / 是不是注释」,
    # 所以它没有守卫是正常的,如实记着。
    ("M11", "尾巴起点又算错(只影响诊断信息,不影响判定)",
     [(TEST, _USED, "used = 0")], True),
]

# 覆盖变异:同时改判据和文案,回答「哪条判据在守这里」。
COVERAGE_MUTANTS = [
    # 期望存活:把 A3 和分类器单测一起拿掉,值错那条就没人拦了 ——
    # A3 是**仓库内容**的独占防线,合成单测守的是**分类逻辑**不是仓库内容。
    ("C-value", "skip 掉 A3 + 分类器单测,同时把 --scale 0.2 写回去",
     [(TEST, _A3, "@pytest.mark.skip\n" + _A3),
      (TEST, _VERDICT_TESTS, "@pytest.mark.skip\n" + _VERDICT_TESTS),
      (PERF, _FIXED_SCALE, "arl-lite perf-bench --scale 0.2")], True),

    # 期望存活:只拿掉 A3,分类器单测还在 —— 单测是合成输入,不读仓库,
    # 所以它压根看不见 `--scale 0.2` 回到了 docstring。这条如实记着:
    # **合成单测不能替代对仓库内容的断言**,两者缺一不可。
    ("C-value-logic-only", "只 skip A3(留着分类器单测),把 --scale 0.2 写回去",
     [(TEST, _A3, "@pytest.mark.skip\n" + _A3),
      (PERF, _FIXED_SCALE, "arl-lite perf-bench --scale 0.2")], True),

    # 期望**被杀**,而且是个好消息:兜住它的不是那条指名道姓的回归判据(已 skip),
    # 而是 `test_criterion_actually_checks_enough_commands` 的**提取条数下限** ——
    # 反引号缺陷一回来,提到 99 条建议掉到 89 条,跌破下限 90。
    # 也就是说反退化下限不只防「提取器退化成什么都不产出」,也防「提取器变窄」:
    # 即使专门给它写的那条回归被 skip 掉,总面积一变它就会响。
    # 首版我按「指名判据是独占防线」预期它存活,实测被下限兜住 —— 记下来。
    ("C-backtick", "skip 掉反引号那条回归判据,同时把反引号修复撤掉",
     [(TEST, "def test_extractor_treats_backticks_as_code_fences_not_quotes(",
             "@pytest.mark.skip\ndef test_extractor_treats_backticks_as_code_fences_not_quotes("),
      (TEST, _BACKTICK, 'seg = seg')], False),

    # 期望**被杀**,杀它的是 A1 —— 说明 A1 与 A3 互不替代:
    # 值错(A3 管的)和子命令拼错(A1 管的)是两类,skip A3 不影响 A1。
    ("C-subcommand", "skip 掉 A3,同时把子命令名改错",
     [(TEST, _A3, "@pytest.mark.skip\n" + _A3),
      (CLI, "'arl-lite run -t <target>'", "'arl-lite runz -t <target>'")], False),
]

TOUCHED = {CLI, PERF, TEST}


def run_tests() -> tuple[bool, bool]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST, "-q", "-p", "no:warnings"],
        capture_output=True, text=True, env=env)
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
        print("── 分类器新分支 / 提取器缺陷 / 产品文案:存活 = 判据有洞 ──")
        bad = sweep(MUTANTS, expect_default=False)
        print("\n── 同时改判据和产品代码:回答「哪条判据在守这里」 ──")
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
