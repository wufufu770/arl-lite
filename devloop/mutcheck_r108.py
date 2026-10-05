"""r108 变异测试:验「协议文档的门禁总表不会跟实现脱节」在守。

## 主题

`docs/devloop-protocol.md` 的 5.1「七道门禁总表」与 `arl_lite/devloop/gates.py`
脱节了 67 轮。**7 条里 5 条的命令/阈值是错的或干脆虚构的** —— 而门禁的
`name` 和 `blocking` 七条全对,所以粗看没问题,只有逐条核对才看得出。

判据在 `tests/test_protocol_doc_gate_table_matches_code.py`,5 条。

## 变异清单

实现变异(期望全被杀,改 `docs/devloop-protocol.md`):
  M1 把 G2 的门禁名改错          → 判据 1(不多不少)红
  M2 把 G7 的 blocking 标反      → 判据 2(标注与类属性一致)红
  M3 **还原 G3 原来的那句谎话**  → 判据 3(`.py` 路径真实)红
  M4 **还原 G7 原来的那句谎话**  → 判据 4(`state.json` 键真实)红
  M5 把整张 5.1 表清空           → 判据 1 + 判据 5(表恰好 7 行)红

覆盖变异(期望全存活,拆判据):
  C1 拆「门禁名不多不少」← M1 的唯一防线
  C2 拆「blocking 标注一致」← M2 的唯一防线
  C3 拆「.py 路径真实存在」← M3 的唯一防线
  C4 拆「state.json 键真实」← M4 的唯一防线
  C5 拆「剥离规则自检」    ← M5 的第二道防线

## M3/M4 为什么是「还原谎话」而不是「编个新的假路径」

r107 的教训:变异如果只是随手编一个假值,它验的是「判据认不认识这个假值」,
不是「判据能不能抓住 r108 查出的**那一种**缺陷」。所以 M3/M4 直接把 G3/G7
改回 r108 动手之前的样子 —— 判据要是当初就存在,这两条当场就该红。

它们同时也是**假阳性的反证**:M3 里 `tools/check_imports.py` 是用 `「」`
包着的(引述),要写成自己声称用「没这个文件」那句;M4 里的
`state.json.last_doc_audit_round` 必须**不在** `「」` 里 —— 它一旦被
`test_the_stripper_itself_still_strips_what_it_claims` 的剥离规则吃掉,
M4 就存活,判据立刻报「变异没做到它名字声称的事」。

## 首版栽的一次:靠 tail 补丁排除 `state.json.tmp`

首版把「`state.json.tmp` 是文件名不是键」当成「后面跟不跟扩展名」来判断,
先写成 `k + ext`(拼出来是 `"tmp.tmp"`,**永远匹配不上**),改成扫 tail 之后
又得逐个补 4 种上下文:行尾、`(写了一半`、`(覆盖)`、` -> `。

那是给假阳性打补丁。四处 `state.json.tmp` **全在代码块里**(1 处 ```python
围栏、3 处 4 空格缩进块),改成剥代码区后一次性归零,一条启发式都不用:

    原文扫到 7 处 → 剥引述+代码区后剩 1 处(`history`,真键)

判据自己也得有牙:另加一条用**合成输入**独立验剥离规则(把 `_strip_quoted`
换成 `return text`,实测本条 + 上面两条共 3 条转红)。理由是剥离一旦失效,
症状会报成「文档有虚构引用」,修的人会去删 G3 那句**关键的更正**。

## 另跑了 6 条负控制(不是变异,是判据本身的手工体检)

| 注入 | 预期 | 实测 |
|---|---|---|
| NC1 门禁名改错 | 判据 1 红 | 1 failed ✓ |
| NC2 blocking 标反 | 判据 2 红 | 1 failed ✓ |
| NC3 表里塞不存在的 .py | 判据 3 红 | 1 failed ✓ |
| NC4 正文塞不存在的 state 键 | 判据 4 红 | 1 failed ✓ |
| NC5 剥离规则退化成 `return text` | 剥离自检 + 2 条红 | 3 failed ✓ |
| NC6 整张表删空 | 判据 1/2/5 红 | 3 failed ✓ |

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。纯插入 must_not 留空、纯删除 must_have 留空。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_protocol_doc_gate_table_matches_code.py"
DOC = REPO / "docs" / "devloop-protocol.md"

TARGET = ["tests/test_protocol_doc_gate_table_matches_code.py"]

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


# ── 锚点:从 docs/devloop-protocol.md 逐行 repr 取,每条改前只出现一次 ──
A_G2 = "| **G2** | `test_baseline` |"
M1_TYPO = "| **G2** | `test_baselin` |"

A_G7 = "| **G7** | `doc_freshness` | 文档吹嘘已下架依赖 | **非阻断** |"
M2_BLOCK = "| **G7** | `doc_freshness` | 文档吹嘘已下架依赖 | **blocking** |"

# M3 还原 G3 动手前的样子:不是「没有这个文件」,而是**声称靠它跑**
A_G3 = "建图做 DFS。**没有「`tools/check_imports.py`」** —— 本表此前写的是它"
M3_LIE = "建图做 DFS,**自写 `tools/check_imports.py` 跑 import 图 DFS**"

# M4 还原 G7 动手前的样子。注意 `last_doc_audit_round` 不能落在 `「」` 里 ——
# 落在里面就是「引述」,会被剥离规则吃掉,M4 就名不副实。
A_G7_STATE = "**与 `state.json` 无关** |"
M4_LIE = "**判据是 `state.json.last_doc_audit_round` 与当前轮差 ≤ 5** |"

# M5:整张表清空(判据 5 钉的「恰好 7 行」)
A_G7_HEAD = "| **G7** | `doc_freshness` |"

# ── 覆盖变异的边界锚点(判据文件里 def 的先后顺序) ──
C1_BODY = "def test_the_table_lists_exactly_the_gates_that_exist():"
C1_END = "def test_blocking_column_matches_the_class_attribute():"
C2_BODY = "def test_blocking_column_matches_the_class_attribute():"
C2_END = "def test_every_python_path_the_doc_mentions_actually_exists():"
C3_BODY = "def test_every_python_path_the_doc_mentions_actually_exists():"
C3_END = "def test_every_state_json_key_the_doc_mentions_actually_exists():"
C4_BODY = "def test_every_state_json_key_the_doc_mentions_actually_exists():"
C4_END = "def test_the_stripper_itself_still_strips_what_it_claims():"
C5_BODY = "def test_the_stripper_itself_still_strips_what_it_claims():"
C5_END = "def test_the_criterion_would_notice_a_blank_table():"

CLAIMS = {
    "M1-门禁名改错": (
        ["| **G2** | `test_baselin` |"],
        ["| **G2** | `test_baseline` |"],
    ),
    "M2-blocking标反": (
        ["| **G7** | `doc_freshness` | 文档吹嘘已下架依赖 | **blocking** |"],
        ["| **G7** | `doc_freshness` | 文档吹嘘已下架依赖 | **非阻断** |"],
    ),
    "M3-还原G3的谎话": (
        ["建图做 DFS,**自写 `tools/check_imports.py` 跑 import 图 DFS**"],
        ["建图做 DFS。**没有「`tools/check_imports.py`」**"],
    ),
    "M4-还原G7的谎话": (
        ["**判据是 `state.json.last_doc_audit_round` 与当前轮差 ≤ 5**"],
        ["**与 `state.json` 无关**"],
    ),
    "M5-整张表清空": (
        [],  # 纯删除
        ["| **G7** | `doc_freshness` |"],
    ),
    "C1-拆门禁名那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆blocking那条": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆py路径那条": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆state键那条": (["    pass  # 变异 C4:整条判据没了"], []),
    "C5-拆剥离自检那条": (["    pass  # 变异 C5:整条判据没了"], []),
}

MUTANTS = [
    ("M1-门禁名改错", lambda p: _apply(p, A_G2, M1_TYPO), False, (DOC,)),
    ("M2-blocking标反", lambda p: _apply(p, A_G7, M2_BLOCK), False, (DOC,)),
    ("M3-还原G3的谎话", lambda p: _apply(p, A_G3, M3_LIE), False, (DOC,)),
    ("M4-还原G7的谎话", lambda p: _apply(p, A_G7_STATE, M4_LIE), False, (DOC,)),
    ("M5-整张表清空", lambda p: _write_all_gone(p), False, (DOC,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆门禁名那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆blocking那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
    ("C3-拆py路径那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT,)),
    ("C4-拆state键那条",
     lambda p: _replace_fn(p, C4_BODY, C4_BODY + "\n    pass  # 变异 C4:整条判据没了\n",
                           C4_END), True, (CRIT,)),
    ("C5-拆剥离自检那条",
     lambda p: _replace_fn(p, C5_BODY, C5_BODY + "\n    pass  # 变异 C5:整条判据没了\n",
                           C5_END), True, (CRIT,)),
]


def _write_all_gone(path: pathlib.Path) -> None:
    """把 5.1 那张表的 7 行全删掉(其余正文留着,免得文件变成别的东西)。"""
    src = path.read_text(encoding="utf-8")
    keep = [ln for ln in src.splitlines() if not ln.startswith("| **G")]
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
    # 覆盖变异 C1–C5 改的是 `tests/test_protocol_doc_gate_table_matches_code.py`,
    # **是 .py** —— 语法守卫在这里是真需要的,不是形式。r107 栽过一次:
    # 我按「r107 只写 .md」把整段删了,结果覆盖变异没处可写。
    # **同一个 `_write_checked` 承接两类目标时,守卫得按后缀分支,不能整段删。**
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
        print(f"\n=== r108 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:22s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr108 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
