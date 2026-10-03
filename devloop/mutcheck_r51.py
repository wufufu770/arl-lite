"""r51 变异测试:删的方向、时间格式、默认不删,都要真的红

同 r41~r50:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮的特有陷阱:方向反了看起来「完全成功」

r51 第一版 `prune_changes` 用了 `>=`(取较新的),而它要删的是较旧的。
实测 `monitor prune --yes` 删掉的是**最新**那条、旧的原封不动 ——
命令不报错、退出码 0、行数确实少了。所以 M1/M2 那两个变异是本轮最要紧的:
它们要把「删错方向」重新塞回去,而判据必须当场抓住。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

MONITOR = "arl_lite/core/monitor.py"
CLI = "arl_lite/cli.py"
TEST = "tests/test_change_retention.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

MUTANTS = [
    # ── 方向:本轮的头号风险 ──
    ("M1", "prune 用 `>=` —— 删掉的是最新的,旧的原封不动(第一版的真 bug)",
     [(MONITOR, '''    frag, val = _older_than_clause("detected_at", older_than)
    with storage._conn() as conn:''',
        '''    frag, val = _since_clause("detected_at", older_than)
    with storage._conn() as conn:''')]),

    ("M2", "把「删旧」写成「留旧」—— 同样是方向反了",
     [(MONITOR, '''    frag, val = _older_than_clause("detected_at", older_than)
    with storage._conn() as conn:''',
        '''    frag, val = f"{_normalized('detected_at')} >= ?", older_than
    with storage._conn() as conn:''')]),

    # ── 时间格式 ──
    ("M3", "去掉格式归一 —— 空格分隔的行查不到、也删不掉",
     [(MONITOR, '''def _normalized(column: str) -> str:
    """把某一列的时间戳归一成字典序可比的形状(见 `_DETECTED_AT_SQL`)"""
    return f"REPLACE({column}, ' ', 'T')"''',
        '''def _normalized(column: str) -> str:
    """把某一列的时间戳归一成字典序可比的形状(见 `_DETECTED_AT_SQL`)"""
    return column''')]),

    ("M4", "归一化只替换第一个空格 —— 归一了个寂寞",
     [(MONITOR, '''    return f"REPLACE({column}, ' ', 'T')"''',
        '''    return f"REPLACE({column}, ' ', 'T', 1)"''')]),

    # ── 默认不删 ──
    ("M5", "dry_run 默认改成 False —— 少打一个 --yes 就删了",
     [(MONITOR, "def prune_changes(storage, older_than: str, dry_run: bool = True) -> dict:",
                "def prune_changes(storage, older_than: str, dry_run: bool = False) -> dict:")]),

    ("M6", "CLI 把 --yes 取反 —— 加了 --yes 反而不删",
     [(CLI, "    dry = not args.yes", "    dry = args.yes")]),

    # ── 工作区隔离 ──
    # 这条**第一版存活**,查实了原因:每个工作区是独立的 db 文件
    # (`<root>/<name>/data.db`),两个工作区的数据不在一个库里,所以去掉
    # `workspace_id = ?` 在行为上完全等价 —— 和 r48 查实的「hash 里的
    # workspace_id 恒等于 1」是同一件事:这个条件今天几乎是空转的。
    # 所以行为测试区分不出来(补测试也补不出来),改用 AST 把意图钉住,
    # 并把「当前布局让它空转」写进那条测试的文档。写对一个条件只有一个
    # AND 的成本,空转的成本是「哪天改单库多工作区,清理静默跨库删数据」。
    ("M7", "prune 漏掉 workspace_id 条件(当前布局下行为等价,靠 AST 那条抓)",
     [(MONITOR, '''        cur = conn.execute(
            f"DELETE FROM asset_changes WHERE workspace_id = ? AND {frag}",
            [storage.workspace_id, val])''',
        '''        cur = conn.execute(
            f"DELETE FROM asset_changes WHERE {frag}", [val])''')]),

    # ── dry-run 计数与实际删除一致 ──
    ("M8", "试算的计数和真删用两套判据 —— dry-run 骗人",
     [(MONITOR, '''        affected = int(conn.execute(
            f"SELECT COUNT(*) FROM asset_changes "
            f"WHERE workspace_id = ? AND {frag}",
            [storage.workspace_id, val]).fetchone()[0])''',
        '''        affected = int(conn.execute(
            f"SELECT COUNT(*) FROM asset_changes "
            f"WHERE workspace_id = ? AND {frag} AND 1=0",
            [storage.workspace_id, val]).fetchone()[0])''')]),

    # ── `--since` 的解析只有一份,且拒绝垃圾值 ──
    ("M9", "`--since` 回到「能 parse 就当时间」—— 垃圾值静默返回全部",
     [(CLI, '''    dt = parse_ts(value)
    if dt is None:
        raise ValueError(f"not a recognised window or timestamp: {value!r}")
    return dt.isoformat()''',
        "    return value")]),

    ("M10", "空串 `--since` 静默当成「没给」—— 用户以为筛过了",
     [(CLI, '    if getattr(args, "since", None) is not None:',
                "    if getattr(args, \"since\", None):")]),

    ("M11", "负数窗口被接受 —— `-3d` 是三天后,方向是反的",
     [(CLI, '''        n = int(value[:-1])
        if n < 0:
            raise ValueError(f"{unit} must be >= 0, got {n}")''',
        "        n = int(value[:-1])")]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这里」。
COVERAGE_MUTANTS = [
    # 期望**存活**:去掉「方向」那条,再把方向改回去 —— 那条是承重的。
    # 但第一版这里期望存活而实测被杀:方向其实被**重复覆盖**了 ——
    # 「空格格式那条也删得掉」(它断言 deleted == 2)和
    # 「试算计数 == 实际删除」两条都会在方向反了时变红。如实记着:
    # 我原以为那条是唯一防线,实际上有三道。
    ("C-direction", "去掉「prune 删的是旧的」那条,同时把方向改回 >= ",
     [(TEST, "def test_prune_deletes_the_old_rows_not_the_new_ones(",
             "@pytest.mark.skip\ndef test_prune_deletes_the_old_rows_not_the_new_ones("),
      (MONITOR, '''    frag, val = _older_than_clause("detected_at", older_than)
    with storage._conn() as conn:''',
        '''    frag, val = _since_clause("detected_at", older_than)
    with storage._conn() as conn:''')], False),

    # 期望**被杀,但死在另一道闸**:「空格格式也删得掉」那条去掉之后,
    # 查询侧那条(`test_space_separated_rows_are_found_within_the_same_day`)
    # 还在,去掉归一化它就红。如实记着:格式不变式被覆盖了两遍。
    ("C-format", "去掉「空格格式那条也删得掉」那条,同时去掉格式归一",
     [(TEST, "def test_space_separated_rows_are_prunable(",
             "@pytest.mark.skip\ndef test_space_separated_rows_are_prunable("),
      (MONITOR, '''    return f"REPLACE({column}, ' ', 'T')"''', "    return column")], False),

    # 期望**被杀,但死在另一道闸**:把默认改成真删,`test_dry_run_is_the_default`
    # 还在,它会红。如实记着谁兜住的。
    ("C-dryrun", "去掉「默认不删」那条,同时把 dry_run 默认改成 False",
     [(TEST, "def test_dry_run_is_the_default_and_deletes_nothing(",
             "@pytest.mark.skip\ndef test_dry_run_is_the_default_and_deletes_nothing("),
      (MONITOR, "def prune_changes(storage, older_than: str, dry_run: bool = True) -> dict:",
                "def prune_changes(storage, older_than: str, dry_run: bool = False) -> dict:")],
     False),
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
        print("── 方向/格式/默认不删:存活 = 测试有洞 ──")
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
