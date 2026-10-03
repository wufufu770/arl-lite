"""r52 变异测试:「少算的资产」这个洞,判据真的会红吗

同 r41~r51:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮特有:两个方向的错都得能被抓

r52 的 bug 是「少算」(3 种 vs 5 种)。但**最容易被采用的修法是错的**:
`get_stats()` 返回的键里就有那 5 张资产表,看起来「直接遍历它的键」
更通用、更不会漏 —— 而它的键里还有 `tasks` 和 `correlations`。
那是同一类错的镜像:不该算的算了。

所以 M2/M3 这两个「多算」的变异是本轮的重点:判据不能只认「少算」,
还得认「多算」。两者都要杀,才算真的钉住了「什么是资产」。

## 覆盖变异回答「哪条测试在守这里」

行为测试测得动的东西,结构判据就是多余的;但行为测不动的
(「下次第 6 种资产来的时候会不会又漏」),只有结构判据能守。
两个覆盖变异分别把这两半拆开单独试。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

WATCHER = "arl_lite/core/watcher.py"
TEST = "tests/test_watcher_new_count.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

# 把整段派生换成手抄三元组 / 从 get_stats 取键,这两处是 r52 之前的形状
_DERIVE = "        asset_tables = tuple(Monitor._ASSET_TABLES.values())"
_DICT = '''        new_by_type = {t: max(0, after.get(t, 0) - before.get(t, 0))
                       for t in asset_tables}'''
_LASTCOUNT = "        wt.last_count = sum(after.get(t, 0) for t in asset_tables)"
_LOOP = "        for asset_type, table in Monitor._ASSET_TABLES.items():"
_NEWTOTAL = "        new_total = sum(new_by_type.values())"

MUTANTS = [
    # ── 少算(就是 r52 本人的 bug) ──
    ("M1", "new_by_type 回到手抄三元组 —— ports 和 sites 又漏了(r52 的真 bug)",
     [(WATCHER, _DICT,
       '''        new_by_type = {t: max(0, after.get(t, 0) - before.get(t, 0))
                       for t in ("hosts", "domains", "findings")}''')]),

    ("M2", "last_count 回到 hosts+domains —— 字段名写着「资产数」,只算两种",
     [(WATCHER, _LASTCOUNT,
       '        wt.last_count = after.get("hosts", 0) + after.get("domains", 0)')]),

    ("M3", "last_count 只算 hosts 一张",
     [(WATCHER, _LASTCOUNT, "        wt.last_count = after.get(\"hosts\", 0)")]),

    # ── 多算(最容易踩的「修法」) ──
    ("M4", "清单从 get_stats() 的键取 —— tasks/correlations 混进资产数",
     [(WATCHER, _DERIVE,
       "        asset_tables = tuple(after.keys())")]),

    ("M5", "清单多带一张非资产表",
     [(WATCHER, _DERIVE,
       '        asset_tables = tuple(Monitor._ASSET_TABLES.values()) + ("tasks",)')]),

    # ── 汇总日志:手写三个,总数对但日志说谎 ──
    ("M6", "汇总日志回到手写三项 —— 总数对,日志只提三种",
     [(WATCHER, '''            f"({' '.join(f'{t}={new_by_type[t]}' for t in asset_tables)})"''',
       '''            f"(hosts={new_by_type['hosts']} domains={new_by_type['domains']} "
            f"findings={new_by_type['findings']})"''')]),

    # ── 清单来源退回手抄(行为仍对,只有结构判据该抓) ──
    ("M7", "资产清单手抄成五元组 —— 行为全对,但第 6 种资产来时又会漏",
     [(WATCHER, _DERIVE,
       '        asset_tables = ("domains", "hosts", "ports", "sites", "findings")')]),

    # ── 逐类检测那个循环:两份清单会各自漂 ──
    ("M8", "逐类检测的循环回到手抄五元组 —— 和数新增的清单分成两份",
     [(WATCHER, _LOOP,
       '''        for asset_type, table in (("domain", "domains"), ("host", "hosts"),
                                  ("port", "ports"), ("site", "sites"),
                                  ("finding", "findings")):''')]),

    ("M9", "逐类检测只遍历两类 —— ports/sites 的变更不再被记",
     [(WATCHER, _LOOP,
       '''        for asset_type, table in (("domain", "domains"),
                                  ("host", "hosts")):''')]),

    # ── 累加:少算的差值会漏到累计里 ──
    ("M10", "new_total 取 max 而不是求和",
     [(WATCHER, _NEWTOTAL, "        new_total = max(new_by_type.values())")]),

    ("M11", "new_count 不再累计 —— 那个字段是用户唯一能查的总数",
     [(WATCHER, "        wt.new_count += new_total",
       "        wt.new_count = new_total")]),

    # ── 负数:资产被清库时差值为负,不能报成「新增 -5」 ──
    ("M12", "去掉 max(0) —— 并发清库时报出负的新增数",
     [(WATCHER, _DICT,
       '''        new_by_type = {t: after.get(t, 0) - before.get(t, 0)
                       for t in asset_tables}''')]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 期望**存活**:全部行为测试 skip,少算回两种时,结构判据该抓到。
    # 记它是为了说明结构判据不是摆设 —— 即使行为那半边整个失效,
    # 「清单必须从契约表派生」这条仍然有人喊。
    ("C-behavior", "skip 掉主行为测试,同时把 new_by_type 改回少算两种",
     [(TEST, "def test_five_asset_types_all_counted_in_the_new_total(",
             "@pytest.mark.skip\ndef test_five_asset_types_all_counted_in_the_new_total("),
      (WATCHER, _DICT,
       '''        new_by_type = {t: max(0, after.get(t, 0) - before.get(t, 0))
                       for t in ("hosts", "domains")}''')], False),

    # 期望**被杀,但死在另一道闸**:`test_both_asset_list_reads_come_from_the_contract_table`
    # 第一版这里期望存活(我以为「skip 掉反手抄那条,行为测试该全绿」),
    # 实测被 `test_contract_table_and_detected_types_stay_in_step` 逮住 ——
    # 它末尾也断言了 `asset_tables` 的赋值来源。如实记着:
    # 「反手抄」这个意图被**两条**判据覆盖,不是只有一条。
    ("C-structure", "skip 掉「不许手抄清单」那条,同时把手抄五元组塞回来",
     [(TEST, "def test_run_target_has_no_handwritten_asset_table_tuple(",
             "@pytest.mark.skip\ndef test_run_target_has_no_handwritten_asset_table_tuple("),
      (WATCHER, _DERIVE,
       '        asset_tables = ("domains", "hosts", "ports", "sites", "findings")')],
     False),

    # 期望**被杀,但死在另一道闸**:只 skip 了「新增数」那条,
    # `test_last_count_ignores_non_asset_tables` 和
    # `test_five_asset_types_all_counted_in_the_new_total` 都还在,
    # 另外 `test_contract_table_and_detected_types_stay_in_step` 也会因为
    # 赋值里不再引用契约表而红 —— 三道。如实记着。
    ("C-overcount", "skip 掉「非资产表不算资产」两条,同时从 get_stats 取键",
     [(TEST, "def test_task_and_correlation_rows_are_not_counted_as_assets(",
             "@pytest.mark.skip\ndef test_task_and_correlation_rows_are_not_counted_as_assets("),
      (WATCHER, _DERIVE, "        asset_tables = tuple(after.keys())")], False),
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
        print("── 少算/多算/来源/日志:存活 = 测试有洞 ──")
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
    print(f"{len(MUTANTS)} 个实现变异全杀,"
          f"{len(COVERAGE_MUTANTS)} 个覆盖变异结果均符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
