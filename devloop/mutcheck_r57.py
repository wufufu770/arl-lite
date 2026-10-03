"""r57 变异测试:「导出静默丢数据」这个洞,判据真的会红吗

同 r41~r56:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮最要紧的是 M1:退出码

丢数据却退出 0,`export` 在 CI 和定时任务里就是一个**说谎的绿灯**。
所以 M1 只改返回码,输出一个字都不改 —— 专门验「退出码这条判据自己
站得住」,不被那句警告带着一起变红。

## M12 值得单独说

`fetch_all` 返回 `(rows, total)`,而 r57 之前只有 `rows`。
如果有人图省事只解包第一个数(`rows, _ = storage.fetch_all(...)`),
数据照样丢、警告照样没有 —— 而「有多少行」这件事**根本没被问过**。
判据 C-drop-total 问的就是这个:把两处解包都改成丢掉 total,
行为判据会不会漏掉它。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

STORAGE = "arl_lite/db/storage.py"
CLI = "arl_lite/cli.py"
HTML = "arl_lite/cli_report_html.py"
TEST = "tests/test_export_row_loss.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

_FETCH_ALL = '''        n = EXPORT_ROW_CAP if cap is None else int(cap)
        if n < 0:
            raise ValueError(f"cap must be >= 0, got {cap!r}")
        return self.query(table, limit=n), self.count_rows(table)'''
_EXPORT_LOOP = '''    for table in _EXPORT_TABLES:
        rows, total = storage.fetch_all(table)
        data[table] = rows
        totals[table] = total'''
_CLIPPED = '''    clipped = {t: (totals[t], len(data[t])) for t in _EXPORT_TABLES
               if totals[t] > len(data[t])}'''
_CLIP_WARN = '''    if clipped:
        print(f"[!] 导出的数据**不完整** —— 以下表超过了 "
              f"{EXPORT_ROW_CAP} 行上限,只取到了前 {EXPORT_ROW_CAP} 行:",
              file=sys.stderr)'''
_RETURN_1 = '''        print(f"[!] 文件已写出,但**不要**当成完整数据集用。要导全就分表分批:"
              f"arl-lite query <table> --limit 10000(逐段导出),"
              f"或调高 db.storage.EXPORT_ROW_CAP。", file=sys.stderr)
        return 1
    return 0'''
_HTML_CLIP = '''    _clipped = [(t, tot, got) for t, tot, got in _clipped if tot > got]
    if _clipped:'''
_HTML_FETCH_HOSTS = '    hosts, _hosts_total = storage.fetch_all("hosts")'

MUTANTS = [
    # ── r57 本体 ──
    ("M1", "丢数据却退出 0 —— CI 里的绿灯谎言",
     [(CLI, _RETURN_1, _RETURN_1.replace("        return 1\n    return 0",
                                         "        return 0\n    return 0"))]),

    ("M2", "只报第一张超限的表 —— 第二张又静默了",
     [(CLI, _CLIPPED, '''    _c = {t: (totals[t], len(data[t])) for t in _EXPORT_TABLES
             if totals[t] > len(data[t])}
    clipped = dict(list(_c.items())[:1])''')]),

    ("M3", "截断检测整段删掉 —— 回到「静默丢 + 报成功」",
     [(CLI, _CLIP_WARN, "    if False:")]),

    ("M4", "用 `==` 而不是 `>` —— 正好超一条就当没超",
     [(CLI, "               if totals[t] > len(data[t])}",
       "               if totals[t] == len(data[t])}")]),

    ("M5", "警告不指名表名 —— 用户不知道该去看哪张",
     [(CLI, '''        for table in sorted(clipped):
            total, got = clipped[table]
            print(f"      {table}: 库里 {total} 行,只导出 {got} 行"
                  f"(少了 {total - got} 行)", file=sys.stderr)''',
       '''        for _t, (total, got) in sorted(clipped.items()):
            print(f"      有表超限了:库里 {total} 行,只导出 {got} 行", file=sys.stderr)''')]),

    ("M6", "警告不说少了多少行 —— 只说「不完整」等于没说",
     [(CLI, '''            print(f"      {table}: 库里 {total} 行,只导出 {got} 行"
                  f"(少了 {total - got} 行)", file=sys.stderr)''',
       '''            print(f"      {table}: 不完整", file=sys.stderr)''')]),

    # ── 数据层:两个数绑在一起这件事本身 ──
    ("M7", "`fetch_all` 的 total 拿 `len(rows)` 顶 —— 恒等于显示数,警告永远打不出",
     [(STORAGE, _FETCH_ALL, '''        n = EXPORT_ROW_CAP if cap is None else int(cap)
        if n < 0:
            raise ValueError(f"cap must be >= 0, got {cap!r}")
        _rows = self.query(table, limit=n)
        return _rows, len(_rows)''')]),

    ("M8", "`fetch_all` 忽略 cap —— 全表取出来,上限形同虚设",
     [(STORAGE, "        return self.query(table, limit=n), self.count_rows(table)",
       "        return self.query(table, limit=10 ** 9), self.count_rows(table)")]),

    ("M9", "`cmd_export` 退回裸的 `query(limit=10000)` —— total 没了,警告无从谈起",
     [(CLI, _EXPORT_LOOP,
       '''    for table in _EXPORT_TABLES:
        data[table] = storage.query(table, limit=10000)
        totals[table] = len(data[table])''')]),

    # ── HTML 报告:同一个病,四处 ──
    ("M10", "HTML 报告不显示警告块 —— 报告照样生成,数据照样不全",
     [(HTML, _HTML_CLIP, '''    _clipped = [(t, tot, got) for t, tot, got in _clipped if tot > got]
    if False:''')]),

    ("M11", "HTML 报告又退回裸的 `query(limit=10000)`",
     [(HTML, _HTML_FETCH_HOSTS,
       '    hosts = storage.query("hosts", limit=10000)\n'
       '    _hosts_total = len(hosts)')]),

    # ── 「没超上限时别啰嗦」 ──
    ("M12", "没超上限也喊不完整 —— 喊多了就不灵了",
     [(CLI, _CLIP_WARN, "    if True:")]),

    ("M13", "HTML 没超上限也显示警告块",
     [(HTML, "    _clipped = [(t, tot, got) for t, tot, got in _clipped if tot > got]",
       "    _clipped = list(_clipped)")]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 期望**存活**:只 skip 那条「必须同时有警告和退出码」的判据,
    # 然后把警告和退出码都去掉 —— 剩下的判据(逐张表报少行数、
    # stderr 那条、fetch_all 返回值那条)会红吗?
    # 实测:会红(它们要求 stdout/stderr 里出现具体数字)。
    # 这条留在这儿是为了如实记着:退出码那条判据**不是唯一**防线。
    ("C-exitcode", "skip 主判据,同时把退出码改回 0",
     [(TEST, "def test_truncated_export_says_so_and_exits_nonzero(",
             "@pytest.mark.skip\ndef test_truncated_export_says_so_and_exits_nonzero("),
      (CLI, _RETURN_1, _RETURN_1.replace("        return 1\n    return 0",
                                         "        return 0\n    return 0"))], False),

    # 首版这里**期望存活**,实测被杀 —— 我以为「所有关于 fetch_all 返回
    # 什么的判据都会绿」,查实漏了一条:`test_every_clipped_table_is_named`
    # 直接断言输出里出现「domains: 库里 12000 行,只导出 10000 行」,
    # 而 `totals[table] = len(rows)` 让 `totals[t] > len(data[t])` 恒假,
    # 于是报告根本不会打,那条立刻红。
    #
    # 也就是说「把 total 丢掉」不是没人管,而是**逐张表报少行数**那条
    # 天然兜住了 —— 因为少行数只有在两个数不同的时候才存在。
    # 首版那两条 HTML 的 edit 还写重复了(new 和 old 相同,等于没改),
    # 一并删掉:没改的变异不是变异。
    ("C-drop-total", "把 cmd_export 的解包改成丢掉 total",
     [(CLI, _EXPORT_LOOP,
       '''    for table in _EXPORT_TABLES:
        rows, _ignored = storage.fetch_all(table)
        data[table] = rows
        totals[table] = len(rows)''')], False),

    # 期望**存活**:skip 掉 HTML 那两条判据,HTML 侧退回静默 ——
    # 导出那条路(JSON/CSV)仍然是对的,两条线互不兜底。
    ("C-html", "skip 掉 HTML 两条判据,同时 HTML 侧退回裸 query",
     [(TEST, "def test_html_report_warns_when_a_table_was_clipped(",
             "@pytest.mark.skip\ndef test_html_report_warns_when_a_table_was_clipped("),
      (TEST, "def test_html_report_has_no_bare_limited_query_left(",
             "@pytest.mark.skip\ndef test_html_report_has_no_bare_limited_query_left("),
      (HTML, _HTML_FETCH_HOSTS,
       '    hosts = storage.query("hosts", limit=10000)\n'
       '    _hosts_total = len(hosts)\n'
       '    _clipped = []')], True),
]

TOUCHED = {STORAGE, CLI, HTML, TEST}


def run_tests() -> tuple[bool, bool]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST, "-q", "-p", "no:warnings"],
        capture_output=True, text=True, env=env)
    broken = any(s in p.stdout for s in _BROKEN_SOURCE)
    return p.returncode == 0, broken


def apply(edits) -> bool:
    done = []
    for path, old, new in edits:
        s = open(path, encoding="utf-8").read()
        if old not in s:
            for p, _, _ in done:
                open(p, "w", encoding="utf-8").write(
                    open(p + ".mutbak", encoding="utf-8").read())
            print(f"[!!] 变异没打上({path}: 原串不匹配) —— 变异本身失效了")
            return None
        open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
        done.append((path, old, new))
    return done


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
        if apply(edits) is None:
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
        print("── 说不说/退不退出/两个数绑没绑:存活 = 测试有洞 ──")
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
