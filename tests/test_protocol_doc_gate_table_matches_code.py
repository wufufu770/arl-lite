"""r108:协议文档的门禁总表和实现脱节了 67 轮 —— 没人查

## 实测发现的(不是推测)

`docs/devloop-protocol.md` 的 5.1「七道门禁总表」与 `arl_lite/devloop/gates.py`
的实际实现逐条对了一遍,**7 条里 5 条的命令/阈值是错的或干脆虚构的**:

| 条目 | 文档写的 | 实际 |
|---|---|---|
| G2 | `state.json.baseline.fail_count` | 在 `devloop/baselines.json`,且 r27 起**按测试身份**判定而不是只比总数 |
| G3 | 自写 `tools/check_imports.py` 跑 import 图 DFS | **`tools/` 目录都不存在** |
| G4 | `find arl_lite -name "*.py" ... ≤ 18,000 行` | `baselines.json` 的 `total_loc` + 300 容差(现值 16567) |
| G5 | 每条 YAML 规则必须有 `advice:` 非空 | `name`/`risk`/`confidence`/`advice` **四个字段**都要非空 |
| G7 | `state.json.last_doc_audit_round` 与当前轮差 ≤ 5 | 只扫 `docs/PROJECT_PLAN.md` 查 4 个已下架依赖;**`state.json` 里没有 `last_doc_audit_round` 这个键** |

门禁的**名字**和 **blocking 属性**七条全对 —— 所以粗看没问题,只有逐条
核对才看得出。这也解释了它为什么能错 67 轮:文档里没有任何东西在对照实现。

## 为什么这类错误比缺文档更糟

一份说谎的协议比没有协议更糟:它让人**以为某些检查存在**,于是不去补。
比如 G3 那一行会让人以为循环依赖检测是靠一个独立脚本跑的,于是改了
`gates.py` 也不会去动它。

## 判据

五条,各钉一个方向,互不替代:

- 表里的门禁名与 `gates.py` 里各 Gate 类的 `name` 属性**完全一致**
  (不多不少)。少一条 = 有门禁没人记;多一条 = 记了个不存在的门禁。
- 表里标 blocking / 非阻断,和类属性一致。
- **5.1 表里**引用的 `.py` 路径必须真实存在(只扫表,不扫全文 —— 全文有
  4 处是占位符/举例,见该判据 docstring)。
- 文档里的 `state.json.<键>` 必须真的是 `state.json` 的顶层键。这两条
  抓的是 G3 和 G7 那种**虚构引用**。
- 上面用的剥离规则自己没坏 —— 用**合成输入**独立验,不依赖当前文档。

最后一条是前四条的前提:剥离一旦失效,前两条会报「文档有虚构引用」,
而修的人会去改文档,把 G3 那句关键的「没有这个文件」删掉。**判据自己
坏掉比判据太松更危险。**

## 用 AST 还是文本

本文件的判据大量涉及「文档里写了什么」,那是 markdown,按
`tests/test_source_checks_are_structural.py` 的负控制组,读 markdown 查词
是**允许**的(那是按行为断言,不是源码结构检查)。而涉及 `gates.py` 的部分
全走 AST 拿类属性,不用 `ast.unparse` 出来的文本去匹配。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "devloop-protocol.md"
GATES = REPO / "arl_lite" / "devloop" / "gates.py"
STATE = REPO / "devloop" / "state.json"

# 5.1 那张表的行:`| **G1** | `no_thirdparty_import` | ... | **blocking** | ... |`
_ROW_RE = re.compile(
    r"^\|\s*\*\*G(\d)\*\*\s*\|\s*`([a-z_]+)`\s*\|(.*?)\|(.*?)\|(.*?)\|\s*$",
    re.M,
)

# 文档里出现的**可执行脚本引用**:`xxx.py` / `dir/xxx.py`,排除 markdown 链接
_PY_REF_RE = re.compile(r"(?<![\w/])((?:[\w.]+/)*[\w]+\.py)\b")
# `state.json.<键>` 或 `state.json` 附近的键名引用
_STATE_KEY_RE = re.compile(r"state\.json\.([a-z_]{3,})")
# `「...」` 是**引述**(本文件讲历史时引述旧文档的错误),不是自己的声明
_QUOTED_RE = re.compile(r"「[^」]*」")
# 代码区也是引述 —— 里面是**别处的源码/示意图**,不是本文档的声明
_FENCE_RE = re.compile(r"^```.*?^```", re.S | re.M)     # ```lang ... ```
_INDENT_RE = re.compile(r"^(?:[ ]{4,}.*|\n(?=[ ]{4,}))", re.M)  # 4 空格缩进块


def _strip_quoted(text: str) -> str:
    """剥掉三类「不是本文档自己的声明」的东西。

    首版只剥 `「」`,于是两条判据各报一条**假阳性**:
    - `.py` 检查报 `tools/check_imports.py` —— 那是 G3 单元格**引述旧表格**说
      「没有这个文件」,引号已经包上了,只是检查没调 `_QUOTED_RE.sub`。
    - `state.json` 检查报 `tmp` —— 4 处全在代码块里(`with_suffix(".tmp")`
      的实现、并发写的示意图),那里的 `state.json.tmp` 是**文件名**不是键。
    """
    return _INDENT_RE.sub("", _FENCE_RE.sub("", _QUOTED_RE.sub("", text)))


def _doc_rows() -> dict[str, tuple[str, str]]:
    """5.1 表 → {门禁名: (blocking 段, 整行)}"""
    out = {}
    for m in _ROW_RE.finditer(DOC.read_text(encoding="utf-8")):
        name = m.group(2)
        blocking_cell = m.group(4)
        out[name] = (blocking_cell, m.group(0))
    return out


def _real_gates() -> dict[str, bool]:
    """gates.py 里真实的门禁 → {name: blocking}"""
    tree = ast.parse(GATES.read_text(encoding="utf-8"))
    out: dict[str, bool] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        name_val = None
        blocking = True
        for item in node.body:
            if not isinstance(item, ast.Assign) or len(item.targets) != 1:
                continue
            t = item.targets[0]
            if not isinstance(t, ast.Name) or not isinstance(item.value, ast.Constant):
                continue
            if t.id == "name" and isinstance(item.value.value, str):
                name_val = item.value.value
            elif t.id == "blocking":
                # 首版写的是 `getattr(node, "blocking", None)` —— 那是**错的**:
                # `ast.ClassDef` 实例没有 `blocking` 这个 Python 属性(AST 的字段
                # 存在 `_fields` 里,访问要走 `node.blocking`),getattr 恒返回 None,
                # 于是 `else` 分支给出 True,把 `doc_freshness` 误判成 blocking。
                # **一个取不到值的兜底默认值,会把「没取到」说成「是 True」。**
                blocking = bool(item.value.value)
        if name_val:
            out[name_val] = blocking
    return out


# =====================================================================
# 名字:不多不少
# =====================================================================


def test_the_table_lists_exactly_the_gates_that_exist():
    """少一条 = 有门禁没人记;多一条 = 记了个不存在的门禁。"""
    doc = set(_doc_rows())
    real = set(_real_gates())
    assert doc, (
        "5.1 那张表一行都没解析出来 —— 表格格式变了,"
        "本判据现在什么都验不到(前置条件不成立)"
    )
    assert doc == real, (
        f"门禁总表和 gates.py 对不上。\n"
        f"  只在文档里:{sorted(doc - real)}\n"
        f"  只在代码里:{sorted(real - doc)}\n"
        f"  表里写了 {len(doc)} 条,代码里有 {len(real)} 条"
    )


def test_blocking_column_matches_the_class_attribute():
    """blocking 标错的后果:该拦的放过去了,或者没必要的拦死了。"""
    real = _real_gates()
    wrong = []
    for name, (cell, _line) in _doc_rows().items():
        if name not in real:
            continue                      # 由上一条负责报
        says_blocking = "非阻断" not in cell
        if says_blocking != real[name]:
            wrong.append(
                f"{name}: 文档说 {'blocking' if says_blocking else '非阻断'},"
                f"代码里 blocking={real[name]}")
    assert not wrong, "blocking 标注与实现不一致:\n  " + "\n  ".join(wrong)


# =====================================================================
# 虚构引用:抓 G3 / G7 那种
# =====================================================================


def test_every_python_path_the_doc_mentions_actually_exists():
    """**5.1 表里**引用的 `.py` 路径必须真的在仓库里。

    r108 之前 G3 那一行写的是 `tools/check_imports.py` —— **`tools/` 目录
    根本不存在**。它能存在 67 轮,是因为没有任何东西检查「文档提到的文件
    是不是真的」。

    ## 只扫表格,不扫全文 —— 首版扫全文出了 4 个结果,3 个是误报

    | 扫出来的东西 | 是什么 |
    |---|---|
    | `tools/check_imports.py` | **真虚构**,在表里,该报 |
    | `test_phaseN.py` | 占位符(N 是轮次变量) |
    | `tests/x.py` | 举例 |
    | `tests/没写的文件.py` | 举例「文件不存在」 |

    表格那一列是**声明性最强**的地方(它说「实现靠什么跑」),而正文里的
    路径多半是示意。为三个假阳性去加「含大写就算占位符」之类的启发式,
    就是 r105 写过的那个错 —— **判不准的检测器比没有更危险**。

    剥引述(`「」`)之后,G3 那句「没有「`tools/check_imports.py`」」整个消失,
    表里只剩 G6 真的 `arl_lite/ai/prompts.py`。
    """
    paths: set[str] = set()
    for _name, (_cell, row) in _doc_rows().items():
        paths.update(_PY_REF_RE.findall(_strip_quoted(row)))
    assert paths, (
        "5.1 表里一个 .py 路径都没扫出来 —— 前置条件不成立,"
        "本判据现在验不到任何虚构引用"
    )
    missing = sorted(p for p in paths if not (REPO / p).exists())
    assert not missing, (
        f"5.1 表里引用了仓库里不存在的 .py 文件:{missing}\n"
        f"  一个说谎的协议比没有协议更糟:它让人以为某些检查存在,于是不去补"
    )


def test_every_state_json_key_the_doc_mentions_actually_exists():
    """`state.json.<键>` 里的键必须是 `state.json` 真实的顶层键。

    r108 之前 G7 写的是 `state.json.last_doc_audit_round` —— 那个键不存在,
    而 doc_freshness 门禁压根不读 state.json。全文另有三个:`baseline`
    (真身在 `devloop/baselines.json`)、`last_all_green_round`、`round_history`
    (真身是 `history`),前两个现已被 `「」` 包成引述。

    ## 判据自己踩过的两个坑

    一,`state.json.tmp` 是**原子写用的临时文件名**,4 处引用(`with_suffix`
    的实现、并发写坏文件的示意图)全在代码块里。那里的 `.tmp` 是路径的一
    段,不是顶层键。首版想用「后面跟不跟扩展名」来排除,拼出来是
    `"tmp.tmp"`,**永远匹配不上**,排除等于没写;改成扫 tail 之后又得逐个
    补 4 种上下文(行尾、`(写了一半`、`(覆盖)`、` -> `)—— 那是给假阳性打
    补丁。改成剥代码区,4 处一次性归零,不用任何启发式。

    二,剥得太狠也是坏的。所以下面有两条前置条件:剥完之后**至少还剩一个**
    引用(否则 `assert not phantom` 恒真),以及 `test_the_stripper_itself_`
    独立验一遍剥离规则没坏。
    """
    state_keys = set(json.loads(STATE.read_text(encoding="utf-8")))
    text = _strip_quoted(DOC.read_text(encoding="utf-8"))
    mentioned = set(_STATE_KEY_RE.findall(text))
    assert mentioned, (
        "剥掉引述和代码区之后,文档里一个 state.json.<键> 都没剩下 —— "
        "前置条件不成立,本判据现在验不到任何虚构引用"
    )
    phantom = sorted(k for k in mentioned if k not in state_keys)
    assert not phantom, (
        f"协议文档引用了 state.json 里不存在的键:{phantom}\n"
        f"  state.json 真实的顶层键:{sorted(state_keys)}"
    )


# =====================================================================
# 判据自己坏掉 —— 比判据太松更危险
# =====================================================================


def test_the_stripper_itself_still_strips_what_it_claims():
    """上面那个 `_strip_quoted` 会不会哪天悄悄退化成一个 `return text`?

    会,而且**症状指错方向**:剥离一旦失效,`tmp` 和 `tools/check_imports.py`
    就回到扫描结果里,上面两条报「文档有虚构引用」。而修的人看到的是一张
    干干净净的 5.1 表写着「G3 引用了不存在的 `tools/check_imports.py`」——
    他会**把那一句删掉**,正好抹掉 r108 查出来的那条更正。

    所以这里用一段**合成输入**独立验剥离规则本身,不依赖当前文档长什么样。
    实测(把 `_strip_quoted` 换成 `return text`):本条 + 上面两条共 3 条转红。
    """
    synthetic = (
        "真引用 `state.json.history` 要留下\n"
        "```python\n"
        "tmp = p.with_suffix('.tmp')  # state.json.tmp\n"
        "```\n"
        "    缩进块里的 state.json.tmp 也得没\n"
        "引述「`state.json.ghost`」也得没\n"
    )
    out = _strip_quoted(synthetic)
    for gone in ("ghost", "tmp", "with_suffix"):
        assert gone not in out, (
            f"_strip_quoted 没剥掉 {gone!r};剩余:\n{out}"
        )
    assert "state.json.history" in out, (
        f"_strip_quoted 把**真引用**也剥掉了 —— 它剥得太狠,不是太松:\n{out}"
    )


# =====================================================================
# 前置条件
# =====================================================================


def test_the_criterion_would_notice_a_blank_table():
    """把表清空,上面两条会不会一起恒绿?

    会 —— `doc == real` 在两边都空时成立。所以必须有一条独立地要求
    「表里有东西」。少了它,「整张表被删掉」是一个不会被发现的退化。
    """
    rows = _doc_rows()
    assert len(rows) == 7, (
        f"5.1 表里解析出 {len(rows)} 行(应为 7 条门禁):{sorted(rows)}"
    )
