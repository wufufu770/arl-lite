"""r64 变异测试:「接回去重验」这一步,拆开了测试真的会红吗

同 r41~r63:变异 = 把实现/文案改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮的核心:重建这一步最容易被拆成「看起来还在跑」

重建不是一个布尔判断,它是一条链:切出值 → 接回 ASCII token → 填槽 → 跑 parser。
每一环都能被悄悄退化,而退化之后 8 条 UNVERIFIED **还是 8 条**、判定**还是
「干净」** —— 看起来一切正常,只是地又没扫了。所以每个环节都配一个变异,
而且大部分配的是**合成输入**:仓库数据全绿时,它们是唯一的牙齿。

## 删掉 M7 的理由,和 M5 首版的教训是同一件事

首版有个 M7:「把「验过的条数」下限从 98 改成 0」。实测**存活**。查下去发现
它**在原理上就不可观测**:数据没变,只有断言变松了 —— 没有任何判据能发现
「有人把一个数字从 98 改成了 0」,因为那条判据自己就是那个数字。
r47 早写过:空操作变异不证明任何事。这条就是空操作,删掉。

**要让「覆盖面被削掉」可观测,必须同时削数据**:那正是 M2/M3/M4 干的 ——
把重建链某一环拆掉,8 条 UNVERIFIED 立刻变少,钉数判据就红。

M5 首版也是同一族:我把**非死路**那一支改了,而重建结果是死路时压根走不到
那支 —— 变异打在了不产生差异的路径上。改成拆**死路**那一支才真能混桶。
两次都是同一个错:**没先确认变异会产生差异,就直接写进脚本。**
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

DOC = "docs/devloop-protocol.md"
CRIT = "tests/test_cli_advice_commandable.py"
REVERIFY = "tests/test_advice_truncated_tail_reverified.py"
CLASSIFY = "tests/test_advice_unchecked_are_classified.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

_FIXED_DOC = "| `arl-lite devloop add <id> \"标题\" --priority 2` | 手动加待办 |"
_BAD_DOC = "| `arl-lite devloop add <id> \"标题\" -p 2` | 手动加待办 |"

_SLOT_FALLBACK = '''    if says and SLOT_DUMMY in says and "invalid choice" in says:
        m = re.search(r"choose from '([^']+)'", says)
        if m:
            argv = [a.replace(SLOT_DUMMY, m.group(1).split(",")[0].strip()) for a in argv]
            says, extra = _run_argv(argv)'''
_QUOTED_CUT = '''    if tail[0] in ('"', "'", "`"):
        end = tail.find(tail[0], 1)
        if end == -1:
            return None
        value, rest = tail[1:end], tail[end + 1:]'''
_UNGUARDED_FIRST = "        pass  # ← 守卫被拆了:散文和注释的第一个词会被当成参数值"
_GUARDED_FIRST = ('''        if not (value == FSTRING_SLOT or _is_placeholder(value)):'''
                  + "\n            return None")
_NO_TAIL_STOP = '            if not t or t in ("|", "#") or not t.isascii() or not TAIL_TOK.match(t):'
_PREFIX_DEAD = '''    sub, _ = _verified_by_reconstruction(cmd, tail)
    if sub.startswith("deadend"):
        return sub
    return f"unverified:{sub}"'''
_BLIND_REVERIFY = '''    sub, _ = _verified_by_reconstruction(cmd, tail)
    if False:
        return sub
    return f"unverified:{sub}"'''
_COVERAGE_FLOOR = "    verified = len(CLEAN) + len(UNVERIFIED)"
_REVERIFY_TEST = "def test_reconstruction_actually_catches_what_is_behind_the_cut("

MUTANTS = [
    # ── 本轮修的那处死路,重建必须能自动逮到 ──
    ("M1", "文档里 -p 又回来了 —— 重建必须现形,不许再靠人眼",
     [(DOC, _FIXED_DOC, _BAD_DOC)]),

    # ── 重建链的每一环 ──
    ("M2", "引号边界切错(用 shlex 剥过引号的 token 当值)—— 5 条重建不出来",
     [(CRIT, _QUOTED_CUT, '''    if False:
        end = -1
        value, rest = None, None''')]),

    ("M3", "第一个值没形状守卫 —— 散文和注释被当成参数接回命令",
     [(CRIT, _GUARDED_FIRST, _UNGUARDED_FIRST)]),

    ("M4", "尾巴不认停表符和散文 —— 整行散文全被接进命令",
     [(CRIT, _NO_TAIL_STOP, '            if not t:')]),

    # ── 死路不许带 unverified 前缀混进两桶 ──
    # 首版这只是个**空操作**:我把非死路那一支改成了返回 `unverified:clean`,
    # 而重建结果是死路时压根走不到那支 —— 变异打在了一条不产生差异的路径上。
    # 改成 `if False:` 把**死路那一支**拆掉,这才真的能让一个死路混进两桶。
    ("M5", "重建发现的死路被塞进 unverified —— 一个死路能同时算进两桶",
     [(CRIT, _PREFIX_DEAD, _BLIND_REVERIFY)]),

    # 首版按「存活」写,实测**被杀**,而且是三道判据同时兜住:
    # A3、重建判定钉数、以及那组合成输入。也就是说我原先写的
    # 「靠 argparse 纠正槽这件事没有直接判据在守」是**错的** ——
    # 我又是没查就下的结论。删掉槽的纠正会立刻让 3 条带槽的建议
    # 从「重验通过」变成「重建不出来」。
    ("M6", "槽的第二步纠正被删(永远用哑元)",
     [(CRIT, _SLOT_FALLBACK, "    pass")]),

    # ── 重建的牙齿:合成输入那组 ──
    # 首版按「存活」写,实测**被杀** —— 我漏算了另外两条没被 skip 的判据。
    # 杀它的是 `test_no_truncated_item_is_actually_a_dead_end`(在同一个文件里,
    # 我只记得去 skip 合成输入那组)。这条如实记着:剥判据时得先数清楚有几道。
    ("M8", "把「重建能逮到截断点后面的死路」那组合成输入 skip 掉,同时把 -p 写回去",
     [(REVERIFY, _REVERIFY_TEST, "@pytest.mark.skip\n" + _REVERIFY_TEST),
      (DOC, _FIXED_DOC, _BAD_DOC)], False),
]

# 覆盖变异:同时改判据和实现,回答「哪条判据在守这里」。
COVERAGE_MUTANTS = [
    # 期望**被杀**。这一条是「剥到最后一层」:把重建相关的**全部四条**判据
    # 都 skip 掉(A3 之外的),只剩 A3(`test_no_advice_command_carries_a_value_
    # argparse_rejects`)在仓库内容这一层。
    # 结论:**A3 是死路的最后一道防线**。前面那些(重建判定钉数、
    # 死路不许混桶、合成输入)都能被关掉,A3 关掉之后仓库里就没人看
    # 「有没有死路」了 —— 所以 A3 本身不能被 skip,也没有替代品。
    ("C-last-line", "skip 掉重建相关的全部四条判据,同时把 -p 写回去",
     [(REVERIFY, _REVERIFY_TEST, "@pytest.mark.skip\n" + _REVERIFY_TEST),
      (REVERIFY, "def test_no_truncated_item_is_actually_a_dead_end(",
                 "@pytest.mark.skip\ndef test_no_truncated_item_is_actually_a_dead_end("),
      (REVERIFY, "def test_the_reconstruction_verdicts_are_pinned(",
                 "@pytest.mark.skip\ndef test_the_reconstruction_verdicts_are_pinned("),
      (CLASSIFY, "def test_the_counts_are_pinned(",
                 "@pytest.mark.skip\ndef test_the_counts_are_pinned("),
      (DOC, _FIXED_DOC, _BAD_DOC)], False),

    # 期望**被杀**,杀它的是分桶钉数 —— 说明「四桶条数」是重建那一步的第二道。
    ("C-bucket", "skip 掉重建那组合成输入 + 死路不允许混桶,同时把 -p 写回去",
     [(REVERIFY, _REVERIFY_TEST, "@pytest.mark.skip\n" + _REVERIFY_TEST),
      (REVERIFY, "def test_no_truncated_item_is_actually_a_dead_end(",
                 "@pytest.mark.skip\ndef test_no_truncated_item_is_actually_a_dead_end("),
      (CRIT, _PREFIX_DEAD, _BLIND_REVERIFY),
      (DOC, _FIXED_DOC, _BAD_DOC)], False),
]

TOUCHED = {DOC, CRIT, REVERIFY, CLASSIFY}


def run_tests() -> tuple[bool, bool]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest",
         CRIT, REVERIFY, CLASSIFY, "-q", "-p", "no:warnings"],
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
        print("── 重建链每一环 / 死路不许混桶 / 覆盖面不许缩 ──")
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
    raise SystemExit(main())
