"""r80:判据不许拿源码文本当子串匹配

## 这轮的实测结论(不是推测)

r79 逮到「一行注释喂饱 `in inspect.getsource(...)`」。r80 把同一类扫描
扩到整个 `tests/`,结果是 **7 处 / 3 个文件** —— 种子里写的「13 个文件」
被实测推翻:

    tests/test_round_commit_audit.py:206,209     get_source_segment(原文含注释)
    tests/test_watcher_new_count.py:421,426,458  unparse(含字符串字面量)
    tests/test_cli_limit_honesty.py:258(×2)      unparse(查的是源码拼写)

每处都先做了注入证明(删真调用 + 注释/字面量喂串),结论分三种:

| 站点 | 注入后 | 结论 |
|---|---|---|
| round_commit_audit 206/209 | 真调用换成 `start = 0.0`,同函数加一行 `# prev.get("started_at")` → **PASSED,假绿** | 真洞(同文件 2 条行为测试兜住,所以整文件没全绿) |
| watcher 421 | 契约表派生换手抄 5 元组(**行为等价**)+ 字面量喂串 → **PASSED,假绿** | 真洞(被 `test_run_target_has_no_handwritten_asset_table_tuple` 兜住) |
| watcher 426 | 同上 | 真洞 |
| watcher 458 | 同上,但喂串放在**别的**语句里 → 正确 FAIL | **否定结果**:串必须落在它检查的那条赋值内部,喂不进去 |
| cli_limit_honesty 258 | 注释喂不进去(注释不是 `JoinedStr` 节点) | 不同类,但同样改掉,好让这条守卫不用开例外名单 |

## 为什么守卫要 fail-closed

判别「文本源」的那一步(被摘取的到底是代码节点还是一段文案)在静态
分析里是**半可判**的:真实写法几乎都是 `ast.unparse(n)`,而 `n` 是推导式
变量,光看 AST 认不出它是 `JoinedStr`。

所以这里不做这个判别,改成**从宽**:只要比较的一边来自取文本的入口
(`unparse` / `get_source_segment` / `inspect.getsource`,或绑定到它们的
名字),另一边是静态字符串字面量,就算命中。宁可逼人改成结构判定,
也不留一个「看起来是文案检查、其实是源码子串检查」的口子。

## 这条判据不许恒真

只写「一处都没有」的守卫,检测器整个坏掉也照样绿。所以下面配了
**正控制组**(合成的漏洞形状必须被抓到)和**负控制组**(干净的写法
必须放行),检测器自己出问题时它们先红。
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"

# 三个「把代码变成文本」的入口
_TEXT_FUNCS = {"unparse", "get_source_segment", "getsource"}


def _call_name(node):
    """这次调用叫什么(认 `f(...)` 和 `mod.f(...)` 两种写法)"""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_static_str(node) -> bool:
    """静态字符串:常量,或一个插值都没有的 f-string"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return not any(isinstance(v, ast.FormattedValue) for v in node.values)
    return False


def _is_text_source(node, bound) -> bool:
    """这个表达式是不是「从代码节点上摘下来的文本」

    `bound` 是「绑定到取文本调用的名字」——因为真实写法几乎都是
    `seg = ast.get_source_segment(src, fn)` 然后下一行再拿 `seg` 比。
    """
    if _call_name(node) in _TEXT_FUNCS:
        return True
    for n in ast.walk(node):
        if _call_name(n) in _TEXT_FUNCS:
            return True
        if isinstance(n, ast.Name) and n.id in bound:
            return True
    return False


def code_text_sites(source: str) -> list[tuple[int, str]]:
    """找出「拿代码当文本、再和字面量比」的比较

    返回 `[(行号, 该比较的还原)]`。只认比较的两端:报错文案里
    附带一个 `unparse(...)` 是好的(那是给人看的),不算。
    """
    tree = ast.parse(source)
    bound: dict[str, ast.AST] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and _call_name(n.value) in _TEXT_FUNCS:
            for t in n.targets:
                if isinstance(t, ast.Name):
                    bound[t.id] = n.value
    hits: list[tuple[int, str]] = []
    for cmp in ast.walk(tree):
        if not isinstance(cmp, ast.Compare):
            continue
        operands = [cmp.left, *cmp.comparators]
        if (any(_is_text_source(o, bound) for o in operands)
                and any(_is_static_str(o) for o in operands)):
            hits.append((cmp.lineno, ast.unparse(cmp)))
    return hits


def _scan_dir(root: pathlib.Path) -> list[str]:
    out = []
    for f in sorted(root.glob("*.py")):
        for lineno, text in code_text_sites(f.read_text(encoding="utf-8")):
            out.append(f"{f.relative_to(REPO)}:{lineno}: {text}")
    return out


# ── 正控制组:这些形状必须被抓到 ──
#
# 前三段是**修复前的原文逐字拷贝**(r79 状态),不是照着检测器写的合成样本。
# 合成样本只能证明「检测器认得自己写的形状」;真实代码才能证明它认得
# 「真出过事的形状」。

_R79_ROUND_COMMIT_AUDIT = '''
def test_audit_window_is_since_the_previous_round_started():
    """窗口的起点必须是**上一轮 started_at**,不是上一轮 finished_at"""
    import ast
    src = (REPO / "arl_lite" / "devloop" / "protocol.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "audit_prev_round_commits")
    seg = ast.get_source_segment(src, fn)
    assert 'prev.get("started_at")' in seg, (
        "窗口起点用的不是上一轮的 started_at"
    )
    assert 'prev.get("finished_at")' not in seg, (
        "窗口起点用了 finished_at —— 会把「跑完之后才提交」判成异常"
    )
'''

_R79_WATCHER_NEW_COUNT = '''
def test_both_asset_list_reads_come_from_the_contract_table():
    """数新增的清单和逐类检测的清单,来源必须是 `Monitor._ASSET_TABLES`"""
    tree = ast.parse((REPO / "arl_lite" / "core" / "watcher.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    src = ast.unparse(fn)
    assert "Monitor._ASSET_TABLES" in src, (
        "_run_target 里没引用契约表 —— 资产清单从别处来了")
    loops = [n for n in ast.walk(fn)
             if isinstance(n, ast.For)
             and "Monitor._ASSET_TABLES" in ast.unparse(n.iter)]
    assert loops, (
        "变更检测那个循环不再从契约表取 —— 它和数新增的清单会各自漂移")
    assigns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) == "asset_tables" for t in n.targets)]
    assert len(assigns) == 1, f"asset_tables 被赋值 {len(assigns)} 次"
    assert "Monitor._ASSET_TABLES" in ast.unparse(assigns[0].value), (
        f"asset_tables 不是从契约表派生的:{ast.unparse(assigns[0].value)}")
'''

_R79_CLI_LIMIT_HONESTY = '''
def test_monitor_changes_no_longer_handwrites_its_own_notice():
    """`cmd_monitor_changes` 里不该再有手写的总数文案"""
    tree = ast.parse((REPO / "arl_lite" / "cli.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "cmd_monitor_changes")
    handmade = [n.value for n in ast.walk(fn)
                if isinstance(n, ast.JoinedStr)
                and " of " in ast.unparse(n) and "{total}" in ast.unparse(n)]
    assert not handmade, (
        f"cmd_monitor_changes 里还有手写的总数文案:{handmade}")
'''

# r79 实测:这三段原文一共正好 7 处命中
_REAL_HISTORY = {
    "r39 的 audit 窗口判定": (_R79_ROUND_COMMIT_AUDIT, 2),
    "r52 的资产表来源判定": (_R79_WATCHER_NEW_COUNT, 3),
    "r56 的手写总数文案判定": (_R79_CLI_LIMIT_HONESTY, 2),
}

# 再补几种没在历史里出过的形状,免得检测器只背下了这三段
_MUST_FLAG = {
    "inspect.getsource(r79 原地那条)": '''
def t(fn):
    assert "q.recover_stale(" in inspect.getsource(fn)
''',
    "取文本的 f-string 入参(推导式里的文案检查)": '''
def t(fn):
    bad = [n for n in walk(fn)
           if isinstance(n, ast.JoinedStr) and " of " in ast.unparse(n)]
    assert not bad
''',
    "反向的 not in 也算": '''
def t(src, fn):
    assert 'prev.get("finished_at")' not in ast.get_source_segment(src, fn)
''',
}


# ── 负控制组:这些写法必须放行 ──

_MUST_PASS = {
    "AST 节点判定(结构检查的正确写法)": '''
def t(fn):
    refs = [n for n in ast.walk(fn)
            if isinstance(n, ast.Attribute) and n.attr == "_ASSET_TABLES"
            and isinstance(n.value, ast.Name) and n.value.id == "Monitor"]
    assert refs
''',
    "unparse 只出现在报错文案里(那是给人看的)": '''
def t(fn):
    assert len(fn.body) == 1, f"不对:{ast.unparse(fn)}"
''',
    "读 f-string 自己的字面片段(r80 改完的 cli_limit_honesty)": '''
def t(n):
    text = "".join(v.value for v in n.values
                   if isinstance(v, ast.Constant) and isinstance(v.value, str))
    assert " of " in text
''',
    "拿字符串比字符串": '''
def t(it):
    assert it.status == "pending"
''',
    "比较的是数据结构字段,里面恰好有表格名": '''
def t(r):
    assert "hosts" in r.promotable_fields
''',
    # r80 普查时发现:全库有 8 处「读源码」其实读的是 markdown 或运行时输出,
    # 那是按行为断言,本来就该用字符串。守卫不能把它们也扫进来。
    "读 markdown 查一个词(不是源码)": '''
def t(tmp):
    doc = (tmp / "PERF_BASELINE.md").read_text(encoding="utf-8")
    assert "规模档" in doc
''',
    "查运行时输出的一个词(不是源码)": '''
def t(capsys):
    out = capsys.readouterr().out
    assert "规模档:**small**" in out
''',
}


def _all_hole_shapes() -> dict[str, str]:
    """正控制组全集:真实历史片段 + 补充形状,统一摊平成 `名字 → 源码`"""
    out = {name: snippet for name, (snippet, _) in _REAL_HISTORY.items()}
    out.update(_MUST_FLAG)
    return out


# ── 判据 ──

def test_no_test_file_matches_code_text_against_a_literal():
    """整个 tests/ 里不该再有「拿源码文本和字面量比」的判据"""
    sites = _scan_dir(TESTS)
    assert not sites, (
        "这些判据在拿源码文本当子串匹配 —— 注释/字符串字面量就能喂饱它们:\n  "
        + "\n  ".join(sites)
        + "\n改成 AST 节点判定(判据自己得先能被别人骗过才谈得上守别人)"
    )


def test_detector_flags_every_known_hole_shape():
    """检测器对已知漏洞形状必须**有反应**——否则上面那条是恒真的"""
    missed = {}
    for name, snippet in _all_hole_shapes().items():
        if not code_text_sites(snippet):
            missed[name] = snippet.strip().splitlines()[-1].strip()
    assert not missed, (
        f"检测器漏掉了 {len(missed)} 种已知漏洞形状:{sorted(missed)}\n"
        + "\n".join(f"  {k} → {v}" for k, v in missed.items())
    )


def test_detector_finds_the_exact_historical_site_count():
    """对**真实历史代码**必须报出实测过的那几个数,一个不多一个不少

    r80 实测 r79 状态下是 2 / 3 / 2 共 7 处。报少了说明检测器漏,
    报多了说明它把干净写法也当漏洞 —— 两个方向都要锁。
    """
    wrong = {}
    for name, (snippet, want) in _REAL_HISTORY.items():
        got = len(code_text_sites(snippet))
        if got != want:
            wrong[name] = f"期望 {want} 处,实得 {got} 处"
    assert not wrong, wrong


def test_detector_flags_exactly_the_shapes_it_should():
    """正控制组每一段都必须被逮到,且要能说出是哪一行"""
    for name, snippet in _all_hole_shapes().items():
        hits = code_text_sites(snippet)
        assert hits, f"没抓到:{name}"
        assert all(lineno > 0 for lineno, _ in hits), f"行号不对:{name}"


def test_detector_lets_clean_shapes_through():
    """干净的写法不能被误伤,否则这条守卫会逼人写出更糟的判据"""
    wrong = {}
    for name, snippet in _MUST_PASS.items():
        hits = code_text_sites(snippet)
        if hits:
            wrong[name] = hits
    assert not wrong, (
        f"检测器误伤了 {len(wrong)} 种干净写法:{wrong}\n"
        "守卫比它守的东西还严,就会有人绕过它"
    )


def test_detector_actually_runs_over_the_real_suite():
    """守住「扫描器真的扫到了东西」——扫不到文件的守卫等于没写"""
    files = sorted(TESTS.glob("*.py"))
    assert len(files) > 50, f"只扫到 {len(files)} 个测试文件,扫描范围不对"
    parsed = 0
    for f in files:
        ast.parse(f.read_text(encoding="utf-8"))
        parsed += 1
    assert parsed == len(files), "有测试文件连解析都过不去"


def test_the_three_fixed_files_no_longer_use_code_text():
    """点名这轮改过的三个文件 —— 它们曾经各占一处命中"""
    for name in ("test_round_commit_audit.py",
                 "test_watcher_new_count.py",
                 "test_cli_limit_honesty.py"):
        f = TESTS / name
        assert f.exists(), f"{name} 不见了"
        assert not code_text_sites(f.read_text(encoding="utf-8")), (
            f"{name} 里还有源码子串判定")
