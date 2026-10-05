"""r110 变异测试:验「条目不许被劈成两行」和「索引标题不许声称不存在的轮次」。

## 主题

r109 收尾时点开那条 `backlog-index-stops-at-r52` 的条目,量出三件事。

### 一、条目被劈成两行,下半截 974 字符对所有判据隐形

r106 写这条时把一行写成了两行:L88 是正常的 `标题 | detail | verify`,
L89 是它的下半截(**不带 `- [P` 前缀**)。后果:

  - `_BACKLOG_LINE` 匹配不上 L89 → 不参与播种
  - 不参与任何一致性判据
  - 连 r109 新加的「每行都得解析」都看不见它 —— 那条只查 `- [` 开头的行

而 L89 里装的恰恰是这条待办的**真正做法**「在 backlog.md 顶部写清三条记录
指向哪」和**第二个 verify**。读的人以为它是 L88 的一部分。

**r109 的判据挡住了「整个条目解析不了」,挡不住「条目只写了一半」。**

### 二、verify 要求的正是 detail 拒绝的做法

L88 的 verify:backlog.md 里 ≥45 个不同的 `rNN 完成` 标记(NN>=53),实测 6,
**红**。而 L89 白纸黑字写着「**真要做的不是「补五十轮正文」**(那会写出一份
commit message 的劣质副本)」。所以想让它变绿,唯一办法就是去做它说不要做的
事 —— **这条待办按它自己的处方永远做不绿**。

换成与处方一致的 verify(检查文件头是否写清三层记录各在哪),并真的写那个文件头。

### 三、ROUND_INDEX.md 的标题声称「r53 起」,而 git 里 r53–r69 根本没有轮次提交

实测 `git log --format=%s`:带 `rNN:` 前缀的提交是 **r70–r109**。标题是照着
「devloop 从 r53 开始」想的,不是照着 git 里实际有什么查的。

已有的 `test_the_index_does_not_claim_to_cover_rounds_that_do_not_exist` 查
的是**行**,标题里那个 r53 不是一行,**压根不在它的检查范围里**。

## 变异清单

实现变异(期望全被杀):
  M1 把一条条目劈成两行            → 判据 1(不许劈半)红
  M2 优先级标签 `[P1]` → `[1]`     → 判据 2(每行都得解析)红
  M3 拿掉一条 ✅                    → 判据 3(done 必带标记)红
  M4 文件头标记块整个删掉          → 判据 4(起始轮次一致)红
  M5 ROUND_INDEX 标题改回「r53 起」 → 判据 4 红
  M6 backlog 文件头起始轮次改成 75  → 判据 4 红(三个说法分叉)
  M7 条目区整个清空                → 三条前置条件红

覆盖变异(期望全存活,拆判据):
  C1 拆「条目不许劈半」   ← M1 的唯一防线
  C2 拆「起始轮次一致」   ← M4/M5/M6 的唯一防线
  C3 拆「每行都得解析」   ← M2 的唯一防线
  C4 拆「done 必带标记」  ← M3 的唯一防线

## M1 与 M2 的区别值得单说

M2 是「整行解析不了」,M1 是「行还是好的,但**只写了一半**」。r109 加判据时
以为自己覆盖了前一种形状,实测它对 M1 完全无感 —— 判据只查 `- [` 开头,
而劈开的下半截什么都不带。**每条判据都有一个「不在它遍历范围内」的死角,
而死角不会报错。**

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用、方法调用或下标。纯插入 must_not 留空、
纯删除 must_have 留空。old 侧必须**整句**配平(带 `_write_checked` 的现代约定)。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT_Q = REPO / "tests" / "test_devloop_queue_invariant.py"
CRIT_R = REPO / "tests" / "test_round_index_stays_current.py"
BACKLOG = REPO / "devloop" / "backlog.md"
INDEX = REPO / "devloop" / "ROUND_INDEX.md"

TARGET = ["tests/test_devloop_queue_invariant.py",
          "tests/test_round_index_stays_current.py"]

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


# ── M1:把一条条目劈成两行(下半截不带 `- [P` 前缀) ──
# 这行**不能**以 `#` / `- ` / `>` / `---` 开头 —— 判据把那些当小标题、
# 列表、引用、分隔线跳过,首版这里写了 `# 变异 M1:...`,于是这条变异
# 造出来的形状判据压根不查,变异「存活」的原因会是我自己躲开了检查范围,
# 而报告出来却像「判据抓不住劈半」—— **假结论比没有结论更贵**。
A_ENTRY_HEAD = "- [P1] change: 变更风暴时被 200"
M1_SPLIT = "这行接在一条条目后面却不是条目(变异 M1:条目被劈成了两行)"


# ── M2:优先级标签写坏 ──
A_TAG = "- [P1] change: 变更风暴时被 200"

# ── M3:拿掉一条 ✅ ──
A_MARK = ("✅ r109 补标(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
          "且丢弃条数可见(watcher.py:46-109,478))· ")
M3_GONE = ("完成标记(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
           "且丢弃条数可见(watcher.py:46-109,478))· ")

# ── M4:文件头标记块整个删掉 ──
A_HEAD = "<!-- devloop:where-records-live -->\n## 每一轮的过程记录在哪"
M4_GONE = "# 变异 M4:文件头整个删掉了\n## 每一轮的过程记录在哪"

# ── M5:ROUND_INDEX 标题改回「r53 起」 ──
A_TITLE = "# 轮次索引(r70 起,不是 r53 —— 标题原来写错了,r110 按实测改)"
M5_TITLE = "# 轮次索引(r53 起)"

# ── M6:backlog 文件头的起始轮次与索引分叉 ──
A_START = "带 `rNN:` 前缀的轮次提交**从 r70 起**"
M6_START = "带 `rNN:` 前缀的轮次提交**从 r75 起**"

# ── 覆盖变异的边界锚点(照文件里的**实际顺序**写,不是照脑子里想的顺序) ──
# queue_invariant 实际顺序:
#   real_backlog → looks_like_a → no_split → _real_backlog_entries
#   → done_marked → done_marker_predicate → id_depends_on_position
# round_index 里新判据是**最后一个**,C2 用 `_replace_fn_to_eof`。
C1_BODY = "def test_no_backlog_entry_is_split_across_two_lines():"
C1_END = "def _real_backlog_entries() -> list[dict]:"
C2_BODY = "def test_the_titles_starting_round_is_the_first_round_that_actually_exists():"
C3_BODY = "def test_every_line_that_looks_like_a_backlog_entry_actually_parses():"
C3_END = "def test_no_backlog_entry_is_split_across_two_lines():"
C4_BODY = "def test_done_work_is_marked_done_in_the_human_facing_index():"
C4_END = "def test_the_done_marker_predicate_matches_what_seeding_actually_honours():"

CLAIMS = {
    # **全部字面量,一个名字引用都不许有。**
    # 写这一段的上一轮(r109)我先犯了 `[A_PRED.strip()]`(方法调用)、
    # 这一轮又犯了 `[A_TAG]` / `[A_MARK]` / `[A_TITLE]`(名字引用)——
    # 两次都是 `ast.literal_eval` 遇到 Call / Name 节点抛
    # `ValueError: malformed node`,那 6 条判据于是**全在报错而不是在校验**。
    # **在同一个文件的 docstring 里写下「只写字面量」,然后在下面违反它,
    # 已经不是粗心,是那行注释没有约束力。** 所以这里逐条写成字面量,
    # 并且下面那条 `_assert_claims_are_all_literals` 把这条钉成判据。
    "M1-条目劈成两行": (
        ["这行接在一条条目后面却不是条目(变异 M1:条目被劈成了两行)"], []),
    "M2-优先级标签写坏": (["- [1] change: 变更风暴时被 200"],
                          ["- [P1] change: 变更风暴时被 200"]),
    "M3-拿掉一条完成标记": (
        ["完成标记(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
         "且丢弃条数可见(watcher.py:46-109,478))· "],
        ["✅ r109 补标(队列记 done_round=56;`CHANGE_RECORD_CAP=200` "
         "且丢弃条数可见(watcher.py:46-109,478))· "]),
    "M4-文件头整个删掉": (
        ["# 变异 M4:文件头整个删掉了\n## 每一轮的过程记录在哪"],
        ["<!-- devloop:where-records-live -->\n## 每一轮的过程记录在哪"]),
    "M5-标题改回r53起": (["# 轮次索引(r53 起)"],
                        ["# 轮次索引(r70 起,不是 r53 —— 标题原来写错了,r110 按实测改)"]),
    "M6-文件头起始轮次分叉": (
        ["带 `rNN:` 前缀的轮次提交**从 r75 起**"],
        ["带 `rNN:` 前缀的轮次提交**从 r70 起**"]),
    "M7-条目区整个清空": ([], ["- [P1] change: 变更风暴时被 200"]),
    "C1-拆不许劈半那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆起始轮次一致那条": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆每行都得解析那条": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆done必带标记那条": (["    pass  # 变异 C4:整条判据没了"], []),
}

MUTANTS = [
    ("M1-条目劈成两行", lambda p: _split_entry(p), False, (BACKLOG,)),
    ("M2-优先级标签写坏", lambda p: _apply(p, A_TAG,
                                     A_TAG.replace("- [P1]", "- [1]")), False, (BACKLOG,)),
    ("M3-拿掉一条完成标记", lambda p: _apply(p, A_MARK, M3_GONE), False, (BACKLOG,)),
    ("M4-文件头整个删掉", lambda p: _apply(p, A_HEAD, M4_GONE), False, (BACKLOG,)),
    ("M5-标题改回r53起", lambda p: _apply(p, A_TITLE, M5_TITLE), False, (INDEX,)),
    ("M6-文件头起始轮次分叉", lambda p: _apply(p, A_START, M6_START), False, (BACKLOG,)),
    ("M7-条目区整个清空", lambda p: _write_all_gone(p), False, (BACKLOG,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆不许劈半那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT_Q,)),
    ("C2-拆起始轮次一致那条",
     lambda p: _replace_fn_to_eof(p, C2_BODY,
                                  C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n"),
     True, (CRIT_R,)),
    ("C3-拆每行都得解析那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT_Q,)),
    ("C4-拆done必带标记那条",
     lambda p: _replace_fn(p, C4_BODY, C4_BODY + "\n    pass  # 变异 C4:整条判据没了\n",
                           C4_END), True, (CRIT_Q,)),
]


def _split_entry(path: pathlib.Path) -> None:
    """把一条真条目劈成两行:上半截保留,下半截变成不带 `- [P` 前缀的裸行。

    这正是 r110 在真实文件里发现的形状 —— 上半截完全合规,下半截对所有
    判据隐形。变异必须**真的**造出那个形状,否则测的是「判据认不认识我
    编的字符串」,不是「判据能不能抓住 r110 查出的那一种缺陷」(r107 的教训)。
    """
    src = path.read_text(encoding="utf-8")
    if src.count(A_ENTRY_HEAD) != 1:
        raise AssertionError(
            f"变异锚点在 {path.name} 出现 {src.count(A_ENTRY_HEAD)} 次(必须恰好 1 次)")
    i = src.index(A_ENTRY_HEAD)
    j = src.index("\n", i)
    _write_checked(path, src[:j] + "\n" + M1_SPLIT + src[j:])


def _write_all_gone(path: pathlib.Path) -> None:
    """把 backlog.md 里所有 `- [P…]` 条目行删掉,其余(文件头、候选清单)留着。"""
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
    # 覆盖变异 C1–C4 改的是两个 `tests/test_*.py`,**是 .py** —— 语法守卫在这里
    # 是真需要的,不是形式。r107 栽过一次:按「r107 只写 .md」把整段删了,结果
    # 覆盖变异没处可写。**同一个 `_write_checked` 承接几类目标时,守卫得按后缀
    # 分支,不能整段删。**
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


def _replace_fn_to_eof(path: pathlib.Path, start: str, stub: str) -> None:
    """替换「从 `start` 到文件末尾」。

    r110 新判据是 `test_round_index_stays_current.py` 里的**最后一个**函数,
    后面没有可当结束标记的 `def` —— 硬凑一个标记只会写出「标记写反了」
    那种和判据无关的 BAD-MUTANT(r109 栽过)。这里单独给一条路径。
    """
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    i = src.index(start)
    _write_checked(path, src[:i] + stub + "\n")


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


def _assert_claims_are_all_literals() -> None:
    """CLAIMS 里只许有字面量 —— 方法调用和名字引用都不行

    r109 写了 `[A_PRED.strip()]`,r110 写了 `[A_TAG]` / `[A_MARK]` / `[A_TITLE]`,
    两次都是 `ast.literal_eval` 遇到 Call / Name 节点抛
    `ValueError: malformed node or string`。那个异常会让
    `test_mutcheck_claims_match_their_names.py` 的 6 条判据**全在报错而不是在校验**,
    而门禁把它算成「回归」。

    为什么值得在这里自查:那 6 条判据**确实抓到了**,所以不需要这条断言来防漏 ——
    需要它防的是**第二轮**。r109 我在 docstring 里写下「CLAIMS 只写字面量」,
    r110 在同一个文件的同一个 docstring 里又写了一遍,然后又违反它。
    **同一行注释连续两轮没有约束力,那它就不是规则,只是装饰。**
    这条断言让它至少在跑脚本时就响,而不是等 7 分钟的门禁。

    ## 它自己第一版是**恒真**的

    首版写的是 `getattr(node.value, "elts", [])` —— 而 `ast.Dict` **根本没有
    `.elts`**(它的条目在 `.keys` / `.values` 上),`getattr` 于是返回 `[]`,
    循环一次都没跑,`offenders` 永远是空的,断言永远绿。

    **「用 getattr 取不到就当空」和 r83 那次 `getattr(node, "blocking", None)`
    是同一个错**:取不到值时它不报错,它把「没取到」说成「没有」。一个取不到
    值的兜底默认值,会把「没检查」说成「没有要检查的」。

    所以下面除了正着查,还**反着自查遍历器本身**:CLAIMS 必须非空、必须至少
    含一个字符串字面量。遍历器瞎了的时候这两条会先响,而不是让上面那条
    假装检查过。
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
    # ast.Dict 的条目在 keys / values 上,**不在 elts 上**
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
    # 反向自查:遍历器本身必须真的看得见东西,否则上面那条是恒真的
    assert pairs, "CLAIMS 是空字典 —— 遍历器一次都没跑,上面那些检查是恒真的"
    assert any(isinstance(p, _ast.Constant)
               for _k, v in pairs for s in v.elts for p in s.elts), (
        "CLAIMS 里连一个字符串字面量都没有 —— 遍历器坏了,别信上面那些检查")
    assert not offenders, (
        f"CLAIMS 里出现了非字面量:{offenders}\n"
        f"  `ast.literal_eval` 遇到它们会抛 `ValueError: malformed node`,"
        f"那 6 条判据会**在报错**而不是在校验 —— 而门禁把它算成回归。")


def main() -> int:
    _assert_claims_are_all_literals()
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r110 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:24s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr110 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
