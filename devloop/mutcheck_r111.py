"""r111 变异测试:验「队列与 backlog 的 verify 对账」和「已完成的活验收命令还跑得通」。

## 主题

r110 换掉一条 verify 之后顺手普查,发现 **`backlog.md` 和 `queue.json` 是同一件活
的两份副本,而引擎只读其中一份**:

    protocol.py:786    vr = q.verify_result(item.verify)     ← 队列里那份

改源文件(`backlog.md`,人改的)不更新副本。实测 **11 条不一致**,后果不是「慢一点」:

| 条目 | queue.json 里的 verify | 引擎看到的 `verify_result` |
|---|---|---|
| `confidence` / `confidence-risk` / `confidence-status` | **散文**(怎么做) | `unknown` |
| `item-c66b81`(误报率实测) | **空字符串** | `unknown` |
| `check-filter-sql-union` / `storage` / `disappeared` | **`pytest tests/`** | `unknown`(30s 超时) |
| `san-issuer-fingerprint` | **整套门禁** | `unknown` |

而 `protocol.py` **只拦 `VERIFY_FAIL`**。所以这 11 条在引擎眼里是
**「判断不了」→ 不拦 → 任何时候都能被标 done**。

`item-c66b81` 那条还多一层:它的 verify 现在 **exit 2** —— `fp-bench` 后来加了
「默认不覆盖已提交的基线」的保护。**工具行为变了,验收标准没跟着变,而没有任何
东西会响**;条目 detail 里还写着「可复跑」。

## 三条判据,各钉一个方向

1. **两边 verify 必须一致**(r111 新加)
2. **每条已标 ✅ 的条目,验收命令必须还跑得通** —— 由
   `devloop/backlog_verify_sweep.py` 承担,**不在门禁里**(实测 1m37s,
   而 `test_baseline` 已在 600s 的 535s 上)。代价:一个不在门禁里的检查会烂
   在那里,r107 那条轮次索引就是这么烂掉的。
3. 前置条件:**两边至少 30 条 id 对得上**,否则范围不对,结论不能用

## 变异清单

实现变异(期望全被杀):
  M1 队列里一条 verify 改坏      → 判据 1 红
  M2 backlog 里一条 verify 改坏  → 判据 1 红
  M3 backlog 里 ✅ 被拿掉        → 判据「done 必带标记」红
  M4 backlog 条目劈成两行        → 判据「不许劈半」红
  M5 条目区整个清空              → 前置条件红

覆盖变异(期望全存活,拆判据):
  C1 拆「两边 verify 一致」  ← M1/M2 的唯一防线
  C2 拆「done 必带标记」     ← M3 的唯一防线
  C3 拆「不许劈半」          ← M4 的唯一防线

## M1 与 M2 为什么要各来一条

只改一边(比如只改 backlog)能抓到「两边不一致」,但抓不到**方向**:分不清是
队列过期了还是源文件写错了。两条都改才能确认判据是在比**值**,不是只看
「有一边动了」。

## 扫 verify 跑不跑得通,为什么不写成变异

`backlog_verify_sweep.py` 会真跑 37 条(1m37s)。把它做成变异(M7:把某条 verify
改成一个跑不通的命令,期望 sweep 报红)值得做,但本轮先不加 ——
**一次加太多变异,分不清是哪一条机制在起作用**。下轮补。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量 —— r109 写了 `.strip()`、r110 写了 `A_TAG`,两次都让
`ast.literal_eval` 抛 `ValueError`,6 条判据在报错而不是在校验。
old 侧必须**整句**配平。纯插入 must_not 留空、纯删除 must_have 留空。
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT_Q = REPO / "tests" / "test_devloop_queue_invariant.py"
BACKLOG = REPO / "devloop" / "backlog.md"
QUEUE = REPO / "devloop" / "queue.json"

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


# ── M1:队列里一条 verify 改坏 ──
# 首版把改后写成裸的 `python3 -m pytest tests/`,harness 报 BAD-MUTANT:那个串
# 在 queue.json 里**改前就存在**(108 条里另外 71 条也有)—— 声明被同名兄弟
# 喂饱,正是 r83 记过的那条。给它带上自标识的注释。
A_Q_VERIFY = "python3 -m pytest tests/test_db_errors.py"
M1_Q_VERIFY = "python3 -m pytest tests/  # 变异 M1:退回全量(引擎 30s 超时 → unknown)"

# ── M2:backlog 里同一条 verify 改坏(方向相反) ──
A_B_VERIFY = "python3 -m pytest tests/test_sql_injection.py"
M2_B_VERIFY = "造 60 条关联,断言最高置信度那条排最后(变异 M2:写成散文)"

# ── M3:拿掉一条 ✅ ──
A_MARK = ("✅ r109 补标(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
          "且丢弃条数可见(watcher.py:46-109,478))· ")
M3_GONE = ("完成标记(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
           "且丢弃条数可见(watcher.py:46-109,478))· ")

# ── M4:把一条条目劈成两行 ──
A_ENTRY = "- [P1] change: 变更风暴时被 200"
M4_SPLIT = "这行接在一条条目后面却不是条目(变异 M4:条目被劈成了两行)"

# ── 覆盖变异的边界锚点(照文件里的**实际顺序**写) ──
# queue_invariant 实际顺序:
#   real_backlog → looks_like_a → no_split → verify_agrees
#   → done_marked → done_marker_predicate → id_depends_on_position
C1_BODY = "def test_queue_and_backlog_agree_on_every_shared_items_verify():"
C1_END = "def _real_backlog_entries() -> list[dict]:"
C2_BODY = "def test_done_work_is_marked_done_in_the_human_facing_index():"
C2_END = "def test_the_done_marker_predicate_matches_what_seeding_actually_honours():"
C3_BODY = "def test_no_backlog_entry_is_split_across_two_lines():"
C3_END = C1_BODY

CLAIMS = {
    "M1-队列verify改坏": (
        ["python3 -m pytest tests/  # 变异 M1:退回全量(引擎 30s 超时 → unknown)"],
        ["python3 -m pytest tests/test_db_errors.py"]),
    "M2-backlogverify改坏": (
        ["造 60 条关联,断言最高置信度那条排最后(变异 M2:写成散文)"],
        ["python3 -m pytest tests/test_sql_injection.py"]),
    "M3-拿掉一条完成标记": (
        ["完成标记(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
         "且丢弃条数可见(watcher.py:46-109,478))· "],
        ["✅ r109 补标(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
         "且丢弃条数可见(watcher.py:46-109,478))· "]),
    "M4-条目劈成两行": (
        ["这行接在一条条目后面却不是条目(变异 M4:条目被劈成了两行)"], []),
    "M5-条目区整个清空": ([], ["- [P1] change: 变更风暴时被 200"]),
    "C1-拆两边一致那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆done必带标记那条": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆不许劈半那条": (["    pass  # 变异 C3:整条判据没了"], []),
}

MUTANTS = [
    ("M1-队列verify改坏", lambda p: _apply(p, A_Q_VERIFY, M1_Q_VERIFY), False, (QUEUE,)),
    ("M2-backlogverify改坏", lambda p: _apply(p, A_B_VERIFY, M2_B_VERIFY), False, (BACKLOG,)),
    ("M3-拿掉一条完成标记", lambda p: _apply(p, A_MARK, M3_GONE), False, (BACKLOG,)),
    ("M4-条目劈成两行", lambda p: _split_entry(p), False, (BACKLOG,)),
    ("M5-条目区整个清空", lambda p: _write_all_gone(p), False, (BACKLOG,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆两边一致那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT_Q,)),
    ("C2-拆done必带标记那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT_Q,)),
    ("C3-拆不许劈半那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT_Q,)),
]


def _split_entry(path: pathlib.Path) -> None:
    """把一条真条目劈成两行:下半截不带 `- [P` 前缀。

    **下半截不能以 `#` / `- ` / `>` / `---` 开头** —— 判据把那些当小标题/
    列表/引用/分隔线跳过,r110 首版就是这么造出一条「判据压根不查」的变异,
    报出来却像「判据抓不住劈半」。**假结论比没有结论更贵。**
    """
    src = path.read_text(encoding="utf-8")
    if src.count(A_ENTRY) != 1:
        raise AssertionError(
            f"变异锚点在 {path.name} 出现 {src.count(A_ENTRY)} 次(必须恰好 1 次)")
    j = src.index("\n", src.index(A_ENTRY))
    _write_checked(path, src[:j] + "\n" + M4_SPLIT + src[j:])


def _write_all_gone(path: pathlib.Path) -> None:
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


def _assert_claims_are_all_literals() -> None:
    """CLAIMS 里只许有字面量 —— 方法调用和名字引用都不行

    r109 写了 `[A_PRED.strip()]`,r110 写了 `[A_TAG]` / `[A_MARK]` / `[A_TITLE]`,
    两次都是 `ast.literal_eval` 抛 `ValueError`,那 6 条判据于是**全在报错
    而不是在校验**,而门禁把它算成「回归」。

    ## 它自己第一版是**恒真**的

    首版写的是 `getattr(node.value, "elts", [])` —— 而 `ast.Dict` 根本没有
    `.elts`(在 `.keys` / `.values` 上),`getattr` 返回 `[]`,循环一次没跑,
    `offenders` 永远空,断言永远绿。**「取不到就当空」和 r83 那次
    `getattr(node, "blocking", None)` 是同一个错**:取不到值时它不报错,
    它把「没取到」说成「没有」。

    所以除了正着查,下面还**反着自查遍历器本身**。
    """
    import ast as _ast
    tree = _ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    dicts = [n for n in tree.body
             if (isinstance(n, _ast.Assign) and len(n.targets) == 1
                 and isinstance(n.targets[0], _ast.Name)
                 and n.targets[0].id == "CLAIMS"
                 and isinstance(n.value, _ast.Dict))]
    if not dicts:
        raise AssertionError("这个脚本里根本没有 CLAIMS —— 声明被删了?")
    if len(dicts) > 1:
        raise AssertionError(f"CLAIMS 被赋值了 {len(dicts)} 次 —— 读到哪一份是运气")
    node = dicts[0]
    pairs = list(zip(node.value.keys, node.value.values))
    offenders = []
    for k, v in pairs:
        name = k.value if isinstance(k, _ast.Constant) else "<非字面量键>"
        if not isinstance(v, (_ast.Tuple, _ast.List)):
            offenders.append(f"{name}: 声明本身是 {type(v).__name__}")
            continue
        for side in v.elts:
            if not isinstance(side, (_ast.Tuple, _ast.List)):
                offenders.append(f"{name}: 一侧是 {type(side).__name__}")
                continue
            for piece in side.elts:
                if not isinstance(piece, _ast.Constant):
                    offenders.append(f"{name}: {type(piece).__name__}")
    assert pairs, "CLAIMS 是空字典 —— 遍历器一次都没跑,上面那些检查是恒真的"
    assert any(isinstance(p, _ast.Constant)
               for _k, v in pairs for s in v.elts for p in s.elts), (
        "CLAIMS 里连一个字符串字面量都没有 —— 遍历器坏了,别信上面那些检查")
    assert not offenders, (
        f"CLAIMS 里出现了非字面量:{offenders}\n"
        f"  `ast.literal_eval` 遇到它们会抛 `ValueError: malformed node`,"
        f"那 6 条判据会**在报错**而不是在校验 —— 而门禁把它算成回归。")


def _write_checked(path: pathlib.Path, out: str) -> None:
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    # 覆盖变异 C1–C3 改的是 `tests/test_devloop_queue_invariant.py`,**是 .py**;
    # M1 改的是 `devloop/queue.json`。语法守卫要**按后缀分支**:
    # `.json` 走 `json.loads` 验,`.py` 走 `ast.parse`,其余直接写。
    # r107 栽过一次:按「只写 .md」把整段删了,结果覆盖变异没处可写。
    if path.suffix == ".py":
        import ast
        try:
            ast.parse(out)
        except SyntaxError as e:
            raise AssertionError(
                f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    elif path.suffix == ".json":
        try:
            json.loads(out)
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"变异会让 {path.name} 不是合法 JSON({e})。文件未写入。") from None
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
    _assert_claims_are_all_literals()
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r111 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:22s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr111 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
