"""r84:截断只能用在**决策之后**,不能用来做决策

## 事故(队列项记的两次)

r68 和 r74 我都把种子前提搞错了,根因是探针用 `[:7]` 之类截断输出,
于是把 stderr 的日志行和 stdout 混读,得出「source status 段没打印」
「没有任何提示」这类**根本不存在的结论**:

  - r68 因此**丢了一条真种子**(risk summary 与 risk top 的口径矛盾是真的)
  - r74 因此差点把一个已有出路的问题当成死路

教训是「要看真实输出就别截断」。但更根本的替代方案更精确,也是这条判据
要钉的那个:**截断本身不是毛病,拿截断后的东西做决策才是毛病。**

## r84 实测:仓库里 0 处真实例,5 处全是正确用法

扫 `tests/` + `devloop/` + `arl_lite/` 全部 `.py`,找「对 stdout/stderr/
readouterr 取有界切片」的地方,共 5 处。逐个读过:

    tests/test_phase4.py:435      fail(f"... | {r.stderr[:100]}")
    tests/test_phase5.py:283      print(f"... | {r.stderr[:80]}")
    tests/test_phase6.py:256      fail(f"export html: {r.stderr[:100]}")
    arl_lite/integrations/nuclei.py:69     log.warning(f"... stderr={proc.stderr[:200]}")
    arl_lite/integrations/portscan.py:160  raise RuntimeError(f"...: {result.stderr[:200]}")

五处的**决策依据**分别是 `returncode`、`returncode`、`returncode and
Path.exists()`、`returncode != 0 and not stdout`、`returncode != 0` ——
截断只出现在**决策之后**拼给人看的文案里。这是正确用法,不修。

所以 r84 在仓库代码上是个**否定结果**:反模式不存在于版本控制里,它发生在
我会话中的一次性探针(shell heredoc)上,那些东西没有留档,事后无法审计。
能做的只有把判据钉住,别让它长进来。

## 判据要能区分这两种用法,否则就等于没写

「文件里不许出现 `[...]`」会误伤上面 5 处正确用法,那样的守卫会被人绕过。
真正的判据是:**那个截断的值,有没有流进一个决定**。

本文实现的判定分两层,都是 AST 上的结构判定,不用文本子串:
  1. 切片节点本身落在 `if`/`while`/`assert` 的 `test` 里(或落在一个
     `Compare` 里而那个 `Compare` 在 `test` 里)
  2. 切片被赋给一个名字,而这个名字**后面**被某个 `test` 引用 ——
     `s = r.stdout[:50]` 然后 `if "x" in s:` 这种形态

## 已知漏网(不假装没有)

跨函数的流(把截断结果 return 出去、由调用方决定)判不了。本文不假装
覆盖它,只把能判的两层钉住,漏网的那一类靠下面两条正控制组提醒。
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
SCAN_DIRS = ("tests", "devloop", "arl_lite")

# 会被截断的「输出」形态
_OUTPUT_ATTRS = {"stdout", "stderr", "out", "err", "readouterr", "text"}

_DECISION_FIELDS = (ast.If, ast.While, ast.Assert)


def _is_bounded_slice(node: ast.AST) -> bool:
    return (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)
            and node.slice.upper is not None)


def _is_output_source(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in _OUTPUT_ATTRS


def _decision_roots(node: ast.AST) -> list[ast.AST]:
    """这个函数里所有「会决定流程」的位置的 `test` 节点"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, _DECISION_FIELDS):
            out.append(n.test)
    return out


def _used_in_decision(slice_node: ast.AST, tree: ast.Module,
                      func: ast.FunctionDef) -> str | None:
    """这个截断有没有流进决策?返回一句话说明,没流进返回 None"""
    for root in _decision_roots(func):
        for n in ast.walk(root):
            if n is slice_node:
                return "直接用在 if/while/assert 的判定里"
    # 一层局部流:`s = r.stdout[:50]` 之后 `s` 出现在某个判定里
    for n in ast.walk(func):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.value is slice_node):
            var = n.targets[0].id
            for root in _decision_roots(func):
                if any(isinstance(x, ast.Name) and x.id == var for x in ast.walk(root)):
                    return f"赋给 {var!r} 之后被判定用到"
    return None


def violations(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for n in ast.walk(fn):
            if not (_is_bounded_slice(n) and _is_output_source(n.value)):
                continue
            why = _used_in_decision(n, tree, fn)
            if why:
                out.append(f"{path.name}:{n.lineno}  {why}")
    return out


def _scan() -> list[str]:
    out: list[str] = []
    for d in SCAN_DIRS:
        for f in sorted((REPO / d).rglob("*.py")):
            out.extend(violations(f))
    return out


# ── 判据 ──

def test_no_probe_decides_on_truncated_output():
    """没有任何判据/实现在**截断之后**拿输出做决策"""
    bad = _scan()
    assert not bad, (
        "这些地方拿截断过的输出做决策了:\n  " + "\n  ".join(bad)
        + "\n截断可以留在提示文案里,不能留在判定里 —— "
        "r68 因此丢了一条真种子,r74 因此差点把有出路的问题当成死路")


def test_the_scan_actually_finds_the_known_truncation_sites():
    """扫描器得真找得到东西,否则上面那条是恒真的

    r84 实测仓库里有 5 处「对输出取有界切片」,它们全都合法(只拼文案),
    所以本条断言的是**扫描器找到了这 5 处**,不是断言它们违规。
    """
    found = []
    for d in SCAN_DIRS:
        for f in sorted((REPO / d).rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if _is_bounded_slice(n) and _is_output_source(n.value):
                    found.append(f"{f.name}:{n.lineno}")
    assert len(found) == 5, (
        f"扫到 {len(found)} 处「对输出取有界切片」,r84 实测是 5 处:{found}\n"
        "数量对不上说明扫描规则变了,上面那条的结论不能再直接采信")


# ---- 正/负控制组:形状变了判据必须跟着变 ----

_POSITIVE = {
    "截断直接进判定": '''
def probe(r):
    if "traceback" in r.stderr[:100]:
        return True
    return False
''',
    "截断赋给变量后进判定": '''
def probe(r):
    head = r.stdout[:50]
    if "found: " in head:
        return True
    return False
''',
    "断言里用截断": '''
def probe(r):
    assert "ERROR" in r.stderr[:80]
''',
    "while 条件里用截断": '''
def probe(r):
    while "retry" in r.stdout[:30]:
        r = again()
''',
}

_NEGATIVE = {
    "决策看 returncode,截断只拼文案": '''
def probe(r):
    if r.returncode != 0:
        print(f"exit={r.returncode} | {r.stderr[:100]}")
    return r.returncode
''',
    "决策看文件存在,截断只拼文案": '''
def probe(r):
    if r.returncode == 0 and Path("/tmp/out.html").exists():
        return "ok"
    return f"export html: {r.stderr[:100]}"
''',
    "决策看另一个字段的完整值": '''
def probe(r):
    return r.stderr.count("ERROR") > 2
''',
    "截断结果只 return 出去,不参与判定": '''
def probe(r):
    return r.stderr[:200]
''',
}


def _classify(src: str) -> list[str]:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "probe.py"
        p.write_text(src, encoding="utf-8")
        return violations(p)


def test_detector_catches_every_known_truncation_shape():
    """**正控制组**:四种「截断进决策」的形态都必须被抓到

    r84 写这条判据时最容易犯的错就是只写第一种,把「先赋值后判定」漏掉 ——
    而那恰恰是真实探针最常见的写法(`head = out[:50]` 之后 `if ... in head`)。
    """
    missed = {n: _classify(s) for n, s in _POSITIVE.items() if not _classify(s)}
    assert not missed, f"检测器漏掉了这些形态:{sorted(missed)}"


def test_detector_does_not_flag_truncation_used_only_for_messages():
    """**负控制组**:截断只用来拼提示文案是正确用法,不能误伤

    r84 实测仓库里那 5 处全是这种。守卫比它守的东西还严,就会有人绕过它。
    """
    wrong = {n: _classify(s) for n, s in _NEGATIVE.items() if _classify(s)}
    assert not wrong, f"检测器误伤了正确用法:{wrong}"


def test_the_two_overturned_conclusions_stay_written_down():
    """r68 / r74 两条被推翻的结论必须留在仓库里,而不是只留在对话里

    对话会滚出去,判据的 docstring 不会。
    """
    src = (REPO / "tests" / "test_probe_truncation_only_after_deciding.py").read_text(
        encoding="utf-8")
    for case in ("r68", "r74"):
        assert case in src, f"{case} 那条被推翻的结论没留在文件里"
    assert "risk" in src, "r68 丢掉的那条真种子(risk 口径矛盾)没记下来"
    assert "死路" in src, "r74 那次「差点当成死路」的教训没记下来"
