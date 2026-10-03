"""r42:变更类型白名单 —— `CHANGE_TYPES` 定义了却没人用

## 缺的是什么

`CHANGE_TYPES = ("NEW_ASSET", "DISAPPEARED")` 定义在 `core/monitor.py`
顶部,注释里还写着「schema 注释里列了 6 种,目前实现 2 种」。

**全项目零引用。** 没有任何代码读它,`record_change` 对 `change_type`
也没有任何校验。实测(收紧前):

    record_change(change_type='随便编的') -> True   # 入库了
    record_change(change_type='')          -> True   # 也入库了

## 为什么这条算 bug 而不是「多支持几种类型」

拼错一个字母(`TITLE_CHANGE` 少个 D)会得到一条**永久静默**的记录:
它进了库,于是 `monitor changes --type TITLE_CHANGE` 查得到它;
可没有任何代码路径会生成它,报告里也永远不会出现,还没人会发现。
「记录了」和「有意义」在这里被混成了同一件事。

同一个类的 `Monitor.detect_changes` 对 `asset_type` 是有白名单的
(`_ASSET_TABLES`,注释里明写「与 storage.query 同纪律」)。**两处纪律
不一致**,而 r42 要的就是把它对齐。

## 按「能力」校验,不按 schema 注释里的「词表」

schema 注释列了 6 种取值:`NEW_ASSET / DISAPPEARED / TITLE_CHANGED /
TECH_CHANGED / FINGERPRINT_CHANGED / STATUS_CHANGED`。那是**这张表能
存什么**。`CHANGE_TYPES` 只有 2 种,是**本模块产得出什么**。

按词表校验的话,「注释里写过」就等于「实现了」—— 而事实是
`TITLE_CHANGED` 这类字段级变更根本没有实现:它需要一个「上一轮的字段
快照」来对比,而资产表是原地 upsert 的、不留历史,`asset_changes` 里
也只存变更本身。按能力校验,想接新类型就必须先实现它。

## 报错,不静默改写

静默改写等于把一个拼错换成另一个拼错 —— 错得一模一样,但更难查,
因为原始输入已经不在任何地方了。
"""
from __future__ import annotations

import ast
import re
import tempfile
from pathlib import Path

import pytest

from arl_lite.core.monitor import CHANGE_TYPES, record_change
from arl_lite.db.storage import Storage

REPO = Path(__file__).resolve().parent.parent
ARL_ROOT = REPO / "arl_lite"

# r42 时 schema 注释里的 6 种是「词表」,和「能力」故意不相等。r44 把
# 4 种字段级类型都实现了,又加了第 7 种 ADDRESS_CHANGED,两边第一次对上 ——
# 所以这里不再硬编码一份词表,而是直接去 schema.sql 读,让注释本身当契约。
# 详见 test_schema_comment_and_change_types_agree。


@pytest.fixture
def st(tmp_path):
    return Storage(workspace="t", workspace_root=tmp_path)


def _count_rows(st) -> int:
    with st._conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM asset_changes").fetchone()[0]


# ── 白名单本身 ──

def test_every_whitelisted_type_is_accepted(st):
    """白名单里的每个取值都得真能写进去 —— 白名单不是摆设"""
    for ct in CHANGE_TYPES:
        assert record_change(st, "host", ct, f"h-{ct}",
                             after={"hash": f"h-{ct}"}) is True, (
            f"白名单里的 {ct!r} 反而写不进去"
        )
    assert _count_rows(st) == len(CHANGE_TYPES)


def test_typo_of_a_legal_type_is_rejected(st):
    """少一个字母就报错 —— 这是本条待办存在的全部理由"""
    with pytest.raises(ValueError):
        record_change(st, "host", "TITLE_CHANGE", "h1", after={"a": 1})


def test_field_types_are_rejected_only_if_not_declared(st):
    """白名单外的类型一律报错 —— 判据是 `CHANGE_TYPES`,不是「像不像一个类型」

    r42 写下这条时,schema 注释里的 4 个字段级类型会因为「注释里写过
    但从未实现」而报错。r44 实现了它们,于是它们变成合法值。
    所以这条不能钉死某个具体名单,只能钉「白名单说了算」——
    改 `CHANGE_TYPES` 是在改契约,不是让这条测试红。
    """
    for ct in ("TITLE_CHANGE", "ADDRESS_CHANGE", "标题变了", "status"):
        assert ct not in CHANGE_TYPES
        with pytest.raises(ValueError):
            record_change(st, "host", ct, "h1", after={"a": 1})


def test_empty_and_nonsense_are_rejected(st):
    """空串和纯乱码都不许进库"""
    for ct in ("", "   ", "随便编的", "new_asset", "New_Asset", None, 123):
        with pytest.raises(ValueError):
            record_change(st, "host", ct, "h1", after={"a": 1})


def test_rejection_happens_before_the_write(st):
    """被拒的调用**不能**留下任何行

    判别力的关键:一个「先 insert 再校验」的实现,上面那些
    `pytest.raises` 也能全绿,可库里已经多了 7 条垃圾记录。
    """
    before = _count_rows(st)
    for ct in ("TITLE_CHANGE", "", "随便编的", "NEW_ASSET "):
        with pytest.raises(ValueError):
            record_change(st, "host", ct, "h1", after={"a": 1})
    assert _count_rows(st) == before, (
        f"库里多了 {_count_rows(st) - before} 条 —— 校验发生在写入之后"
    )


def test_rejection_does_not_silently_rewrite_the_type(st):
    """不许偷偷改成别的类型 —— 报错才是唯一出口

    静默改写等于把一个拼错换成另一个拼错:错得一模一样,但原始输入
    已经不在任何地方,下一个人查起来只会看到「类型是对的」。
    """
    with pytest.raises(ValueError):
        record_change(st, "host", "TITLE_CHANGE", "h1", after={"a": 1})
    with st._conn() as conn:
        rows = conn.execute("SELECT change_type FROM asset_changes").fetchall()
    assert rows == [], f"报错却还是写了库:{[r['change_type'] for r in rows]}"


def test_error_message_lists_the_valid_choices(st):
    """报错要说清能选什么,和 `detect_changes` 的措辞一致

    和 `_ASSET_TABLES` 那条门是同一套纪律,错误信息的形状也得一样 ——
    只说「非法」不说「合法的有哪些」,等于让人去翻源码。
    """
    with pytest.raises(ValueError) as ei:
        record_change(st, "host", "NOPE", "h1", after={"a": 1})
    msg = str(ei.value)
    assert "NOPE" in msg, f"报错里没回显实际传进去的值:{msg}"
    for ct in CHANGE_TYPES:
        assert ct in msg, f"报错里没列出合法取值 {ct}:{msg}"


# ── 「注释」与「实现」现在是同一个契约 ──

def test_schema_comment_and_change_types_agree():
    """`schema.sql` 注释里列的类型 == `CHANGE_TYPES`

    r42 时这两者是脱节的:注释列 6 种、代码只有 2 种,而注释没人读 ——
    一份死文档。r44 把 4 种字段级类型都实现了,又加了第 7 种
    `ADDRESS_CHANGED`(资产换 IP 是最主要的监控变化之一,原先 6 种里
    没有一种能诚实地描述它),两边第一次对上。

    这条钉住它们**继续**对上:往 schema 注释里加一个类型而不实现,
    或者实现了却不写进注释,都会红。注释从此不再是「实现过了」的
    同义词 —— 它就是契约本身,两边任何一边漂移都看得见。
    """
    sql = (ARL_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    m = re.search(r"change_type\s+TEXT\s+NOT NULL,\s*--\s*(.+)", sql)
    assert m, "schema.sql 里找不到 change_type 那行的注释"
    # 按 `/` 切而不是扫大写单词:扫描会把注释里提到的 CHANGE_TYPES
    # 这类**别的标识符**也算进去,然后报一个莫名其妙的差集。
    listed = {t.strip() for t in m.group(1).split("/")}
    assert listed == set(CHANGE_TYPES), (
        "schema 注释与 CHANGE_TYPES 不一致,差集:"
        f"{listed ^ set(CHANGE_TYPES)}"
    )


def test_every_declared_change_type_is_actually_produced():
    """`CHANGE_TYPES` 里不许有谁也产不出的类型

    r42 的原话是「`CHANGE_TYPES` 定义了却没人用」—— 一份没人读的
    定义比没有更坏,因为它让人以为那几种类型是通的。r44 补上了字段级
    类型,那这条就从「有死条目」变成了「不许再有死条目」。
    """
    import ast
    from arl_lite.core.monitor import FIELD_CHANGE_TYPES
    produced = set(FIELD_CHANGE_TYPES.values())
    for path in sorted(ARL_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            # 扫**所有**变更记录入口,不只 record_change:r50 加了
            # record_change_capped 包装,只扫 record_change 时 `NEW_ASSET` 和
            # `DISAPPEARED` 的字面量在包装里,这里会误判成「谁也产不出」。
            if name not in CHANGE_WRITERS:
                continue
            if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
                produced.add(node.args[2].value)
    dead = set(CHANGE_TYPES) - produced
    assert not dead, f"CHANGE_TYPES 里有谁也产不出的类型:{dead}"


# ── 生产代码里的调用点:AST 检查,不靠文本子串 ──

# 扫哪些函数名算「变更记录入口」。`record_change` 之外还有它的包装:
# r50 加的 `record_change_capped` 内部把 change_type 原样传下去,所以调用点
# 在**包装里**,不在 `record_change` 上。
#
# 这不是「为了让测试变绿而加个名字」——r50 实测过:只扫 `record_change` 时,
# `test_every_declared_change_type_is_actually_produced` 报
# `CHANGE_TYPES 里有谁也产不出的类型:{'NEW_ASSET', 'DISAPPEARED'}`。
# 那不是那条测试坏了,是**静态判据真的漏了**:字面量被挪进了包装,
# 扫描器看不见了。r42 的坑(一份没人读的/看不见的定义比没有更坏)从另一扇门回来。
# 所以这份名单由下面的 `test_scanner_covers_every_caller_of_record_change` 守住:
# 新增任何一个包装而忘了加进来,那条会红。
CHANGE_WRITERS = ("record_change", "record_change_capped")


def _record_change_literals():
    """遍历 arl_lite/ 源码,取出每个变更记录调用点的 change_type

    用 AST 而不是正则/子串:文本里出现的 `"NEW_ASSET"` 可能在注释、
    字符串常量、甚至另一个函数名里,那些都不是调用点。
    """
    found = []
    for path in sorted(ARL_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            if name not in CHANGE_WRITERS:
                continue
            ct = None
            if len(node.args) >= 3:
                arg = node.args[2]
                ct = arg.value if isinstance(arg, ast.Constant) else "<非字面量>"
            for kw in node.keywords:
                if kw.arg == "change_type":
                    ct = kw.value.value if isinstance(kw.value, ast.Constant) else "<非字面量>"
            found.append((path.relative_to(REPO).as_posix(),
                          node.lineno, ct))
    return found


def _functions_calling(name: str) -> set[str]:
    """arl_lite/ 里所有**直接调用** `name` 的函数名"""
    callers = set()
    for path in sorted(ARL_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                callee = node.func
                cname = callee.id if isinstance(callee, ast.Name) else (
                    callee.attr if isinstance(callee, ast.Attribute) else None)
                if cname == name:
                    callers.add(fn.name)
    return callers


def _non_literal(call_sites) -> list:
    return [c for c in call_sites if c[2] == "<非字面量>"]


def test_production_call_sites_all_pass_whitelisted_literals():
    """arl_lite/ 里每个 record_change 调用点都传白名单内的字面量

    白名单只挡「运行时传进来的值」,挡不住「源码里写死的值」——
    而后者正是下一个人会犯的错。这条把静态检查也补上。
    """
    found = _record_change_literals()
    assert found, "一个 record_change 调用点都没扫到 —— AST 扫描本身坏了"
    # 只判**字面量**:变量/表达式的值静态看不出来,把它们算成违规
    # 只会逼着这条测试变成「必须所有调用都写死字符串」,那既做不到
    # 也没道理(storage 里的类型是查 FIELD_CHANGE_TYPES 得到的)。
    bad = [c for c in found if c[2] not in CHANGE_TYPES and c[2] != "<非字面量>"]
    assert not bad, (
        "生产代码里有 record_change 调用写死了白名单外的 change_type:"
        f"{bad}"
    )


def test_production_call_sites_are_actually_scanned():
    """防「扫描器扫了个寂寞」

    没有这条,上面那条在 `rglob` 路径写错时会静默地什么都不检查,
    而它本身仍然全绿 —— 恒真测试的又一种形态。
    """
    found = _record_change_literals()
    files = {p for p, _, _ in found}
    assert any("watcher" in p for p in files), (
        f"没扫到 watcher.py 里的调用点,扫到的是:{sorted(files)}"
    )
    # monitor.py 是字段级变更的记录点(r44 起)。它传的是变量而不是
    # 字面量(类型来自 `FIELD_CHANGE_TYPES`),静态判不了类型合不合法,
    # 所以这里只要求「扫到了」—— 那条 test_field_types_are_all_real_types
    # 负责校验映射表里的类型都是真的。
    assert any(p.endswith("core/monitor.py") for p in files), (
        f"没扫到 core/monitor.py 里的调用点,扫到的是:{sorted(files)}"
    )
    # 「至少 3 个调用点」这条是 **r50 之前的写法**,现在刻意不保留了。
    # 它是个魔法数字,绑的是当时的代码形状:r50 把 watcher 里两个调用点合并进
    # record_change_capped,扫到的从 3 个变成 2 个,那条断言就红了。
    # 把 3 改成 2 是**为了让它绿**,而那正是本协议反复警告的事 ——
    # 门槛跟着代码走,就等于没有门槛。
    #
    # 真正要守的是「没有漏扫」,那就别用数量表达:下面那条
    # `test_scanner_covers_every_caller_of_record_change` 独立扫一遍所有
    # 调用 record_change 的函数,要求它们全都在扫描名单里。数量涨了跌了
    # 都不影响那条成立,而漏掉一个包装它就红。


# `CHANGE_WRITERS` 之外的合法例外:这些函数也调用 record_change,但它们传的
# change_type **本来就该是变量**,字面量不在它们身上。
#
# 放在这里而不是放宽判据,是因为豁免本身需要被 review —— 只写在代码注释里的
# 豁免等于没有豁免。
#
# `_sink` / `attach_field_change_sink`:字段级变更路径。change_type 来自
# `FIELD_CHANGE_TYPES` 的**值**(那张表由 `test_field_types_are_all_real_types`
# 校验每个类型都真存在),所以字面量不在调用点上。r50 之前它们本来也不在
# 扫描范围内 —— 那时候 `produced` 集合是靠 `set(FIELD_CHANGE_TYPES.values())`
# 补齐的,不是靠扫调用点。
VARIABLE_CHANGE_TYPE_CALLERS = {
    "_sink": "字段级变更路径,类型来自 FIELD_CHANGE_TYPES 的值",
    "attach_field_change_sink": "同上(外层函数本身不记变更,只是接线)",
}


def test_scanner_covers_every_caller_of_record_change():
    """扫描名单要覆盖**每一个**靠字面量产出 change_type 的调用者

    r50 实测过的坑:新增 `record_change_capped` 包装之后,只扫
    `record_change` 的判据看不见调用点了,于是
    `test_every_declared_change_type_is_actually_produced` 报
    「NEW_ASSET / DISAPPEARED 谁也产不出」—— 那不是判据太严,
    是判据**漏了**。而漏了的判据比没有更坏:它会给出一个假的「有死条目」结论。

    所以名单不能靠人记得补,得由这条测试从「谁调用了 record_change」推出来。
    以后再有人加包装而忘了加进 `CHANGE_WRITERS`,这里会红。

    注意不是「所有调用者都要进名单」:传**变量**的调用者(字段级变更路径)
    本来就不该出现在字面量扫描里,它们在 `VARIABLE_CHANGE_TYPE_CALLERS` 里
    带理由列出。第一版把判据写成「所有调用者都要进」,结果把那两个也判成违规
    —— 判据要比它守的事窄。
    """
    wrappers = _functions_calling("record_change")
    missing = ({w for w in wrappers if w not in CHANGE_WRITERS}
               - set(VARIABLE_CHANGE_TYPE_CALLERS))
    missing.discard("record_change")
    assert not missing, (
        f"这些函数调用了 record_change,却不在 CHANGE_WRITERS 里:{sorted(missing)}。"
        f"它们的调用点不会被扫到 —— 字面量检查和「谁产得出哪些类型」两条"
        f"判据会一起失效(实测:r50 加 record_change_capped 后就踩到了)。"
        f"如果它传的是变量而不是字面量,加进 VARIABLE_CHANGE_TYPE_CALLERS "
        f"并写明理由。")


def test_change_writers_are_all_real_change_type_passthroughs():
    """`CHANGE_WRITERS` 里的每个名字都必须是真实的变更记录入口

    防止有人往名单里塞一个不相干的函数,让扫描范围看起来比实际大 ——
    那等于给上面两条判据开后门。
    """
    for name in CHANGE_WRITERS:
        if name == "record_change":
            continue
        assert name in _functions_calling("record_change"), (
            f"CHANGE_WRITERS 里的 {name!r} 根本不调用 record_change")


def test_variable_change_type_exceptions_still_exist():
    """例外名单不许腐化:里面的函数要么还在,要么该把理由删掉

    豁免本身需要被 review —— 一个指向已不存在函数的豁免,是给将来的
    改动留的一个不用解释的口子。
    """
    for name in VARIABLE_CHANGE_TYPE_CALLERS:
        assert name in _functions_calling("record_change"), (
            f"例外名单里的 {name!r} 已经不调用 record_change 了 —— "
            f"删掉这条豁免,别留一个指向死函数的免死金牌")
