"""r53 变异测试:「有消失就把截断留痕吞掉」这个洞,判据真的会红吗

同 r41~r52:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮特有:有一个变异**应该**存活

M4 删掉 `account_change_recording` 里的 `max(0, ...)`。它期望存活,
而且这是**对的设计**,不是判据的洞:

    r53 修完之后,`recorded <= detected` 是个恒成立的不变量
    (helper 层的 `test_helper_never_lets_recorded_outrun_detected` 就钉它)。
    所以 `max(0, ...)` 永远不触发 —— 它是「别把 bug 报成负数」的防御,
    代价是两行代码、零行为。

    而删掉它是有害的:r53 修的对称性哪天被破坏,第一个报出来的会是
    一个负的丢弃数,而不是 0 —— 那是把一个 bug 伪装成另一个 bug 的证据。

所以这条留成变异、期望存活,把「它为什么该留着」写在这儿,
免得下一轮有人看到「没测试覆盖」就去删。

## 覆盖变异:本轮的判据高度重叠,如实记着

C1/C2 期望被杀,但**不是**被我以为的那条判据杀的。首版我把
「对照实验」和「主判据」当成各守一半,实测发现两边互相都能兜住对方,
外加消失侧那条和 AST 那条。重叠是好不是坏(改一处红一片),
但「哪条是唯一防线」这种话不许说 —— 见决策 #18。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

WATCHER = "arl_lite/core/watcher.py"
TEST = "tests/test_watcher_dropped_vs_disappeared.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

_HELPER_RET = "    return detected + int(found), recorded + (int(found) - int(dropped))"
_CALL_NEW = """            detected_total, recorded_total = account_detected_and_recorded(
                detected_total, recorded_total, len(new_rows), dropped_new)"""
_CALL_GONE = """            detected_total, recorded_total = account_detected_and_recorded(
                detected_total, recorded_total, len(gone), dropped_gone)"""
_ACCOUNT = "    dropped = max(0, int(detected) - int(recorded))"
_GONE_TOTAL = "            disappeared_total += len(gone)"

MUTANTS = [
    # ── r53 本体:消失不再并进 detected ──
    ("M1", "消失分支不调 helper —— 回到 r53 之前的两个口径(有消失就报 0)",
     [(WATCHER, _CALL_GONE, "            pass")]),

    ("M2", "helper 只并 recorded、不并 detected —— 正是 r53 的根因",
     [(WATCHER, _HELPER_RET, "    return detected, recorded + (int(found) - int(dropped))")]),

    ("M3", "helper 两个口径都不加 —— 「检出」「记入」双双失联",
     [(WATCHER, _HELPER_RET, "    return detected, recorded")]),

    # ── 减法方向 ──
    ("M4", "删掉 `max(0, ...)` —— **期望存活**:对称后它永远不触发,是防御",
     [(WATCHER, _ACCOUNT, "    dropped = int(detected) - int(recorded)")], True),

    ("M5", "helper 里 recorded 加 found 而非 found-dropped —— 记入数算成检出数",
     [(WATCHER, _HELPER_RET,
       "    return detected + int(found), recorded + int(found)")]),

    ("M6", "helper 里 recorded 加 dropped —— 减法方向反了",
     [(WATCHER, _HELPER_RET,
       "    return detected + int(found), recorded + int(dropped)")]),

    # ── 少调一处 / 重复并入 ──
    ("M7", "新增分支不再走 helper —— 少一处调用,两个分支各走各的",
     [(WATCHER, _CALL_NEW, "            detected_total += len(new_rows)")]),

    ("M8", "消失被并进 detected 两次 —— 丢弃数虚高",
     [(WATCHER, _CALL_GONE,
       """            detected_total, recorded_total = account_detected_and_recorded(
                detected_total, recorded_total, len(gone), dropped_gone)
            detected_total, recorded_total = account_detected_and_recorded(
                detected_total, recorded_total, len(gone), dropped_gone)""")]),

    ("M9", "found 传成「已记入的条数」—— 减法被做两次",
     [(WATCHER, _CALL_GONE,
       """            detected_total, recorded_total = account_detected_and_recorded(
                detected_total, recorded_total,
                len(gone) - dropped_gone, dropped_gone)""")]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 期望**被杀,但不是死在「对照实验」那条**:首版我以为去掉它之后
    # r53 的 bug 就没人喊了,实测主判据、消失侧那条、AST 那条都在喊。
    # 如实记着:这个洞被**四条**判据覆盖。
    ("C-compare", "skip 掉「对照实验」那条,同时把消失分支的口径改回去",
     [(TEST, "def test_same_dropped_count_with_and_without_disappeared(",
             "@pytest.mark.skip\ndef test_same_dropped_count_with_and_without_disappeared("),
      (WATCHER, _CALL_GONE, "            pass")], False),

    # 期望**被杀,但不是死在主判据**:去掉它之后,「对照实验」和
    # 「消失侧自己也留痕」两条都还在。
    ("C-main", "skip 掉主判据,同样把消失分支的口径改回去",
     [(TEST, "def test_disappeared_assets_do_not_erase_the_truncation_count(",
             "@pytest.mark.skip\ndef test_disappeared_assets_do_not_erase_the_truncation_count("),
      (WATCHER, _CALL_GONE, "            pass")], False),

    # 期望**存活**:这条日志判据是唯一的防线。去掉它并把
    # `disappeared_total` 清零,其余全部判据照常全绿 —— 库里记了 20 条
    # 消失,而用户看的那行日志会说 0 条。
    ("C-log", "skip 掉日志判据,同时把 disappeared_total 清零",
     [(TEST, "def test_disappeared_log_count_matches_what_was_recorded(",
             "@pytest.mark.skip\ndef test_disappeared_log_count_matches_what_was_recorded("),
      (WATCHER, _GONE_TOTAL, "            disappeared_total += 0")], True),
]

TOUCHED = {WATCHER, TEST}


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
    for p in TOUCHED:
        shutil.copy(p, p + ".mutbak")
    pristine = {p: open(p, "rb").read() for p in TOUCHED}
    bad = []
    try:
        if not run_tests()[0]:
            print("[对照] 基线就红,测不了变异")
            return 1
        print("[对照] 未变异时全通过 —— 符合预期\n")
        print("── 口径对称/减法方向/少调一处:存活 = 测试有洞 ──")
        bad += sweep(MUTANTS, expect_default=False)
        print("\n── 同时改测试和生产代码:回答「哪条测试在守这里」 ──")
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
        print(f"不符合预期 {len(bad)} 个:{bad}")
        return 1
    print(f"{len(MUTANTS)} 个实现变异(其中 1 个期望存活),"
          f"{len(COVERAGE_MUTANTS)} 个覆盖变异结果均符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
