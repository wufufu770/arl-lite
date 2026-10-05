"""r56 变异测试:「limit 截断要说真话」这个机制,三条命令会不会各自漂

同 r41~r55:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮的重心:不是「有没有提示」,是「会不会分叉」

r55 已经在 `monitor changes` 上手写了一份提示。r56 给 `query` /
`search` 也加上,于是那份手写必须收掉 —— 否则项目里就有两段各写一遍的
截断提示,改一边忘另一边,两个命令报的话对不上(决策 #9)。

所以 M1/M2/M12 都是「某条命令绕过共用机制」的形状;而 M3/M4/M5
盯的是「总数和列表不同源」—— 那会报出一个看起来精确的假数字,
比静默截断更坏(r55 同一个教训)。

## M6 值得单独说

`count_search` 在 FTS5 出错时返回 `None` 而不是 `0`。改成 `0` 看着
只是个返回值选择,实际是把一次查询失败**伪装成「零条命中」**。
判据两条:`count_search` 必须返回 None,以及 CLI 遇到 None 必须说
UNKNOWN 而不是印一个数。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

STORAGE = "arl_lite/db/storage.py"
CLI = "arl_lite/cli.py"
TEST = "tests/test_cli_limit_honesty.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

_WHERE_FILTER = '''        sql = " WHERE workspace_id = ?"
        if filter_sql:
            check_filter_sql(filter_sql)
            sql += f" AND ({filter_sql})"
        return sql'''
_COUNT_ROWS = '''        ws = workspace_id or self.workspace_id
        where = self._query_where(table, ws, filter_sql)
        with self._conn() as conn:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM {table}{where}", [ws]).fetchone()[0])'''
_COUNT_SEARCH = '''        frag, _, params = self._search_where(table, keyword)
        if not frag:                      # 空关键词:`search` 返回 [],这里也是 0
            return 0
        with self._conn() as conn:
            try:
                return int(conn.execute(
                    f"SELECT COUNT(*) {frag}", params).fetchone()[0])
            except Exception as e:
                # 不 raise:统计是旁路,不该让整条命令挂掉。但也不能假装数到了。
                log.warning(f"FTS5 count failed for keyword {keyword!r}: {e}")
                return None'''
_NEEDS = '''def _needs_total(shown: int, limit: int) -> bool:'''
_NEEDS_RET = "    return shown >= limit"
_NOTICE_TOTAL = '''    if total > shown:
        return (f"[i] {shown} of {total} {noun}(s) (只显示了最新 {shown} 条;"
                f"--limit {limit}。要全看就调大 --limit,机器消费用 --json)")'''
_JSON_QUERY = '''    if args.format == "json":
        _print_json(rows)
    else:
        # `count_rows` 走 `_query_where`,和 `query` 同一份条件 ——
        # `--filter` 是**用户传的** SQL,所以共用尤其要紧:总数必须是
        # 同一个筛选下的条数,否则报出的是个看起来精确的假数字(r56)。
        total = (storage.count_rows(args.table, filter_sql=args.filter)
                 if _needs_total(len(rows), args.limit) else len(rows))
        _print_table_limited(rows, total, args.limit)'''
_JSON_SEARCH = '''    if args.format == "json":
        _print_json(rows)
    else:
        # 只有行数顶到 limit 时才去数总数 —— 没顶到显然没截断(r56)
        total = (storage.count_search(args.table, args.keyword)
                 if _needs_total(len(rows), args.limit) else len(rows))
        _print_table_limited(rows, total, args.limit)'''

MUTANTS = [
    # ── 某条命令绕过共用机制 ──
    ("M1", "cmd_query 永远不算总数 —— 回到「一个提示都不给」",
     [(CLI, _JSON_QUERY, '''    if args.format == "json":
        _print_json(rows)
    else:
        _print_table(rows)''')]),

    ("M2", "cmd_search 永远不算总数 —— 同上",
     [(CLI, _JSON_SEARCH, '''    if args.format == "json":
        _print_json(rows)
    else:
        _print_table(rows)''')]),

    ("M3", "cmd_monitor_changes 不走共用文案 —— 恢复 r55 那份手写",
     [(CLI, '    notice = _limit_notice_text(len(rows), total, args.limit, "change")\n'
            '    if notice:\n        print(notice)',
       '    if total > shown_:\n'
       '        print(f"[i] {shown_} of {total} change(s) (只显示了最新 {shown_} 条)")\n'
       '    else:\n'
       '        print(f"[i] {total} change(s)")')]),

    # ── 总数和列表不同源:报出一个看起来精确的假数字 ──
    ("M4", "`_query_where` 漏掉用户传的 filter —— count 与 query 看的不是同一批行",
     [(STORAGE, _WHERE_FILTER, '        sql = " WHERE workspace_id = ?"\n        return sql')]),

    ("M5", "`count_rows` 不带任何条件 —— 报的是全表行数",
     [(STORAGE, _COUNT_ROWS,
       '''        with self._conn() as conn:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM {table}", []).fetchone()[0])''')]),

    ("M6", "`count_search` 失败时返回 0 —— 把查询失败伪装成零条命中",
     [(STORAGE, _COUNT_SEARCH,
       '''        frag, _, params = self._search_where(table, keyword)
        if not frag:
            return 0
        with self._conn() as conn:
            try:
                return int(conn.execute(
                    f"SELECT COUNT(*) {frag}", params).fetchone()[0])
            except Exception:
                return 0''')]),

    ("M7", "`count_search` 从 `search` 推导 —— 它数的是受限列表",
     [(STORAGE, _COUNT_SEARCH,
       "        return len(self.search(table, keyword, limit=50))")]),

    # ── 判定与文案的边角 ──
    ("M8", "`_needs_total` 用 `>` 而不是 `>=` —— 正好顶到 limit 时不去数",
     [(CLI, _NEEDS_RET, "    return shown > limit")]),

    # new 里必须**连 return 一起换**:只把 `if total > shown:` 换成
    # `if total >= shown:`,原来的多行 return 被一起吃掉,剩下的
    # `if` 后面直接跟注释和一个同缩进的 return —— IndentationError。
    # 首版就是这么写的,被假杀守卫挡下:写坏语法的变异证明不了任何事。
    ("M9", "没截断时也喊「of」—— 喊多了就不灵了",
     [(CLI, _NOTICE_TOTAL, '''    if total >= shown:
        return (f"[i] {shown} of {total} {noun}(s) (只显示了最新 {shown} 条;"
                f"--limit {limit}。要全看就调大 --limit,机器消费用 --json)")''')]),

    ("M10", "总数未知时硬编一个数 —— 宁可报假数也不说不知道",
     [(CLI, '''    if total is None:
        return (f"[i] {shown} {noun}(s) shown; total UNKNOWN "
                f"(计数查询失败,不猜 —— 见上面的 warning)")''',
       '''    if total is None:
        total = 0
        return f"[i] {shown} of {total} {noun}(s)"''')]),

    # ── `--json` 那条路故意不动 ──
    # old 串必须对得上**当前**实现:r56 之前 cmd_query 走的是裸
    # `_print_table`,现在走 `_print_table_limited`。首版照抄了旧形态,
    # 原串一个都匹配不上,于是报「变异没打上」—— 变异本身失效了,
    # 这种红和假杀一样证明不了任何事。
    ("M11", "给 `--json` 也算总数并加提示 —— 破坏可解析性",
     [(CLI, _JSON_QUERY, '''    total = storage.count_rows(args.table, filter_sql=args.filter)
    if args.format == "json":
        _print_json(rows)
        print(f"[i] {len(rows)} of {total} row(s)")
    else:
        _print_table_limited(rows, total, args.limit)''')]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 期望**存活**:两条「机制共用」判据都不在时,恢复 r55 的手写没人喊。
    # 而行为判据全绿 —— 因为手写的提示和共用函数**此刻**说的同一句话。
    # 这说明「形状统一」这件事只有那两条结构判据在守,行为测不出来。
    ("C-shape", "skip 掉两条机制共用判据,同时恢复 r55 的手写文案",
     [(TEST, "def test_limit_notice_and_need_are_shared_by_all_three_commands(",
             "@pytest.mark.skip\ndef test_limit_notice_and_need_are_shared_by_all_three_commands("),
      (TEST, "def test_monitor_changes_no_longer_handwrites_its_own_notice(",
             "@pytest.mark.skip\ndef test_monitor_changes_no_longer_handwrites_its_own_notice("),
      (CLI, '    notice = _limit_notice_text(len(rows), total, args.limit, "change")\n'
            '    if notice:\n        print(notice)',
       '    if total > len(rows):\n'
       '        print(f"[i] {len(rows)} of {total} change(s) (只显示了最新 {len(rows)} 条)")\n'
       '    else:\n'
       '        print(f"[i] {total} change(s)")')], True),

    # 期望**存活**:把「不变式」和「filter 后的总数」两条都去掉,
    # `_query_where` 漏掉 filter 就没人喊 —— 正是 M4 那个变异。
    # 如实记着:「总数必须带同一个 filter」被**两条**判据覆盖。
    ("C-filter", "skip 不变式和 filter 那条,同时让 _query_where 漏掉 filter",
     [(TEST, "def test_count_rows_equals_an_unbounded_query_under_every_filter(",
             "@pytest.mark.skip\ndef test_count_rows_equals_an_unbounded_query_under_every_filter("),
      (TEST, "def test_query_total_respects_the_user_supplied_filter(",
             "@pytest.mark.skip\ndef test_query_total_respects_the_user_supplied_filter("),
      (STORAGE, _WHERE_FILTER, '        sql = " WHERE workspace_id = ?"\n        return sql')], True),

    # 期望**存活 —— 而且这是设计如此,不是判据的洞**。
    # `test_unknown_total_is_stated_not_guessed` 把 `Storage.count_search`
    # 整个 patch 成返回 None,所以它测的是「CLI 拿到 None 会说 UNKNOWN」,
    # **不是**「storage 失败时返回 None」。这两件事不互相兜底:
    # 本条把 storage 那条判据 skip 掉、再把实现改回返回 0,CLI 那条照样绿
    # —— 因为它根本不看 storage 的实现。首版我以为它能兜住,
    # 实测存活。如实记着,免得下一轮误以为「被另一道闸兜住了」。
    ("C-unknown", "skip 掉 count_search 返回值那条,同时让它失败时返回 0",
     [(TEST, "def test_count_search_returns_none_not_zero_when_counting_fails(",
             "@pytest.mark.skip\ndef test_count_search_returns_none_not_zero_when_counting_fails("),
      (STORAGE, _COUNT_SEARCH,
       '''        frag, _, params = self._search_where(table, keyword)
        if not frag:
            return 0
        with self._conn() as conn:
            try:
                return int(conn.execute(
                    f"SELECT COUNT(*) {frag}", params).fetchone()[0])
            except Exception:
                return 0''')], True),
]

TOUCHED = {STORAGE, CLI, TEST}


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
        print("── 有没有提示/会不会漂/会不会说假数:存活 = 测试有洞 ──")
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
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
