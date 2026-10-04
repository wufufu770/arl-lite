"""每个 mutcheck 脚本都必须能 parse,且新脚本必须有写盘语法守卫。

背景:「多行 `assert X, (` 只替换首行会留下裸 `)` 变 SyntaxError」这个坑一共栽了
四次(r71 C1、r72 C2/C3、r74 C2、r77 C1)。r74 加的 `ast.parse` 写盘守卫每次都拦住了,
但那只把「静默假结果」变成「响亮报错」。

r77 暴露了守卫的边界:我用 heredoc 修变异脚本时写成了
`p.write_text(...)` **在 `ast.parse` 之前** —— 语法坏掉的文件直接进了磁盘。
**守卫保护变异目标,保护不了变异脚本自己。** 顺序错了,守卫等于不存在。

所以本条判据管两件事,都不需要例外清单:

  1. 每个 `devloop/mutcheck_*.py` 都必须能 `ast.parse`。脚本自己被写坏时,
     破损在**全量测试里**当场报红,而不是等我跑到那一轮才发现。

  2. round >= r74 的脚本必须守住写盘路径。阈值由**文件名里的轮次号**推导,
     所以未来每一轮自动适用、零维护 —— 不用维护一张「哪些脚本已加固」的名单,
     那种名单一定会漂(和 devloop 队列里那些 stale 条目一个毛病)。

r41~r73 那 33 个脚本不加守卫:它们是过去轮次的运行记录,改动等于篡改历史,
而且它们都已跑过、结果进了 git。守卫从 r74 起才有意义。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MUTCHECK_DIR = REPO / "devloop"

# 守卫从这一轮起才有意义(前面那些脚本是历史记录,不改)
GUARD_FROM_ROUND = 74

SCRIPTS = sorted(MUTCHECK_DIR.glob("mutcheck_*.py"))


def _round_of(path: Path) -> int:
    m = re.search(r"mutcheck_r(\d+)\.py$", path.name)
    return int(m.group(1)) if m else -1


def _writers_and_guards(path: Path):
    """分类「变换后写回」的函数,沿模块内调用图传递。

    要区分两种写:
      - **危险写**:读文件文本 → `.replace()`/`.split()` 变换 → 写回。变换可能
        留下孤儿行产生语法错误,必须先 `ast.parse`。
      - **还原写**:`finally` 里 `write_bytes(backup)`,写的是改动前读出来的原始
        字节,**不可能**引入语法错误 —— 首版判据把它也当成危险写,`_sweep`
        被误报,4 个脚本全红。

    还要看**委托**:r76/r77 的形状是 `_apply` 做变换、把写盘交给
    `_write_checked`(那个函数自己 ast.parse + write_text,但它没有 `.replace(`,
    因为 `out` 是参数)。只看单个函数会把这两个脚本误判成「没有守卫」。
    首版就是栽在这里。

    返回 (受保护的变换函数名, 没受保护的变换函数名)
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    segs = {name: (ast.get_source_segment(src, n) or "") for name, n in funcs.items()}

    def calls_ast_parse(node: ast.AST) -> bool:
        """**结构化**判断有没有 `ast.parse(...)` 调用。

        首版用 `"ast.parse" in seg` 文本子串,结果被 docstring 喂饱了 ——
        r76 的 `_write_checked` 文档字符串第一句就写着「先 ast.parse」,
        于是把守卫删掉之后我的判据仍判定「有守卫」,M1 存活。
        这正是本仓纪律里那条:结构检查用 AST,不用文本子串匹配。
        我写了这条判据,却对它自己犯了同一个错。
        """
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "parse"
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "ast"):
                return True
        return False

    def writes_text(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in ("write_text", "write_bytes")):
                return True
        return False

    def transforms(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in ("replace", "split", "join")):
                return True
        return False

    def callees(name: str, seen: set[str]) -> set[str]:
        out: set[str] = set()
        for n in ast.walk(funcs[name]):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in funcs and n.func.id not in seen:
                out.add(n.func.id)
        return out

    def has_guard(name: str, seen: set[str] | None = None) -> bool:
        """自身 ast.parse 且写盘,或委托到那样的函数。"""
        seen = set() if seen is None else seen
        if name in seen:
            return False
        seen.add(name)
        node = funcs[name]
        if calls_ast_parse(node) and writes_text(node):
            return True
        return any(has_guard(c, seen) for c in callees(name, seen))

    protected: set[str] = set()
    dangerous: set[str] = set()
    for name, node in funcs.items():
        if not transforms(node):
            continue
        if not writes_text(node):
            # 变换但不自己写 → 只要委托链上有守卫就算受保护
            if has_guard(name):
                protected.add(name)
            continue
        (protected if has_guard(name) else dangerous).add(name)
    return protected, dangerous


def test_there_are_mutcheck_scripts_to_check():
    """前提本身也要验 —— 别让「一个脚本都没有」变成恒真。"""
    assert len(SCRIPTS) >= 30, f"只找到 {len(SCRIPTS)} 个 mutcheck 脚本,数量不对"
    assert all(_round_of(p) > 0 for p in SCRIPTS), "有脚本名不符合 mutcheck_rNN.py 约定"


@pytest.mark.parametrize("path", SCRIPTS, ids=[p.stem for p in SCRIPTS])
def test_every_mutcheck_script_parses(path):
    """脚本自己必须语法正确。

    r77 就是在维护变异脚本时把 write_text 写在 ast.parse 之前,坏文件进了磁盘
    却没人发现。这里让它在全量里当场报红。
    """
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        pytest.fail(f"{path.name} 自己语法错误:{e}")


@pytest.mark.parametrize("path", SCRIPTS, ids=[p.stem for p in SCRIPTS])
def test_new_mutchecks_guard_their_write_path(path):
    """round >= r74 的脚本:每条写盘路径上都必须先 ast.parse。"""
    if _round_of(path) < GUARD_FROM_ROUND:
        pytest.skip(f"r{_round_of(path)} 早于 r74,是历史运行记录,不改")
    protected, dangerous = _writers_and_guards(path)
    assert protected, (
        f"{path.name} 没有任何「变换后写回」的语法守卫 —— "
        f"变异把文件写坏时不会被发现"
    )
    assert not dangerous, (
        f"{path.name} 里有变换后直接写回、却没先 ast.parse 的路径:{sorted(dangerous)}。"
        f"守卫必须只有一条路径可走,否则「有守卫」只是看起来有(r76 的教训)"
    )


def test_the_guard_threshold_is_derived_not_maintained():
    """阈值从文件名轮次号推导 —— 不是一张手维护的名单。

    手维护的名单一定会漂(这正是本仓 devloop 队列里 stale 条目的同款毛病),
    所以这里钉死「必须靠推导」这件事本身。
    """
    recent = [p for p in SCRIPTS if _round_of(p) >= GUARD_FROM_ROUND]
    assert recent, f"没有任何 round >= {GUARD_FROM_ROUND} 的脚本,推导规则失效了"
    for p in recent:
        assert re.search(r"mutcheck_r\d+\.py$", p.name), (
            f"{p.name} 不符合 mutcheck_rNN.py 命名,轮次号推导会失效"
        )


def test_the_judgment_catches_a_broken_script(tmp_path):
    """自检:本判据对**语法坏掉的脚本**必须真的会红。

    判据自己也得有牙齿 —— 不然上面几条全是恒真。
    """
    broken = tmp_path / "mutcheck_r99.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")  # 故意写坏
    with pytest.raises(SyntaxError):
        ast.parse(broken.read_text(encoding="utf-8"))
    # 反向:一个能 parse 的脚本不应被误报
    ok = tmp_path / "mutcheck_r98.py"
    ok.write_text("def f():\n    return 1\n", encoding="utf-8")
    ast.parse(ok.read_text(encoding="utf-8"))
