"""r48:`monitor changes` 没法按资产过滤,而且 hash 的算法散在好几个地方

## r47 顺带查实的缺口(实测)

`monitor changes` 只有 `-t/--type`、`-c/--change-type`、`-l/--limit`、
`--json`、`-w/--workspace` 五个选项,`list_changes()` 也没有 `asset_hash`
参数 —— SQL 里只有 `workspace_id` 加两个可选的类型条件。两个 host 各记一条
变更之后,**没有任何办法只看其中一个**。资产一多,用户能做的就是盯着最近
50 条自己扫。

## 动手前先查实的:名字 → hash 可以纯本地算,不用查表

`_upsert_asset` 原本是 `compute_hash(str(workspace_id), unique_key)`,
5 种类型的 `unique_key` 逐条实测全中。于是「按名字过滤」不必去 join 资产表,
算出来就行。

**但更值得说的是查的过程中撞见的**:`hash_key` 是 `_upsert_asset` 的一个
**死参数** —— storage.py:658 声明,5 个调用点都传了,函数体里从来没读过,
而且 5 处传的还都和 `unique_key` 同值。和 r42 的 `CHANGE_TYPES` 零引用
同一类:签名在,调用在,没有人用。

## 所以这轮真正的形状:一份契约,一个来源

`unique_key` 的形状原先是 5 个 `add_*` 调用点各手写一份。CLI 要用就得再抄
一份 —— 那正是 r45 清掉过的毛病(choices 硬编码词表),而我在 r46 转头又写了
一份 `_ASSET_LABEL_FIELDS`。现在收进 `_ASSET_IDENTITY` 一张表,
`add_*` 和 CLI 都走 `compute_asset_hash`,并用测试把两侧的键集合双向钉住。

## 已知没做的(不是忘了,是不该一锅端)

`bulk_insert`(storage.py:802 附近)有**第三份**去重键推导,还是 if-else 链,
兜底是 `str(i)`(行号)。`fp_bench` / `perf_bench` 是第四、第五份,故意用
`json.dumps(row, sort_keys=True)` —— 它们要的只是「每行 hash 不重复」,
不是「hash 恒等于身份」。这三条各有各的理由,混进本轮只会把 diff 撑大,
已单独立项。
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import pathlib

import pytest

from arl_lite.cli import (_ASSET_TYPES_TO_TABLE, _resolve_asset_hash,
                          _split_identity, main)
from arl_lite.core.monitor import list_changes, record_change
from arl_lite.core.monitor import Monitor
from arl_lite.db.storage import (Storage, _ASSET_IDENTITY, compute_asset_hash,
                                compute_hash)

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """独立工作区。同 r46/r47:只改 HOME,`cmd_monitor_changes` 自己建 Storage"""
    monkeypatch.setenv("HOME", str(tmp_path))
    return Storage(workspace="t"), tmp_path


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


# ── 一、契约表和 Monitor 的资产表必须是同一套键 ──
# r45 那条教训:同一张表两个来源,迟早漂。双向钉 —— 只查一个方向的话,
# 「加了类型忘了加身份定义」照样溜过去。

def test_identity_keys_are_exactly_the_monitor_asset_tables():
    """`_ASSET_IDENTITY` 的键 == `Monitor._ASSET_TABLES` 的值,不多不少

    多了:有一张表有 hash 规则但 `monitor changes --type` 选不到。
    少了:有一个资产类型能入库,但算不出 hash。
    """
    assert set(_ASSET_IDENTITY) == set(Monitor._ASSET_TABLES.values())


def test_every_asset_type_can_be_routed_to_its_table():
    """`asset_type → table` 和 `table → 身份` 两张表能接上"""
    for at, table in _ASSET_TYPES_TO_TABLE.items():
        assert at in Monitor._ASSET_TABLES, f"{at} 不在资产表里"
        assert table in _ASSET_IDENTITY, f"{at} → {table} 没有身份定义"


# ── 二、`add_*` 存的 hash 必须真的等于 `compute_asset_hash` 算出来的 ──
# 这是本轮最承重的一条:它证明「单一来源」不是嘴上说说,而是**生产路径
# 真的走了它**。以前的写法是调用点各手写 `unique_key=`,这里就是它。

@pytest.mark.parametrize("add,splat,table,expected", [
    ("add_domain", ("d.example.com", "subdomain"), "domains", ("d.example.com",)),
    ("add_host", ("web.example.com",), "hosts", ("web.example.com",)),
    ("add_port", ("1.1.1.1", 443), "ports", ("1.1.1.1", 443)),
    ("add_site", ("http://shop.example.com",), "sites", ("http://shop.example.com",)),
])
def test_stored_hash_equals_compute_asset_hash(ws, add, splat, table, expected):
    st, _ = ws
    if add == "add_site":
        getattr(st, add)(1, splat[0], "shop.example.com", "1.1.1.1", 80, "http")
    else:
        getattr(st, add)(1, *splat)
    with st._conn() as c:
        real = c.execute(f"SELECT hash FROM {table}").fetchone()["hash"]
    assert real == compute_asset_hash(st.workspace_id, table, *expected)


def test_finding_stored_hash_equals_compute_asset_hash(ws):
    """findings 单独写:身份是三段复合,构造参数和身份段不是一一对应"""
    st, _ = ws
    st.add_finding(1, "http://shop.example.com", "weak_tls", title="过期证书")
    with st._conn() as c:
        real = c.execute("SELECT hash FROM findings").fetchone()["hash"]
    assert real == compute_asset_hash(
        st.workspace_id, "findings", "http://shop.example.com", "weak_tls", "过期证书")


# ── 二之二、身份怎么拼是**冻结的兼容性契约** ──
# 上一组测试有个洞:它拿「存进库的 hash」和「`compute_asset_hash` 算出来的」
# 比,两边走的是同一张表 —— 把分隔符从 `:` 换成 `|`,两边**一起变**,测试照样绿。
# 变异 M1/M2 就是这么活下来的。
#
# 所以这里钉 golden 值。钉 golden 不算「在测试里重新实现一遍」:那正是
# M13 那种「CLI 自己另拼一份」的写法,只会把漂移合法化。golden 是**一个
# 不许变的常量**,而它之所以不许变,是因为 hash 已经落在磁盘上:改了拼接方式,
# 库里所有旧行再也算不出同一个 hash,资产还在,但 `--asset` 一过滤就是空的,
# 而空结果看起来完全正常。

_GOLDEN = {
    ("domains", "d.example.com"): "ab92747e0bd28235",
    ("hosts", "web.example.com"): "68cd3922cdc45da2",
    ("ports", "1.1.1.1:443"): "26c72a4e08b3283d",
    ("sites", "http://shop.example.com"): "23f385ee905e69b6",
    ("findings", "http://shop.example.com|weak_tls|过期证书"): "cb1e24d67702c38f",
}


@pytest.mark.parametrize("typed", list(_GOLDEN))
def test_composition_is_frozen(ws, typed):
    """身份段的顺序、分隔符、段数,改了会让**已有的**资产再也过滤不出来

    golden 值是 workspace_id=1 时算的,那正是每个新工作区的取值
    (见 `test_workspace_id_is_always_one_in_the_current_layout`)。
    """
    table, identity = typed
    st, _ = ws
    if table == "domains":
        st.add_domain(1, "d.example.com", "subdomain")
    elif table == "hosts":
        st.add_host(1, "web.example.com", ip="1.1.1.1")
    elif table == "ports":
        st.add_port(1, "1.1.1.1", 443)
    elif table == "sites":
        st.add_site(1, "http://shop.example.com", "shop.example.com",
                    "1.1.1.1", 80, "http")
    else:
        st.add_finding(1, "http://shop.example.com", "weak_tls", title="过期证书")
    with st._conn() as c:
        real = c.execute(f"SELECT hash FROM {table}").fetchone()["hash"]
    assert real == _GOLDEN[typed], (
        f"{table} 的 hash 变了(现在 {real})。身份拼接是磁盘上的兼容性契约:"
        f"改了之后库里已有的 {table} 行再也算不出同一个 hash,"
        f"资产还在但按名字过滤会返回空 —— 除非你确认所有库都能重跑,否则回退。")


# ── 二之三、大小写不能被偷偷吃掉 ──
# 变异 B1/B3 靠 `host.lower()` / `url.lower()` 活了下来,因为所有测试数据
# 都是小写。大小写敏感是被测行为的一部分,不是实现细节。

def test_identity_is_case_sensitive(ws):
    """`WEB.example.com` 和 `web.example.com` 是两个资产,不能被合并成一个

    DNS 本身不区分大小写,但**库里的 hash 区分** —— 存进去的是原样。
    哪天真要大小写归一,那是换 hash 算法的决定,得显式做,不能顺手 lower()。

    host 和 site 都要:变异 B1 用 `host.lower()`、B3 用 `url.lower()`,
    两条都靠「所有测试数据都是小写」活了下来 —— 只测一种就还是漏。
    """
    st, _ = ws
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.add_host(2, "WEB.example.com", ip="1.1.1.1")
    st.add_site(1, "http://Shop.example.com", "Shop.example.com",
                "1.1.1.1", 80, "http", title="Shop")
    st.add_site(2, "http://SHOP.example.com", "SHOP.example.com",
                "1.1.1.1", 80, "http", title="Shop")
    with st._conn() as c:
        hosts = {r["host"]: r["hash"]
                 for r in c.execute("SELECT host, hash FROM hosts")}
        sites = {r["url"]: r["hash"]
                 for r in c.execute("SELECT url, hash FROM sites")}
    assert hosts["web.example.com"] != hosts["WEB.example.com"], (
        "host 的大小写被合并了 —— 库里现在只能靠 host 列区分,按名字反查会命中错的那个")
    assert sites["http://Shop.example.com"] != sites["http://SHOP.example.com"], (
        "site 的 URL 大小写被合并了 —— 同一个站点被存成两个资产")
    assert _resolve_asset_hash(st, "WEB.example.com", "host") == hosts["WEB.example.com"]
    assert _resolve_asset_hash(st, "web.example.com", "host") == hosts["web.example.com"]
    assert (_resolve_asset_hash(st, "http://SHOP.example.com", "site")
            == sites["http://SHOP.example.com"])


# ── 三、workspace_id 在 hash 里,但今天恒等于 1 ──
# 我第一版注释写的是「hash 是工作区作用域的,两个工作区算出不同的 hash」。
# 写完顺手测了一下,**是错的**:每个工作区是独立的 db 文件
# (`<root>/<name>/data.db`),每个文件的 `workspaces` 表只有自己那一行,
# 所以 `workspace_id` 永远是 1,同一个域名在两个工作区算出**同一个** hash。
#
# 前提错了就得改判据,不能改测试迁就代码(反过来也一样不行 —— 得先查清
# 到底哪边是真的)。实测真相比原来的说法有意思:hash 会跨工作区撞车,
# **真正把两个工作区隔开的只有 SQL 里那个 `workspace_id = ?`**。

def test_workspace_id_is_always_one_in_the_current_layout(tmp_path, monkeypatch):
    """把「恒等于 1」钉住 —— 这是上面那条判据的前提,不是细节

    哪天改成「一个库放多个工作区」,这里会红,那时候才算真的工作区作用域。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("t", "u", "v"):
        st = Storage(workspace=name)
        assert st.workspace_id == 1, (
            f"工作区 {name} 的 workspace_id 是 {st.workspace_id},不再是 1 —— "
            f"hash 的作用域真的变了,上面那两条测试的前提要重写")


def test_same_name_in_two_workspaces_collides_on_the_same_hash(tmp_path, monkeypatch):
    """同一个域名在两个工作区**算出同一个 hash**(实测,不是设计意图)

    记下来是因为它反直觉:看到 hash 里带了 `workspace_id`,很容易以为隔离
    靠的是 hash。实际靠的是「两个工作区压根不在同一个文件里」。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    st = Storage(workspace="t")
    other = Storage(workspace="u")
    assert compute_asset_hash(st.workspace_id, "hosts", "web.example.com") == \
        compute_asset_hash(other.workspace_id, "hosts", "web.example.com")


def test_filtering_in_one_workspace_never_sees_the_other(tmp_path, monkeypatch):
    """hash 撞车的前提下,两个工作区仍然互不可见 —— 因为 SQL 按 workspace_id 筛

    这条是上一条的**后果**,也才是真正保证隔离的东西:把 `list_changes` 里的
    `WHERE workspace_id = ?` 去掉,用户在工作区 t 就会看到工作区 u 的变更,
    而输出里的一切看上去都完全正常(有 hash、有标识、有时间)。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    st = Storage(workspace="t")
    other = Storage(workspace="u")
    h = compute_asset_hash(st.workspace_id, "hosts", "web.example.com")
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    record_change(st, "host", "ADDRESS_CHANGED", h,
                  before={"ip": "1.1.1.1"}, after={"ip": "2.2.2.2"})

    assert len(list_changes(st, asset_hash=h)) == 1
    assert list_changes(other, asset_hash=h) == [], (
        "工作区 u 看到了工作区 t 的变更 —— 隔离靠的是 workspace_id 条件,"
        "不是 hash")
    rc, out, err = _run("monitor", "changes", "-w", "u", "--asset", h)
    assert rc == 0 and "no changes" in out, f"CLI 同样必须看不见:\n{out}{err}"


def test_missing_workspace_id_would_produce_a_hash_that_matches_nothing(ws):
    """把 workspace_id 漏掉的后果:算得出 16 位 hash,但一条都查不到

    这条和上面两条方向相反 —— 今天 workspace_id 恒为 1,于是「算错」的表现
    格外隐蔽:hash 照样是 16 位十六进制,看着完全正常,只是永远对不上。
    """
    st, _ = ws
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    with st._conn() as c:
        real = c.execute("SELECT hash FROM hosts").fetchone()["hash"]
    without_ws = compute_hash("web.example.com")
    assert without_ws != real
    record_change(st, "host", "ADDRESS_CHANGED", real,
                  before={"ip": "1.1.1.1"}, after={"ip": "2.2.2.2"})
    assert len(list_changes(st, asset_hash=without_ws)) == 0, (
        "漏掉 workspace_id 的 hash 本该一条都查不到,查到了说明过滤没生效")


# ── 四、`--asset` 的行为 ──

def _seed_two_hosts(st):
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.add_host(2, "api.example.com", ip="3.3.3.3")
    with st._conn() as c:
        h = {r["host"]: r["hash"] for r in c.execute("SELECT host, hash FROM hosts")}
    for name, ip in (("web.example.com", "1.1.1.1"), ("api.example.com", "3.3.3.3")):
        record_change(st, "host", "ADDRESS_CHANGED", h[name],
                      before={"ip": ip}, after={"ip": "9.9.9.9"})
    return h


def test_filter_by_name_isolates_one_asset(ws):
    """按名字过滤:只出这一个资产的变更,另一个的必须消失"""
    st, _ = ws
    _seed_two_hosts(st)
    rc, out, err = _run("monitor", "changes", "-w", "t",
                        "--type", "host", "--asset", "web.example.com")
    assert rc == 0, err
    assert "web.example.com" in out, out
    assert "api.example.com" not in out, f"过滤没生效,另一个资产还在:\n{out}"
    assert "1 change(s)" in out, out


def test_filter_by_bare_hash_works_without_type(ws):
    """裸 hash:用户从别处粘过来的就是 hash,不该逼他先知道类型"""
    st, _ = ws
    h = _seed_two_hosts(st)
    rc, out, err = _run("monitor", "changes", "-w", "t", "--asset", h["web.example.com"])
    assert rc == 0, err
    assert "web.example.com" in out and "api.example.com" not in out, out


def test_name_without_type_is_an_error_not_an_empty_result(ws):
    """给名字却没给 `--type` → 报错,不是「查不到」

    方向很重要:静默返回空的话,用户会以为「这个资产最近没变过」,
    而真相是他少写了一个参数。
    """
    st, _ = ws
    _seed_two_hosts(st)
    rc, out, err = _run("monitor", "changes", "-w", "t", "--asset", "web.example.com")
    assert rc == 2, f"该报错,不该静默:\nout={out}\nerr={err}"
    assert "--type" in err, f"报错要告诉人怎么改:\n{err}"


def test_filter_that_matches_nothing_is_not_an_error(ws):
    """过滤没命中就是没命中,不是错误 —— 资产真的没变过也是这个结果"""
    st, _ = ws
    _seed_two_hosts(st)
    rc, out, err = _run("monitor", "changes", "-w", "t",
                        "--type", "host", "--asset", "nothing.example.com")
    assert rc == 0, f"没命中不该报错:\n{err}"
    assert "no changes" in out, out


def test_json_output_respects_the_asset_filter(ws):
    """`--json` 也得听过滤 —— 两条路说的是同一件事"""
    st, _ = ws
    _seed_two_hosts(st)
    data = json.loads(_run("monitor", "changes", "-w", "t", "--json",
                           "--type", "host", "--asset", "api.example.com")[1])
    assert len(data) == 1 and data[0]["label"] == "api.example.com", data


# ── 五、port 的写法:必须从右边切 ──

@pytest.mark.parametrize("typed,expected", [
    ("1.1.1.1:443", ("1.1.1.1", "443")),
    ("web.example.com:443", ("web.example.com", "443")),
    # IPv6 从左边切第一个冒号会把地址切烂
    ("2001:db8::1:443", ("2001:db8::1", "443")),
])
def test_port_identity_splits_at_the_last_colon(typed, expected):
    assert _split_identity("port", typed) == expected


@pytest.mark.parametrize("bad", ["1.1.1.1", "", ":443", "1.1.1.1:", "1.1.1.1:http"])
def test_port_identity_rejects_unusable_input(bad):
    """写法不对要报错 —— 悄悄当成别的东西会算出一个永远查不到的 hash"""
    with pytest.raises(ValueError):
        _split_identity("port", bad)


def test_finding_by_name_is_refused_with_a_reason(ws):
    """findings 的身份是三段复合,按名字过滤直接拒绝并说清楚

    宁可明说不支持,也不要让人背内部格式 —— `target|finding_type|title`
    这种拼法只有入库代码自己用得起来。
    """
    st, _ = ws
    rc, out, err = _run("monitor", "changes", "-w", "t",
                        "--type", "finding", "--asset", "http://shop.example.com")
    assert rc == 2, f"该拒绝:\n{out}{err}"
    assert "hash" in err, f"要告诉人还能怎么用:\n{err}"


# ── 六、`compute_asset_hash` 自己的纪律 ──

def test_wrong_arity_raises_instead_of_computing_a_different_asset():
    """少给一段照样能算出 16 位 hash,于是资产被静默存成另一个身份

    这是 `add_domain` 拒绝空域名同一纪律:宁可报错,不要凑。
    """
    with pytest.raises(ValueError):
        compute_asset_hash(1, "ports", "1.1.1.1")
    with pytest.raises(ValueError):
        compute_asset_hash(1, "findings", "target", "type")


def test_unknown_table_raises():
    """表名不认识就报错 —— 不猜哪张表,也不退回 `compute_hash`"""
    with pytest.raises(ValueError):
        compute_asset_hash(1, "correlations", "x")


# ── 七、死参数不许悄悄回来 ──
# r48 删掉了 `_upsert_asset` 的 `hash_key`(声明了、5 个调用点传了、
# 函数体里零引用)。参数删掉很容易,过两个月又有人为了「对称」加回来。

def test_upsert_asset_has_no_dead_hash_key_parameter():
    """`hash_key` 不许回来 —— 它从来没有被读过"""
    src = (REPO / "arl_lite" / "db" / "storage.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_upsert_asset")
    params = [a.arg for a in fn.args.args]
    assert "hash_key" not in params, f"死参数 {params} 又回来了"
    assert "unique_key" not in params, (
        f"unique_key 也该换成分段 identity,现在还在:{params}")


def test_no_production_call_site_passes_unique_key_or_hash_key():
    """生产代码里不该再有 `unique_key=` / `hash_key=` —— 走 `identity=`"""
    src = (REPO / "arl_lite" / "db" / "storage.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name != "_upsert_asset":
            continue
        kws = {k.arg for k in node.keywords}
        if kws & {"unique_key", "hash_key"}:
            bad.append((node.lineno, sorted(kws)))
    assert not bad, f"这些调用点还在传旧参数,去重键又变成手写的了:{bad}"


def test_cli_resolves_through_the_same_function_as_insert(ws):
    """CLI 反查 hash 必须走 `compute_asset_hash`,不能自己再拼一遍

    这条把 r45 的教训钉在最要紧的地方:入库和过滤是**同一个**资产,
    算出来的 hash 必须一致。CLI 一旦自己拼,拼错时两边各错各的,
    过滤只会返回空 —— 而空结果看起来完全正常。
    """
    st, _ = ws
    hashes = _seed_two_hosts(st)
    # 按名字取,不用 `SELECT hash FROM hosts` —— 无 ORDER BY 时取到哪行不保证,
    # 这次就取到了 api 那行(同一个坑本轮已经踩过两次)。
    real = hashes["web.example.com"]
    from_cli = _resolve_asset_hash(st, "web.example.com", "host")
    assert from_cli == real, "CLI 算的 hash 和库里存的对不上 —— 过滤会永远返回空"
    assert from_cli == compute_asset_hash(st.workspace_id, "hosts",
                                          "web.example.com")


def test_cli_resolves_multi_part_identities_correctly(ws):
    """**多段**身份必须走契约表 —— 单段身份蒙混得过去,多段不行

    这条单独写是因为变异 M13 靠它活了下来:M13 让 CLI 只拿第一段去算
    (`compute_hash(ws, parts[0])`),而 domain/host/site 都只有一段,
    算出来和契约表**完全一样**,于是一条测试都拦不住。只有 ports 的
    两段(`1.1.1.1` + `443`)和 findings 的三段能把这种偷懒暴露出来。
    """
    st, _ = ws
    st.add_port(1, "1.1.1.1", 443, protocol="tcp")
    st.add_finding(1, "http://shop.example.com", "weak_tls", title="过期证书")
    with st._conn() as c:
        ph = c.execute("SELECT hash FROM ports").fetchone()["hash"]
        fh = c.execute("SELECT hash FROM findings").fetchone()["hash"]
    # ports 支持按名字:正好是能暴露「只取第一段」的那一类
    assert _resolve_asset_hash(st, "1.1.1.1:443", "port") == ph, (
        "CLI 对多段身份只取了第一段 —— 这和入库算出来的不是同一个资产")
    # findings 不支持按名字,但底层仍然必须是契约表算的那一个
    assert compute_asset_hash(st.workspace_id, "findings",
                              "http://shop.example.com", "weak_tls",
                              "过期证书") == fh


# ── 八、`--asset` 得真的挂在 parser 上(结构,不是文本) ──

def test_asset_option_is_on_the_changes_subcommand():
    """用 parser 查,不用源码子串匹配"""
    parser = __import__("arl_lite.cli", fromlist=["build_parser"]).build_parser()
    sub = parser._subparsers._group_actions[0].choices["monitor"]
    changes = sub._subparsers._group_actions[0].choices["changes"]
    args = changes.parse_args(["--asset", "x", "--type", "host"])
    assert args.asset == "x" and args.type == "host"
    # 不给也必须有这个属性,否则 `getattr(args, "asset", None)` 之外的用法会炸
    assert changes.parse_args([]).asset is None
