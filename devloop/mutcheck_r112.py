"""r112 变异测试:验「三个条件成立时播种返回 0」和「模块 docstring 不许撒谎」。

## 主题

r112 上轮结论是「backlog 38 条全完成 + 队列 0 pending,下一轮必然是 barren round」。
本轮**先实测再改**:真实仓库上 `seed_if_empty(112)` 返回 **0**。而那一刻
`arl_lite/devloop/queue.py` 里有**两处说法与代码相反**的文本,都在说自己那层:

| 位置 | 原话 | 实测 |
|---|---|---|
| 模块 docstring 不变式 #4 | 「`seed_if_empty()` **永远**能产生至少 1 条新 item(协议不变量)」 | 返回 0 |
| `seed_if_empty` Tier 2 注释 | 「删除之后不变式 #4 **依然成立**:本层是保底,**永远能产出**」 | 返回 0 |
| `_seed_fallback` docstring | 「本层返回空,队列会空掉,是对眼下确实没有维护活要干的诚实回答」 | 诚实 |

**同一份文件里三种说法并存**。读 Tier 2 注释的人会以为队列永远不会空。

## 为什么会 barren:三个条件同时成立

1. `backlog.md` 里每条都打了 ✅ → Tier 1 跳过
2. 三条周期项的产物报告都在 90 天内 → 没到期,不提
3. 兜底信号 `no-due-maintenance-review` 被人 **drop** 了 → `drop` 是**永久开关**
   (`test_a_dropped_signal_is_never_reproposed` 钉着)。当年 drop 它时留下的理由
   自己就写着「drop 掉不解决根因,根因是 `ensure_next_step` 播的就是这种条目,
   已另登记为真活」—— 那些「真活」r103–r111 做完之后,状态**原样回来了**。

所以它**不会自愈**。引擎的处理是如实停下并说「没有下一步」,而不是编一条假活出来。

## 两条判据,各钉一个方向

1. **判据 A(行为)**:三条件成立时 `seed_if_empty` 返回 0,且队列里除了那条
   dropped 信号之外**一条都不许多**。这让那句错话有了一个能复现的反例 ——
   否则「把注释改得更圆」和「把注释删掉」都能让文本判据变绿。
2. **判据 B(文档)**:模块 docstring 不许声称「永远能产出」,而且**只删错话不算
   修好** —— 它必须留下实测到的返回 0、点名 drop、说清 drop 是永久的。

## 为什么判据 B 只查模块 docstring,不查那段注释

那段是 `#` 注释,查它就得回退到源码文本匹配,而本项目已经因为这个栽过一次
(`test_source_checks_are_structural.py` 钉着)。`ast.get_docstring()` 是真结构化
检查:docstring 是语法节点。代价说清楚了:那段注释里「永远能产出」是被**引用的
反面教材**,判据若改成扫注释会先被自己的引用绊倒。这是选择,不是漏洞。

## 变异清单

实现变异(期望全被杀):
  M1 信号无视 drop → 判据 A 红(第二轮播种返回 1)
  M2 保底层随便编一条假条目 → 判据 A 红(返回 1,且队列多一条 `maintenance-todo`)
  M3 模块 docstring 改回原谎话 → 判据 B 红
  M4 只删掉「drop 是永久开关」,不写真实条件 → 判据 B 红(证明 B 不是只查错话)

覆盖变异(期望全存活,拆判据):
  C1 拆判据 A
  C2 拆判据 B

## M2 为什么要单独来一条

M1 是「无视 drop,重复提出**同一条**信号」(id 撞车 → 被 `_disambiguate`
改名成 `-r1`)。M2 是第 16 轮删掉的**整层假活制造机**的原样复活:信号被
drop 了就**编一条新的**,id 是队列里从来没有过的 `maintenance-todo`。

## 实测修正:两条都被**同一条断言**杀掉

写这条时我以为 M2 会红在「队列里不许凭空多出条目」那条上,实测**不是**:
判据 A 里 `assert got == 0` 写在前面,M2 先撞它就停了。所以两条变异
目前共用同一个防线点。

**那为什么还留着 M2?** 因为它验的东西 M1 验不到:M1 编出来的 id 是
`no-due-maintenance-review-r1`,从命名上一眼就看出是同一件事的副本;
M2 编出来的 `maintenance-todo` 是**队列里从来没有过的条目** ——
「为了不空而编新活」正是第 16 轮那场灾难的本体。将来如果有人调整
判据 A 里两条断言的顺序,M2 才会落到第二条上;在那之前它是被
`got == 0` 挡住的。**这一点写下来,免得下一个人以为它验到了第二条。**

## M4 为什么必须单独来一条

它是判据 B 的**反例方向**:一条只把谎话删掉、什么都不补的修改,能让
「不许出现谎话」那半变绿。如果判据 B 只查禁词,M4 就是它放行的合法逃逸。
所以 B 必须同时要求留下真实条件 —— M4 验的就是这半。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量 —— r109 写了 `.strip()`、r110 写了 `A_TAG`,两次都让
`ast.literal_eval` 抛 `ValueError`,判据在报错而不是在校验。
old 侧必须**整句**配平。纯插入 must_not 留空、纯删除 must_have 留空。
带 `_write_checked` 的 modern 脚本 old 侧必须整句配平。
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "arl_lite" / "devloop" / "queue.py"
CRIT = REPO / "tests" / "test_devloop_seed_truthfulness.py"

TARGET = ["tests/test_devloop_seed_truthfulness.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")

# ── M1:兜底信号无视 drop ──
A1_COND = "if not out and self._should_propose_signal():"
M1_COND = "if not out:  # 变异 M1:保底层无条件产出,无视 drop 是永久开关"

# ── M2:保底层编一条新假条目(第 16 轮删掉的整层假活制造机) ──
A2_ELIF = (
    "        elif not out:\n"
    "            log.info(\n"
    '                "queue._seed_fallback: 无到期项,且队里已有复查信号"\n'
    '                "(待确认或已被明确 drop)—— 不重复提出"\n'
    "            )\n"
)
M2_ELIF = (
    "        elif not out:\n"
    "            out.append(Item(\n"
    '                id="maintenance-todo",\n'
    '                title="随便找点事做",\n'
    '                detail="保底层必须总能产出,否则队列会空 —— "\n'
    '                "这正是模块 docstring 不变式 #4 的说法。",\n'
    "                priority=P2,\n"
    '                kind="research",\n'
    '                verify="true",\n'
    '                tags=["maintenance"],\n'
    "                created_round=round_no,\n"
    "            ))\n"
)

# ── M3:模块 docstring 改回原谎话 ──
A3_DOC = (
    "    4. 自愈:seed_if_empty() 在「还有活可干」时产出新 item。\n"
    "       **它不是无条件保证** —— r112 实测它返回过 0:backlog 全部完成 +\n"
    "       三条周期项都没到期 + 兜底信号被人 drop。`drop` 是永久开关\n"
    "       (人明确说「我不要这条」),所以这一状态**不会自愈**。\n"
)
M3_DOC = "    4. 自愈:seed_if_empty() 永远能产生至少 1 条新 item(协议不变量)\n"

# ── M4:只删掉「drop 是永久开关」,不写真实条件(判据 B 的反例方向) ──
A4_DROP = (
    "兜底信号被人 drop。`drop` 是永久开关\n"
    "       (人明确说「我不要这条」),所以这一状态**不会自愈**。\n"
)
M4_DROP = "兜底信号被人 drop。\n"

# ── 覆盖变异的边界锚点(照文件里的**实际顺序**写) ──
C1_BODY = "def test_seeding_can_return_zero_when_three_conditions_all_hold():"
C2_BODY = "def test_module_docstring_must_not_claim_an_invariant_it_cannot_hold():"
C1_STUB = (
    "def test_seeding_can_return_zero_when_three_conditions_all_hold():\n"
    "    pass  # 变异 C1:整条判据没了\n"
)

CLAIMS = {
    "M1-信号无视drop": (
        ["# 变异 M1:保底层无条件产出,无视 drop 是永久开关"],
        ["if not out and self._should_propose_signal():"]),
    "M2-保底层编一条新假条目": (
        ['id="maintenance-todo"'], ["不重复提出"]),
    "M3-模块docstring改回谎话": (
        ["永远能产生至少 1 条"], ["不是无条件保证"]),
    "M4-只删谎话不留真实条件": (
        [], ["是永久开关"]),
    "C1-拆判据A": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆判据B": (["    pass  # 变异 C2:整条判据没了"], []),
}

MUTANTS = [
    ("M1-信号无视drop", lambda p: _apply(p, A1_COND, M1_COND), False, (SRC,)),
    ("M2-保底层编一条新假条目", lambda p: _apply(p, A2_ELIF, M2_ELIF), False, (SRC,)),
    ("M3-模块docstring改回谎话", lambda p: _apply(p, A3_DOC, M3_DOC), False, (SRC,)),
    ("M4-只删谎话不留真实条件", lambda p: _apply(p, A4_DROP, M4_DROP), False, (SRC,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆判据A", lambda p: _replace_fn(p, C1_BODY, C1_STUB, C2_BODY),
     True, (CRIT,)),
    ("C2-拆判据B", lambda p: _stub_tail(p, C2_BODY), True, (CRIT,)),
]


def _stub_tail(path: pathlib.Path, start: str) -> None:
    """把**文件末尾**那个函数整条换成 `pass`。

    判据 B 是这个文件里的最后一个函数,后面没有可作边界的 `def`。
    早先几版 mutcheck 靠"下一个 def"当结束标记,这里只能截到文件尾 ——
    所以单独写一个,而不是给 `_replace_fn` 塞一个空 `end` 让它
    `index("")` 命中全文开头,把整个文件替换掉。
    """
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    i = src.index(start)
    _write_checked(path, src[:i] + start + "\n    pass  # 变异 C2:整条判据没了\n")


def _assert_claims_are_all_literals() -> None:
    """CLAIMS 里只许有字面量 —— 方法调用和名字引用都不行

    r109 写了 `[A_PRED.strip()]`,r110 写了 `[A_TAG]` / `[A_MARK]`,两次都是
    `ast.literal_eval` 抛 `ValueError`,那些判据于是**全在报错而不是在校验**。

    ## 它自己第一版是**恒真**的

    首版写的是 `getattr(node.value, "elts", [])` —— 而 `ast.Dict` 根本没有
    `.elts`(在 `.keys` / `.values` 上),`getattr` 返回 `[]`,循环一次没跑,
    `offenders` 永远空,断言永远绿。**「取不到就当空」和 r83 那次
    `getattr(node, "blocking", None)` 是同一个错**。

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
        f"那些判据会**在报错**而不是在校验 —— 而门禁把它算成回归。")


def _write_checked(path: pathlib.Path, out: str) -> None:
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    # 覆盖变异 C1/C2 改 `.py`,实现变异 M1–M4 也改 `.py`;按后缀分支,
    # r107 栽过一次「按只写 .md 写分支」结果覆盖变异没处可写。
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


def main() -> int:
    _assert_claims_are_all_literals()
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r112 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:22s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr112 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
