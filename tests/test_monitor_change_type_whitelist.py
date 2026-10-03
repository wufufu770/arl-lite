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
import tempfile
from pathlib import Path

import pytest

from arl_lite.core.monitor import CHANGE_TYPES, record_change
from arl_lite.db.storage import Storage

REPO = Path(__file__).resolve().parent.parent
ARL_ROOT = REPO / "arl_lite"

# schema 注释里列的 6 种 —— 「词表」。它们**不是** CHANGE_TYPES,
# 这一点是 r42 明确选出来的,下面有测试钉住。
SCHEMA_VOCABULARY = (
    "NEW_ASSET", "DISAPPEARED", "TITLE_CHANGED", "TECH_CHANGED",
    "FINGERPRINT_CHANGED", "STATUS_CHANGED",
)


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


def test_schema_vocabulary_types_are_rejected(st):
    """schema 注释里的 4 个字段级类型现在一律报错

    它们不是「非法字符串」,是**注释里写过但从未实现**的类型。
    收紧之前它们能静默入库,让人以为字段级变更检测是通的。
    """
    for ct in SCHEMA_VOCABULARY:
        if ct in CHANGE_TYPES:
            continue
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
    for ct in ("TITLE_CHANGED", "", "随便编的", "NEW_ASSET "):
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
        record_change(st, "host", "TITLE_CHANGED", "h1", after={"a": 1})
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


# ── 「能力 vs 词表」这个取舍本身要被钉住 ──

def test_capability_is_strictly_narrower_than_schema_vocabulary(st):
    """CHANGE_TYPES ⊂ schema 的 6 种,且差集正好是 4 个字段级类型

    这是 r42 的核心取舍,写成测试是为了以后有人想「顺手放开」时,
    会看到自己正在拆掉什么 —— 而不是一个看起来无害的字典扩容。
    """
    assert set(CHANGE_TYPES) < set(SCHEMA_VOCABULARY)
    assert set(SCHEMA_VOCABULARY) - set(CHANGE_TYPES) == {
        "TITLE_CHANGED", "TECH_CHANGED", "FINGERPRINT_CHANGED", "STATUS_CHANGED",
    }


# ── 生产代码里的调用点:AST 检查,不靠文本子串 ──

def _record_change_literals():
    """遍历 arl_lite/ 源码,取出每个 record_change 调用点的 change_type

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
            if name != "record_change":
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


def test_production_call_sites_all_pass_whitelisted_literals():
    """arl_lite/ 里每个 record_change 调用点都传白名单内的字面量

    白名单只挡「运行时传进来的值」,挡不住「源码里写死的值」——
    而后者正是下一个人会犯的错。这条把静态检查也补上。
    """
    found = _record_change_literals()
    assert found, "一个 record_change 调用点都没扫到 —— AST 扫描本身坏了"
    bad = [(p, ln, ct) for p, ln, ct in found if ct not in CHANGE_TYPES]
    assert not bad, (
        "生产代码里有 record_change 调用传了白名单外的 change_type:"
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
    assert len(found) >= 2, f"只扫到 {len(found)} 个调用点,预期至少 2 个"
