"""r59 变异测试:「提示里那条路真能走吗」,判据真的会红吗

同 r41~r58:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮有一个变异是 r59 自己引入的真 bug

r59 第一版的 `fetch_all` 分页时**漏了 `offset`**,于是每页都取回同样的
前 N 行:20500 行分三页取,只拿到 10000 个不同的 id、10500 条重复。
它不是假设 —— 判据当场把它逮住了(`test_paging_keeps_every_row_exactly_
once_in_order` 报「有重复行:10500 条」)。

所以 M1 把它放回去:**这个变异必须被杀死**,而且是本轮最要紧的一条 ——
分页取数最典型的错就是「看起来有循环,其实每次拿同一批」。

## M5 守的是另一半:护栏不许被拆

r59 修的是 `fetch_all` 的**取法**(分页),不是 `query` 的护栏。
为了 export 方便把公共护栏拆掉,是拿别的场景的安全换一个场景的方便。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

STORAGE = "arl_lite/db/storage.py"
CLI = "arl_lite/cli.py"
TEST = "tests/test_export_limit_advice.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

_PAGE_LOOP = '''            page = self.query(table, limit=min(_QUERY_PAGE_LIMIT,
                                                want - len(rows)),
                              offset=len(rows))'''
_PAGE_NO_OFFSET = '''            page = self.query(table, limit=min(_QUERY_PAGE_LIMIT,
                                                want - len(rows)))'''
_OFFSET_CHECK = '''        if not isinstance(offset, int) or offset < 0:
            raise ValueError(f"offset must be int >= 0, got {offset!r}")'''
_SQL = '''        sql = (f"SELECT * FROM {table}{where} "
               f"ORDER BY id DESC LIMIT {int(limit)} OFFSET {int(offset)}")'''
_SQL_NO_OFFSET = '''        sql = f"SELECT * FROM {table}{where} ORDER BY id DESC LIMIT {int(limit)}"'''
_GUARD = '''        if not isinstance(limit, int) or limit < 0 or limit > _QUERY_PAGE_LIMIT:
            raise ValueError(
                f"limit must be int 0..{_QUERY_PAGE_LIMIT}, got {limit!r}")'''
_ADVICE = '''        print(f"[!] 文件已写出,但**不要**当成完整数据集用。两种拿全量的办法:"
              f"(1) 分表分批 —— arl-lite query {sorted(clipped)[0]} --limit 10000 "
              f"逐段导出;(2) 把 db.storage.EXPORT_ROW_CAP 调到够大,再导一次 "
              f"(fetch_all 会分页取,内存占用不会跟着涨)。", file=sys.stderr)'''
_FETCH_HEAD = '''        total = self.count_rows(table)
        want = min(n, total)'''

MUTANTS = [
    # ── r59 本人引入的 bug,本轮最要紧 ──
    ("M1", "分页漏掉 offset —— 每页取回同样一批(20500 行只拿到 10000 个不同 id)",
     [(STORAGE, _PAGE_LOOP, _PAGE_NO_OFFSET)]),

    ("M2", "SQL 里根本不写 OFFSET —— offset 参数被收下然后忽略",
     [(STORAGE, _SQL, _SQL_NO_OFFSET)]),

    # ── 分页的其他典型错 ──
    ("M3", "分页循环只跑一页 —— 大表就静默少拿",
     [(STORAGE, '''        while len(rows) < want:''',
       '''        while len(rows) < want and len(rows) < _QUERY_PAGE_LIMIT:''')]),

    ("M4", "分页用 `>` 而不是 `<` —— 第一页就退出",
     [(STORAGE, "        while len(rows) < want:",
       "        while len(rows) > want:")]),

    ("M5", "没取完也不说 —— 循环里 `break` 掉,`total` 说了 20500 而实际给了 10000",
     [(STORAGE, '''            if not page:            # 期间被并发删了:少拿的就少拿,但 total 已经说了真相
                break''',
       '''            if not page:
                return rows, len(rows)      # ← 把 total 改成实际拿到的数''')]),

    # ── 护栏不许被拆 ──
    ("M6", "为了 export 方便把 query 的护栏拆了 —— 别的场景失去保护",
     [(STORAGE, _GUARD, '''        if not isinstance(limit, int) or limit < 0:
            raise ValueError(f"limit must be int >= 0, got {limit!r}")''')]),

    ("M7", "`query` 里又写回魔法数字 —— 护栏和 EXPORT_ROW_CAP 变成两个来源",
     [(STORAGE, _GUARD, '''        if not isinstance(limit, int) or limit < 0 or limit > 10000:
            raise ValueError(f"limit must be int 0..10000, got {limit!r}")''')]),

    ("M8", "offset 负数不报错 —— 负 OFFSET 在 SQLite 里从头数,静默给错数据",
     [(STORAGE, _OFFSET_CHECK, "    # 不校验 offset")]),

    # ── 文案与实现脱节 ──
    ("M9", "建议里又出现 `<table>` 占位符 —— 照抄会报错",
     [(CLI, _ADVICE,
       '''        print(f"[!] 文件已写出,但**不要**当成完整数据集用。分表分批:"
              f"arl-lite query <table> --limit 10000。", file=sys.stderr)''')]),

    ("M10", "建议里不再提 EXPORT_ROW_CAP —— 那条路真通了,藏起来没道理",
     [(CLI, _ADVICE,
       '''        print(f"[!] 文件已写出,但**不要**当成完整数据集用。", file=sys.stderr)''')]),

    # 期望**存活**,而且这是**对**的:`want = n` 时表比 cap 小会怎样?
    # `query(limit=…, offset=7)` 返回 0 行 → `if not page: break` → 退出,
    # 结果完全正确。多跑一次空查询的代价,换来的是「意图显式」。
    # 首版把它当成该杀的洞,实测存活后查实:那处 `break` 就是兜底。
    ("M11", "want 忘了和 total 取小(期望存活:空页的 break 兜住了)",
     [(STORAGE, "        want = min(n, total)", "        want = n")], True),

    ("M12", "total 换成 len(rows) —— 截断又变静默了(r57 的 bug 原样回来)",
     [(STORAGE, _FETCH_HEAD,
       '''        total = self.count_rows(table)
        want = min(n, total)
        return self.query(table, limit=n, offset=0), len(want)''')]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 首版这里**期望存活**(我以为只有那三条可执行性判据在守),
    # 实测被杀,兜住它的是 `test_paging_keeps_every_row_exactly_once_in_order`:
    # 它断言 `len(rows) == total`,而改回单次 query 后 20500 行只取回 10000,
    # 条数立刻对不上。兜住它的不是被我 skip 的那三条,如实记着是谁挡的。
    ("C-executable", "skip 掉两条可执行性判据,同时把分页改回单次 query",
     [(TEST, "def test_raising_the_cap_actually_lets_you_export_everything(",
             "@pytest.mark.skip\ndef test_raising_the_cap_actually_lets_you_export_everything("),
      (TEST, "def test_the_raised_cap_claim_is_not_a_lie(",
             "@pytest.mark.skip\ndef test_the_raised_cap_claim_is_not_a_lie("),
      (TEST, "def test_raising_the_cap_does_not_crash_the_export(",
             "@pytest.mark.skip\ndef test_raising_the_cap_does_not_crash_the_export("),
      (STORAGE, _PAGE_LOOP + '''
            if not page:            # 期间被并发删了:少拿的就少拿,但 total 已经说了真相
                break
            rows.extend(page)
        return rows, total''',
       '''            page = self.query(table, limit=n)
            rows.extend(page)
        return rows, total''')], False),

    # 首版这里**期望存活**(我以为「有没有重复、顺序对不对」是这一族 bug
    # 的唯一防线),实测被杀,兜住它的是
    # `test_raising_the_cap_actually_lets_you_export_everything`:cap 调大后
    # 它要求 `len == EXPORT_ROW_CAP + 2000`,而没有 offset 就只能拿回一页。
    # 也就是说条数断言比「无重复」这条更早、也更粗地抓住了这个错 ——
    # 分页坏了通常先表现为**条数不对**,重复只是它的一个表征。
    ("C-dupes", "skip 掉「无重复且有序」那条,同时把 offset 拿掉",
     [(TEST, "def test_paging_keeps_every_row_exactly_once_in_order(",
             "@pytest.mark.skip\ndef test_paging_keeps_every_row_exactly_once_in_order("),
      (STORAGE, _PAGE_LOOP, _PAGE_NO_OFFSET),
      (STORAGE, _SQL, _SQL_NO_OFFSET)], False),
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
        print("── 分页取数/护栏/文案可执行性:存活 = 测试有洞 ──")
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
    raise SystemExit(mutkit.locked(main))
