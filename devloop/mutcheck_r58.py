"""r58 变异测试:「截断要说真话 + 通知不许看起来像全推了」,判据真的会红吗

同 r41~r57:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮的重心在两处「说谎」

r58 的 r59 之前有三个说谎的点,全在这一轮:
`matched_total` 取在截断之后(拿到的永远是 len(hits),提示打不出来)、
`_notify_correlations` 少收那个总数(汇总行退回 "notified 50/50")、
以及共用文案照抄「最新 N 条」和 `--json` 到 `correlate` 上
(它是按 risk 降序、且**根本没有** --json)。

M1~M3 各钉一个,M8/M9 钉文案泄漏。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

CLI = "arl_lite/cli.py"
TEST = "tests/test_correlate_limit_notice.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

_TOTAL_BEFORE = '''    matched_total = len(hits)
    hits = hits[:args.limit]'''
# 首版这个 new 里括号打错了(`[:args.limit)`),源文件直接 IndentationError,
# 被假杀守卫判成「变异无效」—— 写坏语法的变异证明不了任何事(r47 的教训)。
_TOTAL_AFTER = '''    hits = hits[:args.limit]
    matched_total = len(hits)'''
_CALL_NOTIFY = "    notified = _notify_correlations(args, hits, matched_total)"
_NOTIFY_SIG = '''def _notify_correlations(args, hits, total: int | None = None) -> str | None:'''
_NOTIFY_LINE = '''    line = f"[i] notified {ok}/{len(hits)} correlation(s)"
    if total is not None and total > len(hits):
        line += (f" —— 共命中 {total} 条,按 --limit 只推了前 {len(hits)} 条,"
                 f"其余 {total - len(hits)} 条**没有推**"
                 f"(它们在命令输出和 correlations 表里)")
    if failed:
        line += f" ({failed} failed)"
    return line'''
_SHOW = '''    if total > shown:
        # 不说「最新 N 条」:那是 `ORDER BY id DESC` 的措辞,而 `correlate`
        # 是按 risk 降序取的(高风险在前)—— 共用文案照抄「最新」就是在
        # 骗人。两条命令都是降序取前 N,去掉它不损失任何信息。
        advice = how or "要全看就调大 --limit,机器消费用 --json"
        return (f"[i] {shown} of {total} {noun}(s) "
                f"(只显示了 {shown} 条;--limit {limit}。{advice})")'''
_NOTICE_CALL = '''    _limit_notice = _limit_notice_text(len(hits), matched_total, args.limit,
                                       "correlation",
                                       how="要全看就调大 --limit(命中全都在 "
                                           "correlations 表里,也能用 "
                                           "arl-lite query correlations 看)")'''

MUTANTS = [
    # ── 三个说谎的点 ──
    ("M1", "`matched_total` 取在截断之后 —— 拿到的永远是 len(hits),提示打不出来",
     [(CLI, _TOTAL_BEFORE, _TOTAL_AFTER)]),

    ("M2", "_notify_correlations 不收 matched_total —— 汇总行退回「notified 50/50」",
     [(CLI, _CALL_NOTIFY, "    notified = _notify_correlations(args, hits)"),
      (CLI, _NOTIFY_SIG,
       "def _notify_correlations(args, hits) -> str | None:")]),

    ("M3", "汇总行只留 `notified {ok}/{len(hits)}` —— 60 条推 50 条却说「50/50」",
     [(CLI, _NOTIFY_LINE,
       '''    line = f"[i] notified {ok}/{len(hits)} correlation(s)"
    if failed:
        line += f" ({failed} failed)"
    return line''')]),

    # ── 提示本身 ──
    ("M4", "整个截断提示不打了 —— 回到 r58 之前的沉默",
     [(CLI, _NOTICE_CALL, "    _limit_notice = \"\"")]),

    ("M5", "提示不指名量词 —— 「50 of 60」不知道是什么的 50 和 60",
     [(CLI, '''                                       "correlation",
                                       how="要全看就调大 --limit(命中全都在 "
                                           "correlations 表里,也能用 "
                                           "arl-lite query correlations 看)")''',
       "                                       \"hit\")")]),

    ("M6", "提示说「最新 N 条」—— correlate 是按 risk 降序的,不是按时间",
     [(CLI, 'f"(只显示了 {shown} 条;--limit {limit}。{advice})")',
       'f"(只显示了最新 {shown} 条;--limit {limit}。{advice})")')]),

    ("M8", "correlate 不传自己的 `how` —— 于是推荐了不存在的 --json",
     [(CLI, _NOTICE_CALL, '''    _limit_notice = _limit_notice_text(len(hits), matched_total, args.limit,
                                       "correlation")''')]),

    # ── 「没截断时别啰嗦」 ──
    ("M9", "没截断也喊「of N」—— 喊多了就不灵了",
     [(CLI, "    if total > shown:", "    if total >= shown:")]),

    # ── 基准搞错:把用户自己的筛选说成工具截断 ──
    ("M10", "拿引擎命中总数当基准 —— min_risk 过滤被算成「被截断了」",
     [(CLI, "    matched_total = len(hits)",
       "    matched_total = _ENGINE_HIT_COUNT")]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 首版这里**期望存活**,理由是「在 60 条命中的场景下,引擎总数和
    # 过了 min_risk 的数相同,行为判据分不出来」。查实是错的:
    # `test_min_risk_filter_is_not_reported_as_truncation` 用
    # `--min-risk 10` 把 60 条全滤掉,此时引擎总数 60 ≠ 过滤后 0,
    # 于是「of 60」出现,那条判据立刻红。
    # 也就是说基准对不对**行为是测得出来的** —— 结构判据是加固,不是唯一防线。
    ("C-baseline", "skip 结构判据,让基准退回引擎命中总数",
     [(TEST, "def test_the_limit_slice_lives_in_one_place_with_the_total_already_known(",
             "@pytest.mark.skip\ndef test_the_limit_slice_lives_in_one_place_with_the_total_already_known("),
      (CLI, "    matched_total = len(hits)",
       "    matched_total = 60")], False),

    # 首版这里**期望存活**(我以为只有那两条判据在守文案),实测被杀:
    # `test_correlate_advice_mentions_where_the_full_set_lives` 还在,
    # 它断言 `--json` 不许出现在 correlate 的输出里 —— 于是泄漏照旧被抓住。
    # 兜住它的是**第三条**判据,不是我以为的那两条。如实记着是谁挡的。
    ("C-wording", "skip 掉两条文案判据,同时把「最新」和 --json 加回去",
     [(TEST, "def test_notice_does_not_claim_the_rows_are_the_newest(",
             "@pytest.mark.skip\ndef test_notice_does_not_claim_the_rows_are_the_newest("),
      (TEST, "def test_notice_must_not_advertise_a_json_flag_a_command_does_not_have(",
             "@pytest.mark.skip\ndef test_notice_must_not_advertise_a_json_flag_a_command_does_not_have("),
      (CLI, 'f"(只显示了 {shown} 条;--limit {limit}。{advice})")',
       'f"(只显示了最新 {shown} 条;--limit {limit}。{advice})")'),
      (CLI, _NOTICE_CALL, '''    _limit_notice = _limit_notice_text(len(hits), matched_total, args.limit,
                                       "correlation")''')], False),
]

TOUCHED = {CLI, TEST}


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
        print("── 说不说/说没说全/文案对不对:存活 = 测试有洞 ──")
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
