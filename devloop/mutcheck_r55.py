"""r55 变异测试:「静默截断」和「总数必须与列表同源」,判据真的会红吗

同 r41~r54:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮的要害:那个「总数」不许自己长出来

r55 修的是「说真话」,但真话本身也可能说错:`count_changes` 如果
单独拼一遍 WHERE,两边条件漂了,就会报出「共 200 条」而当前筛选下
其实只有 50 条 —— 那比不报更坏,因为它是个看起来精确的假数字。

所以本轮的变异重心不在 CLI,在**两个函数会不会漂**。M2/M3/M10/M11
都是「少一个条件」的各种写法,而 `test_count_matches_an_unbounded_list_
under_every_filter` 那条不变式是它们的共同防线(8 组参数化,任一漂移都红)。

## C-derive 这条覆盖变异值得单独说

`count_changes` 若写成 `len(list_changes(limit=10**6))`,那条不变式
**恒成立** —— 它数的就是同一个列表的长度,永远对得上。所以行为判据
被绕过,只有结构判据(`不许从受限列表推导`)能喊。这解释了为什么
r55 除了不变式还得加两条 AST 判据。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

MONITOR = "arl_lite/core/monitor.py"
CLI = "arl_lite/cli.py"
TEST = "tests/test_monitor_changes_limit_notice.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

_WHERE_ASSET_TYPE = '''    if asset_type:
        sql += " AND asset_type = ?"
        params.append(asset_type)'''
_WHERE_CHANGE_TYPE = '''    if change_type:
        sql += " AND change_type = ?"
        params.append(change_type)'''
_WHERE_SINCE = '''    if since:
        frag, val = _since_clause("detected_at", since)
        sql += f" AND {frag}"
        params.append(val)'''
_COUNT_BODY = '''    where, params = _changes_where(storage, asset_type, change_type,
                                   asset_hash, since)
    with storage._conn() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM asset_changes{where}", params).fetchone()[0])'''
_BRANCH = '''    shown = len(rows)
    if shown < (args.limit or shown):
        print(f"[i] {shown} change(s):")
    else:'''
_KEPT = '''        if total > shown:
            print(f"[i] {shown} of {total} change(s) "
                  f"(只显示了最新 {shown} 条;--limit {args.limit}。"
                  f"要全看就调大 --limit,机器消费用 --json)")'''
_JSON_RET = '''            for r in rows], ensure_ascii=False, indent=2))
        return 0'''

MUTANTS = [
    # ── r55 本体:又拿显示条数当总数 ──
    ("M1", "首行回到 `len(rows)` —— 200 条里只说 50 条,零提示",
     [(CLI, _BRANCH,
       '''    shown = len(rows)
    if True:
        print(f"[i] {shown} change(s):")
    else:''')]),

    # 只换 `if` 那一行,**不动下面的 print 块** —— 首版把整个块替换成
    # `if False:`,print 留在原缩进上直接变成 IndentationError,被假杀守卫
    # 判成「变异无效」。那次不算数:一个写坏语法的变异证明不了任何事
    # (r47 的教训)。
    ("M2", "截断了也不说 —— 查到了但一个字不提",
     [(CLI, "        if total > shown:", "        if False:")]),

    ("M3", "总数只用 `len(rows)` 那个数,不算真实条数",
     [(CLI, '''        total = count_changes(storage, asset_type=args.type,
                              change_type=args.change_type,
                              asset_hash=asset_hash, since=since)''',
       "        total = shown")]),

    # ── 总数和列表漂移:报出一个看起来精确的假数字 ──
    ("M4", "count_changes 漏掉 --type 的条件 —— 报的是全库条数",
     [(MONITOR, _WHERE_ASSET_TYPE, "    # 漏了 asset_type")]),

    ("M5", "count_changes 漏掉 --change-type 的条件",
     [(MONITOR, _WHERE_CHANGE_TYPE, "    # 漏了 change_type")]),

    ("M6", "count_changes 漏掉 --since 的条件 —— 报的是全历史条数",
     [(MONITOR, _WHERE_SINCE, "    # 漏了 since")]),

    ("M7", "list_changes 漏掉 --type 的条件 —— 列表和总数一起漂",
     [(MONITOR, _WHERE_ASSET_TYPE, "    # 漏了 asset_type")]),

    # ── 总数的算法 ──
    ("M8", "count_changes 从受限列表推导 —— 它数的永远是 limit",
     [(MONITOR, _COUNT_BODY,
       "    return len(list_changes(storage, asset_type=asset_type,"
       " change_type=change_type, asset_hash=asset_hash, since=since,"
       " limit=50))")]),

    ("M9", "count_changes 写成 `len(...)` 包在 fetchone 上 —— 直接崩或返回错类型",
     [(MONITOR, _COUNT_BODY,
       '''    with storage._conn() as conn:
        return len(conn.execute(
            f"SELECT COUNT(*) FROM asset_changes{where}", params).fetchall())''')]),

    # ── 显示侧的噪音 ──
    ("M10", "没截断也喊「只显示了最新 N 条」—— 喊多了就不灵了",
     [(CLI, "    if shown < (args.limit or shown):", "    if True:")]),

    ("M11", "正好等于 limit 也说被截断",
     [(CLI, "    if shown < (args.limit or shown):",
       "    if shown <= (args.limit or shown):")]),

    # ── `--json` 那条路故意不动 ──
    ("M12", "给 `--json` 也加一句人话 —— 破坏可解析性",
     [(CLI, _JSON_RET, '''            for r in rows], ensure_ascii=False, indent=2))
        print("[i] 只显示了最新 10 条,共 200 条")
        return 0''')]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 期望**存活**:把不变式那条和「总数随过滤变」那条都去掉,
    # `count_changes` 漏掉 asset_type 就没人喊了 —— 除了
    # `test_total_is_the_count_before_the_limit_not_the_new_escape`(无过滤场景)
    # 之外。说明「过滤条件不许漂」这件事有**两道**判据,去掉两道就漏。
    ("C-filters", "skip 不变式和「总数随 --type 变」两条,同时让 count 漏掉 asset_type",
     [(TEST, "def test_count_matches_an_unbounded_list_under_every_filter(",
             "@pytest.mark.skip\ndef test_count_matches_an_unbounded_list_under_every_filter("),
      (TEST, "def test_total_respects_the_type_filter(",
             "@pytest.mark.skip\ndef test_total_respects_the_type_filter("),
      (MONITOR, _WHERE_ASSET_TYPE, "    # 漏了 asset_type")], True),

    # 期望**被杀,但死在另一道闸**:`test_both_functions_go_through_the_shared_
    # where_builder` 还在 —— 它要求两个函数都调 `_changes_where`,而这条
    # 变异让 `count_changes` 改走 `list_changes` 了。如实记着:
    # 「条件只有一份」被**三条**结构判据覆盖,不是只有那两条。
    ("C-derive", "skip 掉两条结构判据,同时把 count 改成从列表推导",
     [(TEST, "def test_count_changes_does_not_derive_from_a_limited_list(",
             "@pytest.mark.skip\ndef test_count_changes_does_not_derive_from_a_limited_list("),
      (TEST, "def test_count_changes_really_counts_in_sql(",
             "@pytest.mark.skip\ndef test_count_changes_really_counts_in_sql("),
      (MONITOR, _COUNT_BODY,
       "    return len(list_changes(storage, asset_type=asset_type,"
       " change_type=change_type, asset_hash=asset_hash, since=since,"
       " limit=10 ** 6))")], False),

    # 期望**被杀**:两个函数各拼一遍 WHERE(共用那个被废掉)——
    # 哪怕内容此刻还是一样的,`test_both_functions_go_through_the_shared_
    # where_builder` 也会红。那条守的是「条件只有一份」这个结构。
    ("C-split", "skip 掉「必须共用 WHERE」那条,同时让两个函数各拼各的",
     [(TEST, "def test_both_functions_go_through_the_shared_where_builder(",
             "@pytest.mark.skip\ndef test_both_functions_go_through_the_shared_where_builder("),
      (MONITOR, "    where, params = _changes_where(storage, asset_type, change_type,\n"
                "                                   asset_hash, since)\n"
                "    with storage._conn() as conn:\n"
                "        return int(conn.execute(\n"
                "            f\"SELECT COUNT(*) FROM asset_changes{where}\", params)"
                ".fetchone()[0])",
       "    with storage._conn() as conn:\n"
       "        return int(conn.execute(\n"
       "            \"SELECT COUNT(*) FROM asset_changes WHERE workspace_id = ?\",\n"
       "            [storage.workspace_id]).fetchone()[0])")], False),
]

TOUCHED = {MONITOR, CLI, TEST}


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
        print("── 说不说真话/两个函数会不会漂:存活 = 测试有洞 ──")
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
