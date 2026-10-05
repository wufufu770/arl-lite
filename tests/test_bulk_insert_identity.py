"""r49:`bulk_insert` 里那份第三份去重键推导,一致性没有任何东西在守

## 动手前先量的(不然容易写成「它已经漂了」这种夸大话)

r48 把「身份怎么拼」收进 `_ASSET_IDENTITY` 一份,但 `bulk_insert` 当时还是
自己的 if-else 链。逐条实测之后:

- 5 种资产类型 `bulk_insert` 存的 hash 和契约表算的**全部一致**(hosts 连
  大小写两种都对)。所以「它已经漂了」是夸大,本文件不这么说。
- 真问题一:**这份一致性没有任何东西在守**。`bulk_insert` 生产零调用
  (r44 已用 AST 钉住),所以把它改坏不会有任何测试变红。等哪天有人重新
  启用它,两条推导就各自漂 —— 而漂了的表现是「按名字过滤返回空」,
  输出看起来完全正常,没人会发现。
- 真问题二:if-else 链的兜底 `str(i)`(行号)实测**今天走不到**。白名单里
  6 张表每张都有 NOT NULL 身份列,缺列的行在插入那刻就报 IntegrityError
  (实测:hosts 缺 host → `NOT NULL constraint failed: hosts.host`)。
  所以「hash 依赖行序」今天不存在 —— **但那是 NOT NULL 约束的副作用,
  不是这条兜底写对了**。

## 顺带查实:那个 `elif table == "correlations"` 分支是死代码

`correlations` 表**没有 hash 列**,而外层守卫是 `if "hash" in valid_cols`,
所以那个分支永远进不去。和 r48 删掉的 `hash_key` 死参数同一类:
签名/分支在,没人在里面。

## 本轮真的改了什么

`bulk_insert` 改调 `asset_identity_of_row()`,后者按**列名**从行里取身份,
列名和默认值都住在 `_ASSET_IDENTITY` 里 —— 和 `add_*` 同一份定义。

为了让「按列名取」成立,契约表的段名从**形参名**改成**列名**
(`ports` 的段名是 `ip` 不是 `host`,因为 `add_port` 写的就是
`fields={"ip": host}`,而 `COLUMN_ALIAS` 也会把 `ports.host` 映射成 `ip`)。
传参仍按位置,所以列名只用于「段数对不对」和按列取值,不参与计算 ——
golden 值一个都没变。

## 改的过程中自己引入又修掉一个行为回归

第一版把「段是 `None`」一律判错,结果 `bulk_insert` 省略 `title` 会报错,
而 `add_finding` 的 `title` 形参默认就是 `""`、省略它没事 —— **同一条数据
从两条路进来会变成两个资产**。这比原来更糟,而且不报错。改成把默认值
(`add_*` 形参的默认值,不是新概念)写进契约表,本文件里
`test_omitting_a_part_with_a_default_lands_on_the_same_hash` 钉住它。
"""
from __future__ import annotations

import ast
import pathlib
import sqlite3

import pytest

from arl_lite.cli import _resolve_asset_hash
from arl_lite.core.monitor import list_changes, record_change
from arl_lite.db.storage import (Storage, _ASSET_IDENTITY, compute_asset_hash,
                                compute_hash)

REPO = pathlib.Path(__file__).resolve().parents[1]
# bulk_insert 自己的白名单
_BULK_TABLES = {"domains", "hosts", "ports", "sites", "findings", "correlations"}


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return Storage(workspace="t")


def _rows(storage, table):
    with storage._conn() as c:
        return [r["hash"] for r in c.execute(f"SELECT hash FROM {table}")]


# ── 一、主判据:两条路必须落在同一个 hash 上 ──
# 用**去重**而不是比十六进制:hash 一旦不同,第二条就会插成新行,
# 于是同一批数据从两条路进来变成两个资产 —— 而那正是要防的事。

def _insert_via_bulk(storage, table, row, via_add):
    before = len(_rows(storage, table))
    if via_add == "add":
        getattr(storage, via_add)(1, **row)
    else:
        storage.bulk_insert(table, [row], on_conflict="ignore")
    after = _rows(storage, table)
    return before, after


@pytest.mark.parametrize("table,add_name,kwargs,bulk_row", [
    ("domains", "add_domain",
     {"domain": "d.example.com", "source": "subdomain"},
     {"domain": "d.example.com", "module": "subdomain"}),
    ("hosts", "add_host",
     {"host": "web.example.com", "ip": "1.1.1.1"},
     {"host": "web.example.com", "ip": "1.1.1.1", "module": "portscan"}),
    ("ports", "add_port",
     {"host": "1.1.1.1", "port": 443, "protocol": "tcp"},
     {"ip": "1.1.1.1", "port": 443, "protocol": "tcp", "module": "portscan"}),
    ("sites", "add_site",
     {"url": "http://shop.example.com", "host": "shop.example.com",
      "ip": "1.1.1.1", "port": 80, "scheme": "http"},
     {"url": "http://shop.example.com", "host": "shop.example.com",
      "ip": "1.1.1.1", "port": 80, "scheme": "http", "module": "http"}),
    ("findings", "add_finding",
     {"target": "http://shop.example.com", "finding_type": "weak_tls",
      "title": "过期证书"},
     {"target": "http://shop.example.com", "finding_type": "weak_tls",
      "title": "过期证书", "module": "fingerprint"}),
])
def test_add_and_bulk_insert_agree_on_the_hash(tmp_path, monkeypatch, table,
                                              add_name, kwargs, bulk_row):
    """`add_*` 插过之后再 `bulk_insert` 同一个资产 → 必须被去重,不能变两行

    双向都试一遍:先 bulk 后 add 也是同一个道理,而只试一个方向的话,
    「只有一条路算错」这种情况会漏掉一半。

    `HOME` 在**函数体内**重定向,不是靠 fixture —— `test_no_real_home_writes`
    用 AST 找「哪些函数改过 HOME」,只认同一函数里的 `monkeypatch.setenv`;
    靠 fixture 间接重定向它看不出来,于是把这里的 `Storage(workspace="u")`
    判成会写到用户真实数据目录(实测被它逮到一次)。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    st = Storage(workspace="t")
    st.bulk_insert(table, [bulk_row], on_conflict="ignore")
    n_after_bulk = len(_rows(st, table))
    getattr(st, add_name)(1, **kwargs)
    n_after_add = len(_rows(st, table))
    assert (n_after_bulk, n_after_add) == (1, 1), (
        f"{table}:bulk_insert 插了 {n_after_bulk} 行,add_{add_name[4:]} "
        f"之后变成 {n_after_add} 行 —— 两条路算出的 hash 不一样,"
        f"同一批数据变成了两个资产")

    # 再反过来:先 add 后 bulk
    st2 = Storage(workspace="u")
    getattr(st2, add_name)(1, **kwargs)
    st2.bulk_insert(table, [bulk_row], on_conflict="ignore")
    assert len(_rows(st2, table)) == 1, f"{table}:反方向也该去重"


def test_bulk_insert_hash_equals_the_contract_table(st):
    """逐个类型对一遍 `compute_asset_hash` —— 上一条用去重间接证,这条直接证"""
    st.bulk_insert("hosts", [{"host": "web.example.com", "ip": "1.1.1.1",
                              "module": "m"}], on_conflict="ignore")
    st.bulk_insert("ports", [{"ip": "1.1.1.1", "port": 443, "module": "m"}],
                   on_conflict="ignore")
    st.bulk_insert("findings", [{"target": "t", "finding_type": "weak_tls",
                                 "title": "过期证书", "module": "m"}],
                   on_conflict="ignore")
    st.bulk_insert("sites", [{"url": "http://s.example.com", "module": "m"}],
                   on_conflict="ignore")
    st.bulk_insert("domains", [{"domain": "d.example.com", "module": "m"}],
                   on_conflict="ignore")
    for table, identity in (
            ("hosts", ("web.example.com",)),
            ("ports", ("1.1.1.1", 443)),
            ("findings", ("t", "weak_tls", "过期证书")),
            ("sites", ("http://s.example.com",)),
            ("domains", ("d.example.com",)),
    ):
        assert _rows(st, table) == [compute_asset_hash(
            st.workspace_id, table, *identity)], (
            f"{table} 的 hash 和契约表算的不一致 —— 两份推导又分家了")


# ── 二、省略有默认值的段,必须和显式默认值落在同一个 hash ──
# 这是本轮**自己引入又修掉**的回归,单独钉住:第一版把「段是 None」一律
# 判错,于是 bulk_insert 省略 title 报错、而 add_finding 省略它没事 ——
# 同一条数据从两条路进来变成两个资产,而且不报错。

def test_omitting_a_part_with_a_default_lands_on_the_same_hash(st):
    """`add_finding` 的 `title` 形参默认 `""`,那省略它就该等价于 `title=""`

    两边都断言:一,`bulk_insert` 省略 `title` 不许报错;二,它算出的 hash
    必须和 `add_finding(..., title="")` 一样。
    """
    r = st.bulk_insert("findings", [{"target": "t", "finding_type": "weak_tls",
                                     "module": "m"}], on_conflict="ignore")
    assert not r["errors"], f"省略 title 应当走默认值,不该报错:{r['errors']}"
    st.add_finding(1, "t", "weak_tls")          # title 用形参默认值 ""
    assert len(_rows(st, "findings")) == 1, (
        "省略 title 和显式空串必须落成同一个资产 —— 否则同一批数据从两条路"
        "进来会变成两个,而且不报错")
    assert _rows(st, "findings") == [compute_asset_hash(
        st.workspace_id, "findings", "t", "weak_tls", "")]


def test_defaults_come_from_the_add_signatures_not_a_second_table():
    """默认值必须是 `add_*` 形参的默认值,不能另写一份

    写法:拿 `add_finding` 的 `title` 形参默认值,和契约表里那一段对。
    以后有人把形参默认值改了而忘了改表,这里会红。
    """
    import inspect
    sig = inspect.signature(Storage.add_finding)
    default = sig.parameters["title"].default
    names, _sep, defaults = _ASSET_IDENTITY["findings"]
    i = names.index("title")
    assert defaults[i] == default, (
        f"契约表说 title 的默认值是 {defaults[i]!r},"
        f"而 add_finding 的形参默认是 {default!r}")


# ── 三、缺必需的身份列要报错,不许凑一个 hash ──

@pytest.mark.parametrize("table,row", [
    ("hosts", {"ip": "1.1.1.1", "module": "m"}),
    ("domains", {"module": "m"}),
    ("ports", {"port": 443, "module": "m"}),
    ("findings", {"finding_type": "weak_tls", "module": "m"}),
])
def test_missing_identity_column_is_an_error_not_a_guessed_hash(st, table, row):
    """缺了没有默认值的身份列 → 报错,而且报错要指名是缺哪一列

    凑一个 hash 的话,这一行会被存成**某个别的资产**,而输出里看起来
    完全正常。原来的 `str(i)` 兜底正是这种形状。
    """
    r = st.bulk_insert(table, [row], on_conflict="ignore")
    assert r["inserted"] == 0, f"{table} 缺身份列却插进去了"
    assert r["errors"], f"{table} 缺身份列却没有报错"
    msg = r["errors"][0]
    assert "身份列" in msg, f"报错要说清是身份列的问题,而不是某个约束名:{msg}"
    # 报错里得能看出缺的是哪一列
    names = _ASSET_IDENTITY[table][0]
    assert any(n in msg for n in names), f"报错没指出缺哪一列:{msg}"


def test_wrong_arity_raises():
    """段数不对要报错 —— 少一段照样能算出一个 16 位 hash"""
    with pytest.raises(ValueError, match="身份是 2 段"):
        compute_asset_hash(1, "ports", "1.1.1.1")
    with pytest.raises(ValueError, match="身份是 3 段"):
        compute_asset_hash(1, "findings", "target", "type")


def test_a_none_part_raises_but_an_empty_one_does_not():
    """`None` 是「没给这一段」,`""` 是「这一段就是空的」—— 两回事

    这条是变异 M7 逼出来的:第一版没有任何测试碰 `None` 那一支,所以把
    `if v is None` 改成 `if False` 全绿。差别在 `add_finding` 上是实在的:
    `title` 合法地可以是 `""`,而 `None` 意味着调用方漏传 —— 凑一个 hash
    出来,资产就被静默存成了另一个身份。
    """
    with pytest.raises(ValueError, match="是 None"):
        compute_asset_hash(1, "hosts", None)
    with pytest.raises(ValueError, match="是 None"):
        compute_asset_hash(1, "findings", "t", "weak_tls", None)
    # 空串合法,而且和 None 算出来**不一样** —— 所以不能拿它顶替
    empty = compute_asset_hash(1, "findings", "t", "weak_tls", "")
    assert empty != compute_asset_hash(1, "findings", "t", "weak_tls", "None")


def test_a_hash_column_without_an_identity_definition_is_refused():
    """来了一张「有 hash 列但没有身份定义」的表 → 报错,不许静默跳过

    这是把 `str(i)` 那条兜底彻底关掉之后必须有的一道:以前那张表会落到
    `else` 分支,拿到一个按行号算的 hash,而那一行看起来正常入库了。
    `correlations` 现在当例子用 —— 它没 hash 列所以走不到,但只要哪天有人
    往 `correlations` 加 hash 列,这条就是拦它的。
    """
    from arl_lite.db import storage as storage_mod
    with pytest.raises(ValueError, match="不在 _ASSET_IDENTITY"):
        storage_mod.asset_identity_of_row(1, "correlations", {"rule_name": "r"})


# ── 四、correlations 那条死分支删掉了,别加回来 ──

def test_correlations_has_no_hash_column_so_that_branch_was_dead():
    """`correlations` 没有 hash 列 → 原来那个分支永远进不去

    写下来是因为:它长得和别的分支一模一样,下一个人会以为它有用。
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript((REPO / "arl_lite" / "db" / "schema.sql")
                        .read_text(encoding="utf-8"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(correlations)")}
    conn.close()
    assert "hash" not in cols, "correlations 已经有 hash 列了,那条分支不是死的"
    assert "correlations" not in _ASSET_IDENTITY, (
        "correlations 不是资产类型,不该有资产身份定义")


def test_correlations_still_inserts_without_a_hash(st):
    """没有 hash 列的表照插不误 —— 收敛身份推导不能顺手把这条路堵了"""
    r = st.bulk_insert("correlations", [{"rule_name": "r1", "target": "t"}],
                       on_conflict="ignore")
    assert r["inserted"] == 1 and not r["errors"], r


# ── 五、结构:第二份推导不许回来 ──

def _storage_ast():
    return ast.parse((REPO / "arl_lite" / "db" / "storage.py")
                     .read_text(encoding="utf-8"))


def test_bulk_insert_has_no_identity_if_else_chain():
    """`bulk_insert` 里不该再出现「按表名分支去算 hash」

    判据用 AST 找 **`if table == "..."` 的分支体里有没有给 `row["hash"]`
    赋值**。第一版写成了「`bulk_insert` 里不许出现 `table == <某张资产表>` 的
    比较」,结果误报了 `COLUMN_ALIAS` 那处 —— 那是**列名别名**(`ports` 的
    `host` 要映射成 `ip`),和身份拼接是两回事。判据要比它守的事窄:
    守的是「算 hash 的那条路上不许分支」,不是「不许提到表名」。

    原来的 `if table == "findings"` / `elif table == "ports"` / … 正是这个形状。
    """
    fn = next(n for n in ast.walk(_storage_ast())
              if isinstance(n, ast.FunctionDef) and n.name == "bulk_insert")
    offenders = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        # 这个 if 是不是「按表名分支」?
        branches_on_table = isinstance(node.test, ast.Compare) and any(
            isinstance(c, ast.Constant) and c.value in _ASSET_IDENTITY
            for c in node.test.comparators)
        if not branches_on_table:
            continue
        # 分支体里有没有在算 row["hash"]?
        assigns_hash = False
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            for tgt in sub.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == "hash"):
                    assigns_hash = True
        if assigns_hash:
            offenders.append(ast.unparse(node.test))
    assert not offenders, (
        f"bulk_insert 里又出现了按表名分支的身份推导:{offenders}。"
        f"身份拼接只有 _ASSET_IDENTITY 一份。")


def test_compute_hash_is_only_called_from_the_one_place():
    """`compute_hash` 在 storage.py 里只应被 `compute_asset_hash` 调

    它是底层原语;上面每一层都该经过 `compute_asset_hash`。有人绕过它直接
    `compute_hash(ws, 某段拼好的串)`,就是新的一份推导。
    """
    tree = _storage_ast()
    n_calls = sum(1 for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name)
                  and node.func.id == "compute_hash")
    assert n_calls == 1, (
        f"storage.py 里有 {n_calls} 处 compute_hash(...) 调用,"
        f"应该只有 compute_asset_hash 里那一处 —— 多出来的就是新的一份推导")


# ── 六、端到端:r48 的 `--asset` 对批量进来的资产一样管用 ──

def test_asset_filter_works_on_bulk_inserted_rows(st, tmp_path, monkeypatch):
    """bulk_insert 进来的资产,按名字过滤照样能捞出来

    这是两轮的接口:`monitor changes --asset` 靠的是 hash 和入库路径一致。
    如果 bulk_insert 的推导和入库那份不同,这条就是空的 —— 而空结果
    看起来完全正常。
    """
    import contextlib
    import io
    from arl_lite.cli import main
    monkeypatch.setenv("HOME", str(tmp_path))
    st.bulk_insert("hosts", [{"host": "web.example.com", "ip": "1.1.1.1",
                              "module": "m"}], on_conflict="ignore")
    st.bulk_insert("hosts", [{"host": "api.example.com", "ip": "3.3.3.3",
                              "module": "m"}], on_conflict="ignore")
    h = compute_asset_hash(st.workspace_id, "hosts", "web.example.com")
    record_change(st, "host", "ADDRESS_CHANGED", h,
                  before={"ip": "1.1.1.1"}, after={"ip": "2.2.2.2"})
    assert len(list_changes(st, asset_hash=h)) == 1
    assert _resolve_asset_hash(st, "web.example.com", "host") == h

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["monitor", "changes", "-w", "t", "--type", "host",
              "--asset", "web.example.com"])
    out = buf.getvalue()
    assert "web.example.com" in out, out
    assert "api.example.com" not in out, f"过滤没生效:\n{out}"
