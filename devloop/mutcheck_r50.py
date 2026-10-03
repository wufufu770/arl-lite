"""r50 变异测试:静默截断必须真的会红

同 r41~r49:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死 ——
变异把源文件写坏时 pytest 同样会红,但那和判据无关(r47 踩过)。

## 本轮的陷阱:helper 写得再对也可能没人调

r42 的 `CHANGE_TYPES` 就是这样:定义在、测试也覆盖,但生产零引用。
所以本轮特意有一条「`record_change_capped` 必须真的被 `_run_target` 调用」
的结构测试(数 AST 调用点 + 核对两处的 `snapshot` 参数),M9/M10 就是冲它来的。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

WATCHER = "arl_lite/core/watcher.py"
MONITOR = "arl_lite/core/monitor.py"
CLI = "arl_lite/cli.py"
TEST = "tests/test_watcher_change_truncation.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

MUTANTS = [
    ("M1", "helper 改成返回「写了几条」—— 调用方算出的缺口是负数",
     [(WATCHER, "    return len(rows) - len(keep)", "    return len(keep)")]),

    ("M2", "helper 恒返回 0 —— 截断重新变回静默",
     [(WATCHER, "    return len(rows) - len(keep)", "    return 0")]),

    ("M3", "负数 cap 不再报错 —— `rows[:-1]` 悄悄砍掉最后一条",
     [(WATCHER, '''    cap = CHANGE_RECORD_CAP if cap is None else int(cap)
    if cap < 0:
        raise ValueError(f"cap must be >= 0, got {cap}")''',
        "    cap = CHANGE_RECORD_CAP if cap is None else int(cap)")]),

    ("M4", "helper 忽略 cap,全写 —— 上限形同虚设",
     [(WATCHER, "    keep = list(rows[:cap]) if cap < len(rows) else list(rows)",
                "    keep = list(rows)")]),

    ("M5", "helper 忽略 cap,一条不写 —— 全静默丢",
     [(WATCHER, "    keep = list(rows[:cap]) if cap < len(rows) else list(rows)",
                "    keep = []")]),

    ("M6", "累计计数不再累加 —— 丢多少永远查不到",
     [(WATCHER, "    wt.dropped_change_count += dropped", "    pass")]),

    ("M7", "最近一轮的丢弃数被清零 —— `watch list` 永远显示 0",
     [(WATCHER, "    wt.last_dropped_change_count = dropped",
                "    wt.last_dropped_change_count = 0")]),

    ("M7b", "accounting 不再被接线 —— 丢多少条又没人记了(r42 陷阱)",
     [(WATCHER, """        dropped_this_run = account_change_recording(
            wt, detected_total, recorded_total)""",
        "        dropped_this_run = detected_total - recorded_total")]),

    ("M7c", "accounting 把负数原样放行 —— 一个 bug 被伪装成另一个",
     [(WATCHER, "    dropped = max(0, int(detected) - int(recorded))",
                "    dropped = int(detected) - int(recorded)")]),

    ("M8", "老状态文件的计数器直接取值 —— 字段是 null 就让 watcher 起不来",
     [(WATCHER, "        wt.dropped_change_count = _as_int(d.get(\"dropped_change_count\"))",
                "        wt.dropped_change_count = d.get(\"dropped_change_count\")")]),

    ("M9", "helper 只有一处调用 —— 消失检测又变回静默截断",
     [(WATCHER, """            dropped_gone = record_change_capped(
                self.storage, asset_type, "DISAPPEARED", gone,
                snapshot="before")""",
        """            dropped_gone = 0
            for _row in gone[:200]:
                record_change(
                    self.storage, asset_type, "DISAPPEARED",
                    asset_hash=_row.get("hash") or "",
                    before=_row, task_id=_row.get("task_id"))""")]),

    ("M10", "两处调用的 snapshot 写反 —— 单边快照语义相反且看不出来",
     [(WATCHER, """                self.storage, asset_type, "DISAPPEARED", gone,
                snapshot="before")""",
        """                self.storage, asset_type, "DISAPPEARED", gone,
                snapshot="after")""")]),

    ("M11", "`watch list` 不再显示丢弃数 —— 留痕只落在日志里",
     [(CLI, '''        dropped = t.get("dropped_change_count") or 0
        if dropped:''', '''        dropped = 0
        if dropped:''')]),

    ("M12", "上限常量被改小 —— 测试里的 golden 会跟着一起错,看不出问题",
     [(WATCHER, "CHANGE_RECORD_CAP = 200", "CHANGE_RECORD_CAP = 1000")]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 期望**被杀,但死在另一道闸**:把调用点撤掉时,变异里用的是裸切片
    # `gone[:200]`,于是先被「_run_target 里不许出现裸切片」那条 AST 测试
    # 逮住。如实记着:真正兜住的是那条,不是「helper 必须被调用」那条 ——
    # 后者验的是调用**数量和 snapshot 参数**,前者验的是形状。两者不是一回事。
    ("C-callers", "去掉「helper 必须被 _run_target 调用」那条,同时撤掉一处调用",
     [(TEST, "def test_capped_helper_is_actually_called_from_run_target():",
             "@pytest.mark.skip\ndef test_capped_helper_is_actually_called_from_run_target():"),
      (WATCHER, """            dropped_gone = record_change_capped(
                self.storage, asset_type, "DISAPPEARED", gone,
                snapshot="before")""",
        """            dropped_gone = 0
            for _row in gone[:200]:
                record_change(
                    self.storage, asset_type, "DISAPPEARED",
                    asset_hash=_row.get("hash") or "",
                    before=_row, task_id=_row.get("task_id"))""")], False),

    # 期望**存活**:去掉「丢掉是永久的」那条,同时把 `detect_changes` 的窗口
    # 下界拿掉(`first_seen >= ?` 变成恒真)。那时第 2 轮会重新检出第 1 轮
    # 丢掉的那批 ——「丢掉是永久的」这个前提就不成立,而本轮整套方案
    #(自报而不是补写)正是建立在这个前提上。
    #
    # 第一版这里写的是把常量改成「自己 + 一个注释」,那是**空操作变异**:
    # 它存活不证明任何事,只说明我写了个等于没写的变异。空操作必须换掉。
    ("C-permanent", "去掉「丢掉是永久的」那条,同时拿掉检测窗口的下界",
     [(TEST, "def test_dropped_assets_are_never_reported_in_later_runs(",
             "@pytest.mark.skip\ndef test_dropped_assets_are_never_reported_in_later_runs("),
      (MONITOR, "WHERE workspace_id = ? AND first_seen >= ?",
                "WHERE workspace_id = ? AND ? IS NOT NULL")], True),
]

TOUCHED = {WATCHER, CLI, MONITOR, TEST}


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
        print("── 改截断逻辑/留痕:存活 = 测试有洞 ──")
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
