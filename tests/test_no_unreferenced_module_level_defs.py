"""r101:一个静态检测器差点删掉 18 个能跑的东西 —— 而它唯一的错是一个阈值

## 本轮做了什么

`arl_lite/tui/app.py` 里有两份**手抄**的按键循环:

- `Screen.loop`(基类)
- `MainScreen.loop`(抄件,上面还写着「与 Screen.loop 同款」——自证是抄的)

而 `class MainScreen:` **根本没有继承 `Screen`**。决策 #9 说的正是这个:
两处手抄同一段逻辑,迟早漂。它们已经漂了一次(抄件把 `digits` 抽成了
局部变量,原件是内联的),漂的是排版不是语义 —— 下一次漂什么就不好说了。

改成 `MainScreen(Screen)` 继承,删掉抄件;顺带删掉全仓零引用的
`move_to()`。净减 10 行。

## 但本轮真正的交付物是**检测器**,不是那两处删除

起因是我想找死代码来松开 `loc_budget`。用「静态引用计数 == 0」去找,
跑出 **115 个候选**。逐个查下来,几乎全是误报:

- `FofaModule` / `QuakeModule` / `OtXModule` …… `registry.discover_modules()`
  用 `pkgutil.walk_packages` + `inspect.getmembers` **扫描 `arl_lite.modules`
  下每一个模块**收集 `BaseModule` 子类。它们**按设计就没有静态引用**。
- `cmd_monitor_add` …… 靠 `pma.set_defaults(func=cmd_monitor_add)` 接线。

第二遍剩 95 个,第三遍剩 **2 个**(真的)。三遍之间我只改了两处:

**一、阈值错了。** `def` 的名字在 AST 里**不是 `ast.Name` 节点**
(它只是 `FunctionDef.name`),所以「被调用一次」正好等于计数 1。
我原来用 `<= 1` 当阈值 —— 于是**所有只被调用一次的函数全被判成死代码**。
死代码的判据是 **0 次**。

**二、漏了动态发现面。** `arl_lite/modules/` 下的东西静态计数必然是 0。

## 为什么这个检测器值得落成判据

因为**判不准的检测器比没有检测器更危险**:它会让人去删能跑的代码,
而 `loc_budget` 只剩 1 行余量的时候,「顺手清一清」这种冲动最危险。

所以判据不只是「现在没有死代码」,更重要的是**钉住那两类误报**:
把检测器里任何一处阈值或排除项改错,当场红。

- 引用一次的函数 → 不许被判死(r101 的真实 bug)
- 动态发现目录下的定义 → 不许被判死(结构决定的)

以及一条正控制:合成样本里真的零引用的名字,**必须**能被列出来 ——
不然它就是个恒过的过滤器。
"""
from __future__ import annotations

import ast
import collections
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
ARL = REPO / "arl_lite"

# `registry.discover_modules()` 用 `pkgutil.walk_packages` 遍历
# `arl_lite.modules` 下的**每一个**模块,再 `inspect.getmembers(..., isclass)`
# 收集 BaseModule 子类。所以这个目录下的任何定义都**按设计没有静态引用**。
DYNAMIC_ROOT = ARL / "modules"


def _count_uses(paths: list[pathlib.Path]) -> collections.Counter:
    """统计全仓出现的标识符

    三类都算:`ast.Name`(裸引用)、`ast.Attribute`(`.foo`)、
    字符串字面量里的词(导出清单 / 动态派发的名字藏在字符串里)。

    ## 这里**不许**吞掉解析异常(r101 被门禁逮到)

    首版写的是 `except (SyntaxError, UnicodeDecodeError): continue`。
    它被 `test_swallowed_exceptions_are_accounted_for` 判成新增的
    「接住了就当没事」站点 —— 那个判据是对的,但**改设计比登记更对**。

    跳过一个解析不了的文件,后果不是「少看一点」,而是
    **它的标识符一个都没被计数** → 别处的定义可能被误判成零引用 →
    **检测器自己制造假阳性**。而假阳性在本轮是明确要治的病
    (113/115 都是误报)。吞异常把那个病又请回来了。

    而且仓库里不该有解析不了的 `.py` —— 有的话那本身就是缺陷,
    该炸出来让人看见,不该被这里悄悄放过。
    """
    uses: collections.Counter = collections.Counter()
    for p in paths:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                uses[n.id] += 1
            elif isinstance(n, ast.Attribute):
                uses[n.attr] += 1
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                for tok in n.value.replace(".", " ").split():
                    uses[tok.strip("',()[]:")] += 1
    return uses


def _module_level_defs(path: pathlib.Path) -> list[tuple[int, str]]:
    """该文件的**模块级** def / class(不递归进函数内部)"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [(n.lineno, n.name) for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def find_unreferenced(sources: list[pathlib.Path], uses: collections.Counter,
                      skip: set[pathlib.Path]) -> list[tuple[pathlib.Path, int, str]]:
    """零静态引用的模块级定义

    `skip` 里的文件整份跳过 —— 那就是动态发现面。
    """
    out = []
    for p in sources:
        if p in skip:
            continue
        for lineno, name in _module_level_defs(p):
            if name.startswith("__"):
                continue
            if uses[name] == 0:
                out.append((p, lineno, name))
    return out


# ── 一、正控制:检测器自己不许犯那两类错 ──

def test_a_name_called_exactly_once_is_not_dead(tmp_path):
    """**r101 的真实 bug**:被调用**一次**的名字不是死代码

    `def` 的名字在 AST 里不是 `ast.Name`(它只是 `FunctionDef.name`),
    所以「一处调用」= 计数 1。首版阈值写成 `<= 1`,于是
    `cmd_monitor_add`、`_print_csv` 这些**只被调用一次**的函数全被判死 ——
    候选从 115 降到 2 的过程中,这个 bug 贡献了 93 个误报。

    用合成样本钉死:调用方必须**真的调用**它。
    """
    lib = tmp_path / "lib.py"
    lib.write_text(
        "def used_once() -> None:\n"
        "    return None\n"
        "\n"
        "def used_twice() -> None:\n"
        "    return None\n",
        encoding="utf-8")
    caller = tmp_path / "caller.py"
    caller.write_text(
        "from lib import used_once, used_twice\n"
        "used_once()\n"
        "used_twice()\n"
        "used_twice()\n",
        encoding="utf-8")

    uses = _count_uses([lib, caller])
    dead = find_unreferenced([lib], uses, skip=set())
    names = [n for _, _, n in dead]

    assert "used_once" not in names, (
        f"只被调用一次的 {names} 被判成死代码 —— 阈值错了。"
        f"用引用计数去判死,判据应该是 0 次而不是 <= 1")
    assert "used_twice" not in names, "被调用两次的更不该判死"
    assert not names, f"两个都被调用过的函数不该有任何一个进名单:{names}"


def test_a_genuinely_unreferenced_name_is_still_reported(tmp_path):
    """**反向控制**:真的零引用的必须被列出来

    没有这条,上一条就能靠「永远返回空」变绿 —— 那是个恒过的过滤器,
    比没有检测器更坏。
    """
    lib = tmp_path / "lib.py"
    lib.write_text(
        "def called() -> None:\n"
        "    return None\n"
        "\n"
        "def nobody_calls() -> None:\n"
        "    return None\n",
        encoding="utf-8")
    caller = tmp_path / "caller.py"
    caller.write_text("from lib import called\ncalled()\n", encoding="utf-8")

    uses = _count_uses([lib, caller])
    dead = find_unreferenced([lib], uses, skip=set())
    assert [n for _, _, n in dead] == ["nobody_calls"], (
        f"真正没人调的没被列出来,实际 {[n for _, _, n in dead]} —— "
        f"那这条检测器就是个恒过的过滤器")


def test_dynamically_discovered_definitions_are_not_dead(tmp_path):
    """动态发现目录下的定义**不许**被判死

    `registry.discover_modules()` 遍历 `arl_lite.modules` 下每个模块,
    用 `inspect.getmembers(module, inspect.isclass)` 找 BaseModule 子类。
    `FofaModule`、`QuakeModule`、`OtXModule` 之类**没有任何静态引用**,
    删了它们就是删了能跑的功能 —— 而它们的静态计数就是 0。
    """
    mod = tmp_path / "modules" / "fofa.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("class FofaModule:\n    name = 'fofa'\n", encoding="utf-8")

    uses = _count_uses([mod])          # 全仓只有定义处,零引用
    assert uses["FofaModule"] == 0, "前置条件:它确实零静态引用"

    without_skip = find_unreferenced([mod], uses, skip=set())
    assert [n for _, _, n in without_skip] == ["FofaModule"], (
        "前置条件不成立:不跳过时它应该会被列出来")

    with_skip = find_unreferenced([mod], uses, skip={mod})
    assert with_skip == [], (
        f"动态发现目录下的 {with_skip} 被判成死代码 —— "
        f"删掉它就是删了能跑的功能")


def test_the_real_dynamic_root_exists_and_is_not_empty():
    """**反向控制**:上面那条用的那个目录,是真的存在且非空的

    用临时目录测出来的「跳过机制有效」不说明 `arl_lite/modules/` 真有东西。
    少了这条,`DYNAMIC_ROOT` 指错地方了也没人知道。
    """
    assert DYNAMIC_ROOT.is_dir(), f"{DYNAMIC_ROOT} 不存在"
    pys = [p for p in DYNAMIC_ROOT.rglob("*.py") if "__pycache__" not in str(p)]
    assert len(pys) >= 20, f"{DYNAMIC_ROOT} 下只有 {len(pys)} 个 .py —— 排除项可能指错了"
    # 真的走一遍发现逻辑,证明这些模块确实能被 collect 到
    from arl_lite.modules.registry import discover_modules
    found = discover_modules()
    assert found, "discover_modules() 一个模块都没发现 —— 前提不成立"


# ── 二、主判据:arl_lite 里不该再有零引用的模块级定义 ──

def test_no_unreferenced_module_level_definition_is_left_in_arlite():
    """主判据:`arl_lite/` 里(动态发现目录之外)不许有零静态引用的定义

    这条**会随代码变化而变**:有人新写一个函数还没接线,它就红了。
    那正是它该做的 —— 「刚写的函数没被任何地方调用」是个真问题,
    要么接线,要么删掉,而不是让它躺在那儿等人来猜。
    """
    all_py = [p for p in REPO.rglob("*.py")
              if "__pycache__" not in str(p) and ".git" not in p.parts]
    uses = _count_uses(all_py)

    sources = [p for p in sorted(ARL.rglob("*.py")) if "__pycache__" not in str(p)]
    skip = {p for p in sources if DYNAMIC_ROOT in p.parents or p.parent == DYNAMIC_ROOT}
    dead = find_unreferenced(sources, uses, skip)

    assert not dead, (
        "这些定义零静态引用:\n  "
        + "\n  ".join(f"{p.relative_to(REPO)}:{ln}  {name}" for p, ln, name in dead)
        + "\n要么接线,要么删掉。**注意别误判** —— 动态派发、argparse 的"
        "set_defaults(func=...)、__init__.py 的再导出都算引用,"
        "本判据已经把它们算进去了;确实用不到就是死代码。")

    # 正控制:扫描范围不能是空的,否则上面那条恒过
    assert len(sources) >= 50, f"只扫到 {len(sources)} 个 arl_lite 文件,范围不对"
    assert len(skip) >= 20, f"只跳过 {len(skip)} 个动态发现文件,排除项可能失效"


# ── 三、本轮那两处具体修改,不许被抄回去 ──

def test_main_screen_inherits_the_shared_loop():
    """`MainScreen` 必须继承 `Screen`,不能自己抄一份 `loop`

    r101 之前 `class MainScreen:` 后面是空的 —— 它没有继承任何基类,
    却把 `Screen.loop` 整段抄了一遍,抄件上写着「与 Screen.loop 同款」。
    两份已经漂过一次(抄件把 `digits` 抽成局部变量,原件内联)。
    """
    tree = ast.parse((ARL / "tui" / "app.py").read_text(encoding="utf-8"))
    classes = {c.name: c for c in tree.body if isinstance(c, ast.ClassDef)}
    assert "Screen" in classes and "MainScreen" in classes, "两个类都得在"

    bases = [ast.unparse(b) for b in classes["MainScreen"].bases]
    assert "Screen" in bases, (
        f"MainScreen 的基类是 {bases},不含 Screen —— "
        f"那意味着 loop 又得抄一份(决策 #9)")

    own = {f.name for f in classes["MainScreen"].body
           if isinstance(f, ast.FunctionDef)}
    assert "loop" not in own, (
        f"MainScreen 自己又定义了 {sorted(own)} —— 它继承 Screen 就不该重写 loop")


def test_the_shared_loop_lives_in_exactly_one_place():
    """**反向控制**:整个 TUI 里 `loop` 的实现只能有一份

    上一条只看 `MainScreen` 这一个类。万一有人在第三个类里又抄一份呢?
    所以这里扫全文件:所有 `def loop` 的实现体必须互不相同地只有一处 ——
    准确说是:只有一个类定义了 `loop`。
    """
    tree = ast.parse((ARL / "tui" / "app.py").read_text(encoding="utf-8"))
    definers = [c.name for c in tree.body
                if isinstance(c, ast.ClassDef)
                and any(isinstance(f, ast.FunctionDef) and f.name == "loop"
                        for f in c.body)]
    assert definers == ["Screen"], (
        f"`loop` 在 {definers} 里都有实现 —— "
        f"按键循环只有一份实现,其余的继承它")


def test_move_to_stays_deleted():
    """`move_to()` 是全仓零引用的死代码,不许回来

    实测(全仓,含 tests/ 与 devloop/):除了它自己的定义,零引用。
    它没有走任何动态派发路径,删掉之后 `test_phase3.py` 的 7 条全过。
    """
    tree = ast.parse((ARL / "tui" / "app.py").read_text(encoding="utf-8"))
    names = {n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    assert "move_to" not in names, (
        "move_to 又回来了 —— 全仓零引用的终端光标定位助手;"
        "真要用它就先接线,别让它躺着")
