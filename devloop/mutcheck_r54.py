"""r54 变异测试:「记的是资产数,列名承诺变更数」这个洞,判据真的会红吗

同 r41~r53:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮特有:「记检出数」和「记记入数」分不开

r54 把 `record_run` 的第二个参数从 `new_total`(资产数)换成
`detected_total`(变更检出数)。最自然的另一个候选是
`recorded_total`(实际写进库的条数)—— 在**没有截断**的那几轮里,
`detected_total == recorded_total`,两者行为完全一致。

本轮所有判据都不造截断场景(截断是 r50/r53 的地盘,它们各自钉着),
所以 M2 大概率存活。那是**已知局限**而不是洞,如实记着:
真要分清它们,得再造一个触到 `CHANGE_RECORD_CAP` 的场景 ——
而那会让本轮的判据越出它该守的范围(决策 #12)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

WATCHER = "arl_lite/core/watcher.py"
CLI = "arl_lite/cli.py"
TEST = "tests/test_monitor_last_change_count.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

_CALL = "                    Monitor(self.storage).record_run(m[\"id\"], detected_total)"
_CLI_BLOCK = '''        if m.get("last_run_at") is None:
            changes_txt = "never"
        else:
            changes_txt = str(m.get("last_change_count") or 0)'''

MUTANTS = [
    # ── r54 本体:传的还是资产数 ──
    ("M1", "record_run 又传 new_total —— 回到「只算新增资产」",
     [(WATCHER, _CALL,
       "                    Monitor(self.storage).record_run(m[\"id\"], new_total)")]),

    ("M2", "record_run 传 recorded_total —— 记的是「写进库的」而非「检出的」(期望存活)",
     [(WATCHER, _CALL,
       "                    Monitor(self.storage).record_run(m[\"id\"], recorded_total)")],
     True),

    ("M3", "record_run 传资产表**总行数** —— 和「变更数」毫无关系",
     [(WATCHER, _CALL,
       "                    Monitor(self.storage).record_run(m[\"id\"], wt.last_count)")]),

    ("M4", "record_run 传消失数 —— 只认消失,漏掉新增",
     [(WATCHER, _CALL,
       "                    Monitor(self.storage).record_run(m[\"id\"], disappeared_total)")]),

    # ── 根本忘了记 ──
    ("M5", "整个 record_run 调用被删掉 —— 那一列从此永远是 schema 的 DEFAULT",
     [(WATCHER, _CALL, "                    pass")]),

    # ── CLI 出口 ──
    ("M6", "monitor list 不再显示变更数 —— 修对了也没人能验证",
     [(CLI, _CLI_BLOCK, "        changes_txt = \"\"")]),

    ("M7", "never 的判据从 last_run_at 换成「值是不是 None」—— DEFAULT 0 会被读成 0",
     [(CLI, _CLI_BLOCK,
       '''        changes_txt = "never" if m.get("last_change_count") is None else str(m["last_change_count"])''')]),

    ("M8", "never 变成硬编码 0 —— 「从没跑过」被伪装成「跑过、没变更」",
     [(CLI, _CLI_BLOCK, '        changes_txt = "0"')]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 期望**存活**:去掉两条 CLI 判据并把显示删掉,其余判据全绿 ——
    # 记数那半边是对的,所以行为判据抓不到。说明「有没有出口」这件事
    # 只有那两条判据在守。
    ("C-cli", "skip 掉两条 CLI 判据,同时把变更数的显示删掉",
     [(TEST, "def test_monitor_list_shows_the_change_count(",
             "@pytest.mark.skip\ndef test_monitor_list_shows_the_change_count("),
      (TEST, "def test_monitor_list_says_never_for_a_monitor_that_never_ran(",
             "@pytest.mark.skip\ndef test_monitor_list_says_never_for_a_monitor_that_never_ran("),
      (CLI, _CLI_BLOCK, "        changes_txt = \"\"")], True),

    # 期望**被杀,但死在另一道闸**:首版我以为去掉「只有消失」那条之后
    # r54 就没人喊了,实测 `test_change_count_equals_changes_written_not_assets_added`
    # (3 新增 + 4 消失)照样红 —— 它要求 7,而传回 `new_total` 只给 3。
    # 如实记着:「变更数不等于新增数」被**两条**判据覆盖。
    ("C-disappeared", "skip 掉「只有消失」那条,同时把传参改回资产数",
     [(TEST, "def test_disappeared_only_run_records_a_nonzero_change_count(",
             "@pytest.mark.skip\ndef test_disappeared_only_run_records_a_nonzero_change_count("),
      (WATCHER, _CALL,
       "                    Monitor(self.storage).record_run(m[\"id\"], new_total)")], False),

    # 期望**被杀**。首版这个覆盖变异**存活**过,当时我以为是自己写出的
    # 一个真判据洞,查实不是:它 skip 的正是**当时唯一**能抓 `if True`
    # 的那条(`..._when_no_monitor_matches`)。覆盖变异问的就是
    # 「假设这条判据不存在,还有谁喊」——没有别人喊,存活是**正确**的答案,
    # 不是洞。
    #
    # 后来补的 `test_only_the_matching_monitor_gets_the_count` 是**加固**
    # 不是补洞:它在多 monitor 场景下、给不匹配的那个预置非零旧值,
    # 验证的是「旧值不被覆盖」,比 DEFAULT 0 → 3 更容易看出误写。
    # 于是本条现在有两道闸可依。
    ("C-scope", "skip 掉「只写匹配的那个」那条,同时让所有 monitor 都被记一遍",
     [(TEST, "def test_only_the_matching_monitor_gets_the_count(",
             "@pytest.mark.skip\ndef test_only_the_matching_monitor_gets_the_count("),
      (WATCHER, '''                if m.get("target") == wt.target:''',
       "                if True:")], False),
]

TOUCHED = {WATCHER, CLI, TEST}


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
        print("── 记什么数/有没有出口:存活 = 测试有洞 ──")
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
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
