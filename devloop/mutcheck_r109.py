"""r109 变异测试:验「完成标记的判定只有一份」和「判据的盲区被堵上了」。

## 主题

r108 收尾时顺手量了真实 `backlog.md` 与 `queue.json` 的一致性,量出两件事,
**都是实测的、不是推测**:

### 一、完成标记的判定在两处各写一份,而且**已经漂了**

    播种   arl_lite/devloop/queue.py            detail.startswith("✅")
    判据   tests/test_devloop_queue_invariant.py  "✅" in detail

实测后果:一条 detail 写 `做完了,✅ r99 完成` 的行,判据判定它「标了完成」
(于是要求队列里有 done 记录),而播种判定它**没标完成**,照样捡回来当新活。

比「两处都写错」更难查的是这一种:**判据看不见实现的真实行为**,于是它
绿着而引擎在做另一件事。r28 的 `is_signal_id` 就是为同一个理由抽出来的
(`round` / CLI / 测试各写一份前缀匹配)。r109 把完成标记也归到一份:
`queue.is_backlog_done`,播种和判据都问它。

### 二、四条真实的 backlog 行,队列里 done 却没标 ✅

`bulk-insert` / `200` / `asset-changes` / `watcher-n-3-ports-sites`。代码里
也真做完了(`_ASSET_IDENTITY` 归一 / `CHANGE_RECORD_CAP` 可见截断 /
`monitor changes --since` + `monitor prune` / `new_by_type` 从
`Monitor._ASSET_TABLES` 派生),但 detail 段一个 ✅ 都没有。

**功能上为什么没出事**:播种还有第二道保护 `done_ids`。所以代价只是
`backlog.md` 这个**给人看的索引**在骗人。**也正因为没有任何可观测的功能
故障,判据只能盯一致性,盯不了「有没有出事」** —— 这类缺陷天生难被发现。

### 三、判据自己的盲区:解析失败的行对所有判据隐形

补那 4 个 ✅ 时我把 `- [P1]` 写成了 `- [1]`(`m.group(1)` 捕到的是 `1`,
`P` 是字面量不在捕获组里),4 行整行不再匹配 `_BACKLOG_LINE`。后果:

  - 播种:走 `done_ids` 兜住了,没出事
  - 「标了 ✅ ⇒ done」:它不在 entries 里,不参与检查
  - 「done ⇒ 标了 ✅」(r109 新加):同样不参与检查
  - 竖线数量校验:2 个,**通过** —— 竖线对上了,标签错了,没一条判据看标签

**任何只遍历「解析成功的那些」的判据,都有一个共同的盲区:解析失败的那些。**
而 `_BACKLOG_LINE` 匹配不上时不报错、不告警,只是安静地不参与。这是 r83
记过的「探针静默漏报,然后我拿它的输出下结论」,r109 同一轮里又踩一遍。

## 变异清单

实现变异(期望全被杀):
  M1 判定漂回子串包含            → 判据 3(判定=播种)红
  M2 判定恒 False                 → 判据 3 + 反方向 + 解析 全红
  M3 播种不再问共享判定,自己抄一份 → 判据 3 红
  M4 拿掉一条 ✅                  → 反方向判据红
  M5 优先级标签 `[P1]` → `[1]`     → 解析判据红
  M6 整个 backlog 条目区清空       → 3 条前置条件红

覆盖变异(期望全存活,拆判据):
  C1 拆「判定 = 播种」      ← M1/M2/M3 的唯一防线
  C2 拆「done ⇒ 标了 ✅」    ← M4 的唯一防线
  C3 拆「每行都得解析得出来」  ← M5 的唯一防线

## M5 单独说一句

M4/M6 测的是「内容错了」,M5 测的是**「这行压根没进检查范围」**。而 M5
是 r109 实际踩出来的 —— 写完 4 个标记、竖线数量校验通过、判据全绿,
而那 4 行已经对所有判据隐形了。**判据只遍历它能读到的,而它读不到的
不会报错。** 所以「读不到」本身必须是一条判据。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。纯插入 must_not 留空、纯删除 must_have 留空。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_devloop_queue_invariant.py"
QUEUE = REPO / "arl_lite" / "devloop" / "queue.py"
BACKLOG = REPO / "devloop" / "backlog.md"

TARGET = ["tests/test_devloop_queue_invariant.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")


def _run_itself_broke(outp: str) -> bool:
    lines = _RUN_SUMMARY_RE.findall(outp)
    if not lines:
        return True
    tail = lines[-1]
    if "no tests ran" in tail:
        return True
    if _ERROR_IN_SUMMARY_RE.search(tail):
        return "AssertionError" not in outp
    return False


# ── M1/M2/M3:判据那份「唯一判定」的漂移 ──
A_PRED = '    return detail.startswith("✅")'
M1_SUBSTR = '    return "✅" in detail'
# 首版写的是 `    return False` —— harness 报 BAD-MUTANT:那个串在 queue.py 里
# **出现 18 次**,声明被同名兄弟喂饱,`must_have` 判不出「改前不存在」。
# 正是 r83 记过的那条:声明串必须唯一。给它带上自标识的注释。
M2_ALWAYS_FALSE = "    return False  # 变异 M2:完成标记的闸门被焊死"

A_SOW = "            if is_backlog_done(detail):"
M3_SELFCOPY = '            if "✅" in detail:'

# ── M4:拿掉一条 ✅ ──
# 首版这两串只照抄到 `done_round=56;` 就停了(半句,括号没配平)。本脚本带
# `_write_checked`,按 `tests/test_mutcheck_anchors_are_whole_statements.py`
# 的现代约定,old 侧必须**整句**。这里整体照抄那个完成标记,含结尾括号。
A_MARK = ("✅ r109 补标(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
          "且丢弃条数可见(watcher.py:46-109,478))· ")
M4_GONE = ("完成标记(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
           "且丢弃条数可见(watcher.py:46-109,478))· ")

# ── M5:优先级标签写坏 ──
A_TAG = "- [P1] change: 变更风暴时被 200"
M5_TAG = "- [1] change: 变更风暴时被 200"

# ── 覆盖变异的边界锚点 ──
# 首版按「我以为的顺序」写,C1 的结束锚点落在了起点**之前**,`src.index(end, i)`
# 抛 ValueError。实际顺序是:real_backlog(299) → looks_like_a(379) →
# _real_backlog_entries(420) → done_work_is_marked(439) → done_marker(482)。
# 锚点必须照**文件里的实际顺序**写,不能照脑子里想的顺序写。
C1_BODY = "def test_the_done_marker_predicate_matches_what_seeding_actually_honours():"
C1_END = "def test_id_does_not_depend_on_line_position():"
C2_BODY = "def test_done_work_is_marked_done_in_the_human_facing_index():"
C2_END = "def test_the_done_marker_predicate_matches_what_seeding_actually_honours():"
C3_BODY = "def test_every_line_that_looks_like_a_backlog_entry_actually_parses():"
C3_END = "def _real_backlog_entries() -> list[dict]:"

CLAIMS = {
    # **全部字面量,一个名字引用都不许有。** 首版这里写的是
    # `[A_PRED.strip()]` / `[A_MARK]` / `[M4_GONE]` —— 前者是方法调用、
    # 后者是 `ast.Name`,`test_mutcheck_claims_match_their_names.py` 走
    # `ast.literal_eval`,遇到 Call / Name 节点直接抛
    # `ValueError: malformed node or string`,那 6 条判据于是**全部在报错**
    # 而不是在校验。两次都是同一个错:CLAIMS 里放了名字而不是值。
    # 相邻的字符串字面量会被 AST 折成一个 Constant,写多行仍然安全。
    "M1-判定漂回子串": (['    return "✅" in detail'],
                        ['    return detail.startswith("✅")']),
    "M2-判定恒False": (
        ["    return False  # 变异 M2:完成标记的闸门被焊死"],
        ['    return detail.startswith("✅")']),
    "M3-播种自己抄一份": (['            if "✅" in detail:'],
                          ["            if is_backlog_done(detail):"]),
    "M4-拿掉一条完成标记": (
        ["完成标记(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
         "且丢弃条数可见(watcher.py:46-109,478))· "],
        ["✅ r109 补标(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
         "且丢弃条数可见(watcher.py:46-109,478))· "]),
    "M5-优先级标签写坏": (["- [1] change: 变更风暴时被 200"],
                          ["- [P1] change: 变更风暴时被 200"]),
    "M6-条目区整个清空": ([], ["- [P1] change: 变更风暴时被 200"]),   # 纯删除
    "C1-拆判定等于播种那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆done必带标记那条": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆每行都得解析那条": (["    pass  # 变异 C3:整条判据没了"], []),
}

MUTANTS = [
    ("M1-判定漂回子串", lambda p: _apply(p, A_PRED, M1_SUBSTR), False, (QUEUE,)),
    ("M2-判定恒False", lambda p: _apply(p, A_PRED, M2_ALWAYS_FALSE), False, (QUEUE,)),
    ("M3-播种自己抄一份", lambda p: _apply(p, A_SOW, M3_SELFCOPY), False, (QUEUE,)),
    ("M4-拿掉一条完成标记", lambda p: _apply(p, A_MARK, M4_GONE), False, (BACKLOG,)),
    ("M5-优先级标签写坏", lambda p: _apply(p, A_TAG, M5_TAG), False, (BACKLOG,)),
    ("M6-条目区整个清空", lambda p: _write_all_gone(p), False, (BACKLOG,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆判定等于播种那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆done必带标记那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
    ("C3-拆每行都得解析那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT,)),
]


def _write_all_gone(path: pathlib.Path) -> None:
    """把 backlog.md 里所有 `- [P…]` 条目行删掉,其余(标题、候选清单)留着。"""
    src = path.read_text(encoding="utf-8")
    keep = [ln for ln in src.splitlines() if not re.match(r"^\s*-\s*\[P[0-3]\]", ln)]
    _write_checked(path, "\n".join(keep) + "\n")


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    if not must_have and not must_not:
        raise AssertionError(f"变异 {name!r} 的声明是空的 —— 等于没声明")
    for piece in must_not:
        n = original.count(piece)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {piece!r} 在改前出现 {n} 次 —— 指不准位置")
    for piece in must_have:
        if piece in original:
            raise AssertionError(
                f"变异 {name!r} 的 must_not/must_have 写反了:{piece!r} 改前就存在")


def _verify_claim(name: str, mutated: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    missing = [s for s in must_have if s not in mutated]
    leftover = [s for s in must_not if s in mutated]
    if missing or leftover:
        raise AssertionError(
            f"变异 {name!r} 的替换没有做到它名字声称的事: 缺 {missing} 仍在 {leftover}")


def _write_checked(path: pathlib.Path, out: str) -> None:
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    # M1/M2/M3 改的是 `arl_lite/devloop/queue.py`、M5 改的是 markdown,
    # 但**覆盖变异 C1–C3 改的是 `tests/test_devloop_queue_invariant.py`,
    # 是 .py** —— 语法守卫在这里是真需要的,不是形式。r107 栽过一次:
    # 我按「r107 只写 .md」把整段删了,结果覆盖变异没处可写。
    # **同一个 `_write_checked` 承接几类目标时,守卫得按后缀分支,不能整段删。**
    if path.suffix == ".py":
        import ast
        try:
            ast.parse(out)
        except SyntaxError as e:
            raise AssertionError(
                f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    n = src.count(old)
    if n != 1:
        raise AssertionError(
            f"变异锚点在 {path.name} 出现 {n} 次(必须恰好 1 次):{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _replace_fn(path: pathlib.Path, start: str, stub: str, end: str) -> None:
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    if src.count(end) != 1:
        raise AssertionError(f"变异结束标记没唯一命中 {path.name}:{end!r}")
    i = src.index(start)
    j = src.index(end, i)
    if j <= i:
        raise AssertionError(
            f"结束标记 {end!r} 出现在起点**之前** —— 标记写反了,不是判据的问题")
    _write_checked(path, src[:i] + stub + src[j:])


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q",
         "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
        capture_output=True, text=True, cwd=REPO, timeout=600,
    )


def _sweep(mutants) -> list:
    out = []
    for name, mutate, expect, targets in mutants:
        backups = {t: t.read_bytes() for t in targets}
        try:
            try:
                _check_claim_points_at_one_place(
                    name, "\n".join(t.read_text(encoding="utf-8") for t in targets))
                for t in targets:
                    mutate(t)
                _verify_claim(name, "\n".join(
                    t.read_text(encoding="utf-8") for t in targets))
            except AssertionError as e:
                out.append((name, "BAD-MUTANT", str(e)[:400]))
                continue
            except Exception as e:
                out.append((name, "BAD-MUTANT",
                            f"变异器自己抛了 {type(e).__name__}: {e}"[:400]))
                continue
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if _run_itself_broke(outp):
                out.append((name, "BAD-SYNTAX", outp[-400:]))
                continue
            killed = r.returncode != 0
            detail = "killed" if killed else "survived"
            if killed == expect:
                detail = f"UNEXPECTED-{detail}"
            out.append((name, detail, outp[-500:] if killed == expect else ""))
        finally:
            for t, data in backups.items():
                t.write_bytes(data)
    return out


def main() -> int:
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r109 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:24s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr109 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
