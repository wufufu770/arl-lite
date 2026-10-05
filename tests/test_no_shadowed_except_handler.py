"""r102:同一个 `try` 里有**永远到不了**的 `except` 分支(3 处)

## 实测的三个现场

`fofa.py` / `quake.py` / `virustotal.py` 的 `collect_subdomains` 里,
同一个 `try` 下 `except PermissionError` 出现了**两次**:

    except PermissionError as e:   # 第 139 行 —— 匹配在这里就结束了
        ...
    except ValueError as e: ...
    except PermissionError as e:   # 第 157 行 —— 永远到不了
        return [], str(e), "auth"

Python 逐个匹配 handler,第一个命中的就跳出。第二个 `PermissionError`
**没有任何异常能走到它**。执行级验证:

    PermissionError → 第一个 PermissionError
    TimeoutError    → TimeoutError
    第二个 PermissionError 从未被走到。

它做的事和第一个 handler 的兜底分支**一模一样**(`return [], str(e), "auth"`),
所以删掉它不改变任何行为 —— 这也正是它能安全删的原因。

## 这条检测器我写反了两次,所以判据的重点是**两个方向都得钉**

第一次:`except ValueError` 后面跟 `except Exception`,我报了不可达 —— **错**。
那是**正常且正确**的写法:窄的在前、宽的在后,两条都可达。

    ValueError → ValueError
    TypeError  → Exception
    KeyError   → Exception

规则搞反之后我又报出 11 处,真实只有 3 处。

第二次:修正时把方向修成了「后者在前者的祖先链里」,仍然是反的。
**正确的方向**:后一个 handler 不可达,当且仅当**它的类型是前一个的子类
(或相同)** —— 因为前面的捕获得更宽,把后面整个吃掉了。

判据必须同时锁住两头,否则:

- 只测「同类型重复」→ 方向写反的实现能通过合成样本,却会在真实代码上
  报出 7 处 `ValueError`/`Exception` 组合,把人引向删正常代码;
- 只测「窄在前宽在后不报」→ 同类型重复漏掉,真缺陷继续躺着。

**一个只会查一半的检测器,比没有更危险** —— 它会让人照着它去删能跑的东西。

## 为什么值得落成判据

这是**可证明**的那一类退化:不留任何判断空间,规则由语言本身决定。
不像死代码检测还要猜动态派发,这类的真假只取决于一条继承关系写对没写对。
而那条关系我连着写反两次 —— 说明它值得被钉住,而不是靠人记得。
"""
from __future__ import annotations

import ast
import collections
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
ARL = REPO / "arl_lite"

# 只记**确凿**的继承关系。记不清的类型一律不参与判定 ——
# 宁可漏报,不可误报:这条判据红了会有人去删代码。
BASES: dict[str, str] = {
    "PermissionError": "OSError",
    "FileNotFoundError": "OSError",
    "ConnectionError": "OSError",
    "TimeoutError": "OSError",
    "ValueError": "Exception",
    "TypeError": "Exception",
    "KeyError": "LookupError",
    "IndexError": "LookupError",
    "JSONDecodeError": "ValueError",
    "URLError": "OSError",
    "HTTPError": "URLError",
    "RuntimeError": "Exception",
}
BARE = "<bare>"


def _handler_type(handler: ast.ExceptHandler) -> str:
    return ast.unparse(handler.type) if handler.type is not None else BARE


def _ancestors(name: str) -> set[str]:
    """name 的祖先链。`name` 自己不在里面。"""
    out: set[str] = set()
    cur = name
    while cur in BASES:
        cur = BASES[cur]
        out.add(cur)
    return out


def is_shadowed(earlier: str, later: str) -> bool:
    """`earlier` 这个 handler 会不会把 `later` **整个**吃掉?

    **方向是这条判据的全部难点。** 只有当 `later` 的类型是 `earlier`
    类型的子类(或两者相同)时,`earlier` 才捕获得更宽、把 `later` 全吃掉。

    反过来 —— 窄的在前、宽的在后(`ValueError` 然后 `Exception`)——
    是**正常且正确**的写法,两条都可达,绝不能报。

    这条我写反过两次:第一次把方向整个搞反,报了 11 处假阳性;
    第二次方向对了但实现取的是「后者在前者的祖先链里」,还是反的。
    """
    if earlier == BARE:
        return True
    return earlier == later or earlier in _ancestors(later)


def shadowed_handlers(tree: ast.AST) -> list[tuple[int, str, str]]:
    """返回 [(handler 行号, 该 handler 的类型, 吃掉它的那个类型)]"""
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        seen: list[str] = []
        for handler in node.handlers:
            t = _handler_type(handler)
            for prev in seen:
                if is_shadowed(prev, t):
                    found.append((handler.lineno, t, prev))
                    break
            seen.append(t)
    return found


# ── 一、正控制:两个方向都得对 ──

def test_the_same_exception_type_twice_is_shadowed():
    """**同类型重复** → 后一个必须被判死

    这是 r102 治的那 3 处。用**合成源码**,因为真实代码里已经没有了 ——
    删干净之后就没有样本了,而判据必须还能验自己。
    """
    src = (
        "try:\n"
        "    risky()\n"
        "except PermissionError as e:\n"
        "    first(e)\n"
        "except TimeoutError as e:\n"
        "    second(e)\n"
        "except PermissionError as e:\n"
        "    third(e)\n"
    )
    got = shadowed_handlers(ast.parse(src))
    # 行号是 7 不是 6 —— 首版这里写错了,而**检测器是对的**。
    # 数错的期望值比没有期望值更坏:它会让人去改本来正确的规则。
    assert got == [(7, "PermissionError", "PermissionError")], (
        f"同类型重复没被判死,实际 {got}")


def test_narrow_before_broad_is_not_shadowed():
    """**窄在前、宽在后** → 一律不许报

    这是本判据最容易写错的一半。`except ValueError` 后面跟
    `except Exception` 是正常写法,两条都可达。

    头一版把方向整个搞反,于是真实代码里 `ValueError` + `Exception` 的
    正常组合全被报成「不可达」—— 一共 11 处假阳性,而真缺陷只有 3 处。
    """
    src = (
        "try:\n"
        "    risky()\n"
        "except ValueError as e:\n"
        "    narrow(e)\n"
        "except Exception as e:\n"
        "    broad(e)\n"
    )
    assert shadowed_handlers(ast.parse(src)) == [], (
        "窄在前宽在后被判成不可达 —— 那会让人去删正常代码。"
        "实测反例:`except ValueError` + `except Exception` 在集成层到处都是")


def test_a_narrower_type_after_a_broader_one_is_still_shadowed():
    """宽在前、窄在后 → 后者不可达(这条也常被写反)

    `except Exception` 在前、`except ValueError` 在后,后者永远到不了。
    这跟上一条方向正好相反,两条一起钉,方向就没地方错了。
    """
    src = (
        "try:\n"
        "    risky()\n"
        "except Exception as e:\n"
        "    broad(e)\n"
        "except ValueError as e:\n"
        "    never_runs(e)\n"
    )
    got = shadowed_handlers(ast.parse(src))
    assert got == [(5, "ValueError", "Exception")], (
        f"宽在前时后面的窄分支没被判死,实际 {got}")


def test_a_bare_except_shadows_everything_after_it():
    """裸 `except:` 吃掉它后面的一切"""
    src = ("try:\n"
           "    risky()\n"
           "except:\n"
           "    bare(e)\n"
           "except ValueError as e:\n"
           "    never(e)\n")
    got = shadowed_handlers(ast.parse(src))
    assert got == [(5, "ValueError", BARE)], f"裸 except 没吃掉后面,实际 {got}"


def test_an_indirect_subclass_is_still_shadowed():
    """**间接**子类(隔两层以上)也要认

    只记一层继承的话,`HTTPError` 后面跟 `OSError` 就会被漏掉 ——
    因为 `HTTPError` 的**直接**父类是 `URLError`,一层查不到 `OSError`,
    而实际上 `HTTPError ⊂ URLError ⊂ OSError`,前面的 `OSError` 已经
    把后面整个吃掉了。

    ## 首版这条挑的例子是错的,变异 M6 逮出来的

    我原来举的是「`except URLError` 在前、`except HTTPError` 在后」,
    而 `HTTPError` 的直接父类**就是** `URLError` —— 只隔一层,一层查找
    就够。所以那条测试根本没测到「间接」,M6(把 `_ancestors` 改成只看
    一层)**存活**了。

    **测试名字宣称的事没做到,比没有测试更坏**:它给了一层虚假的安全感。
    现在换成真正隔两层的例子:`OSError` 在前、`HTTPError` 在后。
    """
    # 隔两层:OSError ⊃ URLError ⊃ HTTPError
    src = ("try:\n"
           "    risky()\n"
           "except OSError as e:\n"
           "    broad(e)\n"
           "except HTTPError as e:\n"
           "    never(e)\n")
    got = shadowed_handlers(ast.parse(src))
    assert got == [(5, "HTTPError", "OSError")], (
        f"隔两层的子类(HTTPError ⊂ URLError ⊂ OSError)没被判死,实际 {got}")

    # 隔一层:也要认(回归保护)
    src1 = ("try:\n"
            "    risky()\n"
            "except URLError as e:\n"
            "    broad(e)\n"
            "except HTTPError as e:\n"
            "    never(e)\n")
    got1 = shadowed_handlers(ast.parse(src1))
    assert got1 == [(5, "HTTPError", "URLError")], (
        f"隔一层的子类没被判死,实际 {got1}")

    # 反向:窄在前不算(方向又反了就地现形)
    src2 = ("try:\n"
            "    risky()\n"
            "except HTTPError as e:\n"
            "    narrow(e)\n"
            "except URLError as e:\n"
            "    broad(e)\n")
    assert shadowed_handlers(ast.parse(src2)) == [], "窄在前被判死了 —— 方向又反了"


def test_the_rules_are_checked_against_real_python_not_just_my_opinion():
    """**执行级核对**:规则要和 CPython 的实际行为对上

    这条是本文件最重要的一条。前面的都是「我按规则写出来的」,
    只有这条是真的**跑一遍 Python** 看它到底进哪个分支。

    首版有个探针我自己就写错了(把异常**实例**当类传,`raise exc("boom")`
    于是抛的是 TypeError),结果「窄在前宽在后」那个反例跑出来全是
    `Exception` —— 看着像规则错了,其实探针错了。
    **一个坏探针会让人去改本来正确的规则。**
    """
    def which(exc_cls, chain: str) -> str:
        """按给定的 handler 源码顺序,真正走到哪个分支"""
        src = ("def probe(exc):\n"
               "    try:\n"
               "        raise exc('boom')\n"
               + chain)
        ns: dict = {}
        exec(compile(src, "<chain>", "exec"), ns)   # noqa: S102 — 判据自建的小探针
        return str(ns["probe"](exc_cls))

    narrow_first = ("    except ValueError:\n"
                    "        return 'ValueError'\n"
                    "    except Exception:\n"
                    "        return 'Exception'\n")
    assert which(ValueError, narrow_first) == "ValueError"
    assert which(TypeError, narrow_first) == "Exception", (
        "TypeError 该走宽分支 —— 如果这条红了,说明探针坏了而不是规则坏了")

    dup = ("    except PermissionError:\n"
           "        return '第一个'\n"
           "    except TimeoutError:\n"
           "        return 'Timeout'\n"
           "    except PermissionError:\n"
           "        return '第二个'\n")
    assert which(PermissionError, dup) == "第一个"
    assert "第二个" not in (which(PermissionError, dup),), (
        "第二个分支被走到了 —— 那规则写错了")


# ── 二、主判据:arl_lite 里不该再有不可达的 handler ──

def test_no_shadowed_except_handler_is_left_in_arlite():
    """主判据:`arl_lite/` 里不许有永远到不了的 `except` 分支

    这种 handler 的危害是它**看起来**在处理一类异常,于是下一个人以为
    这条路有人管。删掉它是安全的 —— 因为它做的事和前面的兜底分支一样。
    """
    offenders = []
    for p in sorted(ARL.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for lineno, exc, eaten_by in shadowed_handlers(tree):
            offenders.append(f"{p.relative_to(REPO)}:{lineno}  "
                             f"`except {exc}` 永远到不了(被 `except {eaten_by}` 吃掉)")
    assert not offenders, (
        "有永远到不了的 except 分支:\n  " + "\n  ".join(offenders)
        + "\n它们看起来在处理某类异常,其实一条都走不到。"
          "要么删掉,要么把前一个收窄。")

    # 正控制:扫描范围不能是空的
    pys = [p for p in ARL.rglob("*.py") if "__pycache__" not in str(p)]
    assert len(pys) >= 60, f"只扫到 {len(pys)} 个文件,范围不对"


def test_the_three_original_sites_are_gone_by_name():
    """**定点核对**:r102 治的那三处,按文件点名

    主判据说「全仓没有不可达的 handler」,但它是聚合断言。
    这条把三个具体文件单独点出来 —— 少修一个,这里立刻红。
    """
    for name, line in (("fofa", 157), ("quake", 143), ("virustotal", 131)):
        p = ARL / "integrations" / f"{name}.py"
        assert p.exists(), f"{p} 不见了"
        tree = ast.parse(p.read_text(encoding="utf-8"))
        got = shadowed_handlers(tree)
        assert not got, (
            f"{p} 里又有不可达的 handler 了:{got}"
            f"(r102 删掉的正是第 {line} 行那个)")
