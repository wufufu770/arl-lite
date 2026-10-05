"""r56:`search` 和 `query` 静默截断,连「显示了多少条」都不说

## 实测的退化路径(不是推测)

库里 200 个 site:

    $ arl-lite search sites probe-marker
    (表格,50 行)                 <- 一个「共 N 条」都没有,连显示条数都没有
    $ arl-lite query sites --limit 10
    (表格,10 行)                 <- 同上

比 r55 的 `monitor changes` 更重:那条至少有首行计数,这两条连
**显示了多少**都不告诉用户。而 `--limit` 的默认值是 50,和实际条数
毫无关系。用户拿 `search` 当「这个关键词一共多少东西」的唯一手段时,
拿到的是 50。

## 所以本轮的重点不是「加个提示」,是「别让机制分叉」

r55 已经在 `monitor changes` 上手写了一份提示。r56 要给另外两条加上,
于是那份手写必须收掉 —— **两处手抄同一段话,迟早漂**(决策 #9)。
所以判定和文案都抽成共用函数(`_needs_total` / `_limit_notice_text` /
`_print_table_limited`),三条命令走同一份。

## 总数必须和列表同源,否则比不报更坏

`cmd_query` 的 `-f/--filter` 是**用户传的** SQL WHERE。如果 `count_rows`
自己再拼一遍条件,两边一漂就会报「共 200 条」而当前筛选下其实只有 50 条
—— 那是个看起来精确的假数字。所以 storage 层把 WHERE 抽成
`_query_where`,`query` 和 `count_rows` 共用;FTS 那边同理 `_search_where`。

## 「查不出来」不许说成「零条」

`search` 在 FTS5 出错时会**退回 LIKE 搜索**继续给结果。那种情况下
`count_search` 数的是 FTS 的条数、列表给的是 LIKE 的条数,是两个集合。
把前者当总数是报假数,把它当 **0** 更坏 —— `0` 的含义是「确实一条都没
命中」,而真实情况是「没查出来」。所以 `count_search` 失败返回 `None`,
提示行明说「总数 UNKNOWN」。(r47 的 `_ASSET_LABEL_FIELDS` 同一个病:
静默降级比降级本身更坏。)
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import pathlib

import pytest

from arl_lite.cli import (_limit_notice_text, _needs_total, main)
from arl_lite.db.storage import Storage

REPO = pathlib.Path(__file__).resolve().parents[1]
TOTAL = 200


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    st = Storage(workspace="default")     # CLI 的 --workspace 默认值
    for i in range(TOTAL):
        # 一半 443、一半 80 —— filter 场景要真有区分性,
        # 否则「总数带 filter」和「总数不带 filter」数值相同,判据恒真
        st.add_site(1, f"https://h{i}.probe.com", f"h{i}.probe.com",
                    "1.1.1.1", 443 if i % 2 == 0 else 80, "https",
                    title="probe-marker")
    return st, tmp_path


def _run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(list(argv))
    return buf.getvalue()


def _notices(out):
    return [l for l in out.splitlines() if l.startswith("[i]")]


def _data_rows(out, marker_prefix="  ["):
    return [l for l in out.splitlines() if l.startswith(marker_prefix)]


# ── 一、主判据:两条命令都要说 ──

def test_search_says_how_many_rows_it_shown(ws):
    """200 条命中,默认 50 —— 必须说「50 of 200」

    r56 之前 `search` 连「显示了 50 条」都不说,输出里一个 `[i]` 行都没有。
    """
    out = _run("search", "sites", "probe-marker")
    assert len(_notices(out)) == 1, (
        f"search 只该有一条提示,实际 {len(_notices(out))} 条 —— "
        f"要么没提示,要么喊得太多")
    assert "50 of 200" in _notices(out)[0], f"提示没说真实总数:{out[-300:]}"
    assert "probe-marker" in out


def test_query_says_how_many_rows_it_shown(ws):
    """`query --limit 10` 拿到 200 行时,必须说「10 of 200」"""
    out = _run("query", "sites", "--limit", "10")
    assert "10 of 200" in _notices(out)[0], (
        f"提示没说真实总数:{_notices(out)}")


def test_not_truncated_still_states_the_total(ws):
    """没被截断时也要说总数

    「静默地给全了」让用户分不清「这就是全部」和「恰好没超 limit」。
    少了这一句,「库里到底有多少」永远只能靠猜。
    """
    out = _run("query", "sites", "--limit", str(TOTAL + 5))
    assert " of " not in _notices(out)[0], f"没截断却说了「其中一部分」:{out[-200:]}"
    assert "200 row(s)" in _notices(out)[0], (
        f"没截断时也该说总数 200:{_notices(out)}")


# ── 二、要害:总数必须和列表同源 ──

def test_query_total_respects_the_user_supplied_filter(ws):
    """`--filter` 是用户传的 SQL,总数必须带**同一个** filter

    库里一半 443、一半 80。`--filter "port=443"` 的总数必须是 100。
    如果 `count_rows` 自己再拼一遍条件而漏了 filter,这里会报 200 ——
    那是个看起来精确的假数字,比不报更坏。
    """
    st, _ = ws
    out = _run("query", "sites", "--filter", "port=443", "--limit", "5")
    assert "5 of 100" in _notices(out)[0], (
        f"加 filter 后总数应是 100,实际:{_notices(out)}")
    assert st.count_rows("sites", filter_sql="port=443") == 100, (
        "count_rows 自己都不对")


def test_count_rows_equals_an_unbounded_query_under_every_filter(ws):
    """**不变式**:`count_rows` == `query(limit=足够大)` 的长度

    过滤条件只可能在一个地方漂(漏一个 `AND`、方向反了、忘校验),
    这条把每种漂法都盖住。
    """
    st, _ = ws
    for flt in (None, "port=443", "port=80", "scheme='https'",
                "port=443 AND scheme='https'", "host LIKE 'h1%'"):
        n = st.count_rows("sites", filter_sql=flt)
        rows = st.query("sites", filter_sql=flt, limit=10_000)   # 上限就是 10k
        assert n == len(rows), (
            f"filter={flt!r}:count_rows 报 {n},列出来 {len(rows)} 条 —— "
            f"两边用的 WHERE 不是同一份")


def test_count_search_matches_an_unbounded_search(ws):
    """`count_search` 也必须等于 `search(limit=足够大)` 的长度"""
    st, _ = ws
    for kw in ("probe-marker", "h1", "h99"):
        n = st.count_search("sites", kw)
        rows = st.search("sites", kw, limit=10_000)
        assert n is not None, f"count_search({kw!r}) 返回了 None(该数出来的)"
        assert n == len(rows), (
            f"关键词 {kw!r}:count_search 报 {n},search 列了 {len(rows)} 条")


def test_count_search_returns_none_not_zero_when_counting_fails(ws, monkeypatch):
    """数不出来时返回 `None`,不是 0

    `0` 的含义是「确实一条都没命中」。而 FTS5 出错时 `search` 会退回
    LIKE 搜索继续给结果 —— 那种情况下拿 FTS 的条数(哪怕是 0)当总数,
    就是把一次查询失败伪装成空结果(r47 同一个病)。
    """
    st, _ = ws
    real_conn = st._conn

    class _Boom:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if "COUNT(*)" in str(sql):
                raise RuntimeError("FTS5 暂时不可用")
            return self._inner.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    monkeypatch.setattr(st, "_conn", lambda: _Boom(real_conn()))
    assert st.count_search("sites", "probe-marker") is None, (
        "数不出来却返回了一个 int —— 那会被 CLI 当成真实总数印出去")


def test_unknown_total_is_stated_not_guessed(ws, monkeypatch):
    """总数算不出来时,提示行必须说 UNKNOWN,不能也不该猜一个数"""
    # 只能 patch **类**上的方法:`main()` 内部会
    # `Storage(workspace=args.workspace)` 新建一个实例,patch 测试里的
    # 那个实例对 CLI 毫无影响 —— 首版就是这么写的,于是判据测的是
    # 补丁没生效时的行为,看起来像实现没做(决策 #3)。
    # `count_search` 返回 None 的**原因**由上面那条判据负责。
    monkeypatch.setattr(Storage, "count_search",
                        lambda self, table, keyword: None)
    out = _run("search", "sites", "probe-marker")
    n = _notices(out)
    assert n, "总数算不出来时更不能没有提示"
    assert "UNKNOWN" in n[0], f"数不出来却报了个数:{n[0]}"
    assert "200" not in n[0].replace("202", ""), (
        f"数不出来却报了个具体总数:{n[0]}")


# ── 三、机制不许分叉 ──

def test_limit_notice_and_need_are_shared_by_all_three_commands():
    """`query`/`search`/`monitor changes` 都调那两个共用函数

    r55 在 `monitor changes` 上手写过一份,r56 给另外两条加上 ——
    如果那份手写还留着,就是两处手抄同一段话,迟早漂(决策 #9)。
    """
    tree = ast.parse((REPO / "arl_lite" / "cli.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}
    direct = {name: {sub.func.id for sub in ast.walk(fn)
                     if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)}
              for name, fn in funcs.items()}

    def reaches(name: str, target: str) -> bool:
        """这个命令能到达 `target` 吗 —— 直接调,或经**一层**包装

        `cmd_query` / `cmd_search` 走 `_print_table_limited`(表格输出),
        `cmd_monitor_changes` 是逐行输出、用不了那个包装,于是直接调
        `_limit_notice_text`。所以判据守的是「**走到同一份机制**」,
        不是「必须直接调用」—— 形状不是契约,机制才是(决策 #12)。
        """
        if target in direct.get(name, ()):
            return True
        return ("_print_table_limited" in direct.get(name, ())
                and target in direct.get("_print_table_limited", ()))

    for cmd in ("cmd_query", "cmd_search", "cmd_monitor_changes"):
        for target in ("_needs_total", "_limit_notice_text"):
            assert reaches(cmd, target), (
                f"{cmd} 走不到 {target}(它直接调了 "
                f"{sorted(direct.get(cmd, ()))})—— 三个命令必须用同一份"
                f"判定和文案,否则会各自漂")


def _static_text(node) -> str:
    """f-string 里那些**不是插值**的字面片段,按顺序拼起来

    r80:原来这里用 `" of " in ast.unparse(n)` —— 那是在源码**拼写**里找,
    等于把代码当文本。判据要问的是「这个 f-string 印出来长什么样」,
    所以直接读它的 Constant 片段。
    """
    return "".join(v.value for v in node.values
                   if isinstance(v, ast.Constant) and isinstance(v.value, str))


def _inserts(node, name: str) -> bool:
    """这个 f-string 里有没有真的插了 `name` 这个变量

    注意是「插值」,不是「写着 `{name}` 这几个字」——
    `f"{total}"` 里 `total` 是 FormattedValue,`f"{{total}}"` 里不是。
    """
    return any(isinstance(v, ast.FormattedValue)
               and getattr(v.value, "id", None) == name
               for v in node.values)


def test_monitor_changes_no_longer_handwrites_its_own_notice():
    """`cmd_monitor_changes` 里不该再有手写的 `f\"[{shown} of {total}` 文案"""
    tree = ast.parse((REPO / "arl_lite" / "cli.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "cmd_monitor_changes")
    handmade = [ast.unparse(n) for n in ast.walk(fn)
                if isinstance(n, ast.JoinedStr)
                and _inserts(n, "total") and " of " in _static_text(n)]
    assert not handmade, (
        f"cmd_monitor_changes 里还有手写的总数文案:{handmade} —— "
        f"r55 那份手写已经被 r56 的共用函数取代了")


def test_storage_shares_the_where_builder_with_its_counter():
    """`query`/`count_rows` 共用 `_query_where`,`search`/`count_search` 共用 `_search_where`"""
    tree = ast.parse((REPO / "arl_lite" / "db" / "storage.py")
                     .read_text(encoding="utf-8"))
    q_users: set[str] = set()
    s_users: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_query_where"):
                q_users.add(node.name)
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_search_where"):
                s_users.add(node.name)
    assert {"query", "count_rows"} <= q_users, (
        f"_query_where 的使用者只有 {sorted(q_users)}")
    assert {"search", "count_search"} <= s_users, (
        f"_search_where 的使用者只有 {sorted(s_users)}")


# ── 四、共用函数本身的行为 ──

def test_needs_total_only_when_rows_hit_the_limit():
    """行数没顶到 limit 就**不用**去数 —— 没顶到显然没截断"""
    assert _needs_total(10, 50) is False, "没顶到 limit 就别去数"
    assert _needs_total(50, 50) is True, "正好顶到 limit 要去数"
    # 实际上 `shown > limit` 不该出现(列表最多就 limit 行)。但真出现了
    # 时返回 True 是**保险**:宁可多数一次,也不要把一个说不清的数字
    # 当成完整总数报出去。首版这里断言的是 False,方向反了。
    assert _needs_total(51, 50) is True, "超了也要去数 —— 宁可多报不可少报"


def test_notice_text_says_nothing_invented():
    """三种情形各说各的,尤其 `None` 不能被当成 0"""
    assert "of 200" in _limit_notice_text(50, 200, 50)
    assert "200 row(s)" in _limit_notice_text(200, 200, 500)
    assert "UNKNOWN" in _limit_notice_text(50, None, 50)
    assert " of " not in _limit_notice_text(200, 200, 500), "没截断时不喊「其中一部分」"


def test_notice_text_respects_the_noun():
    """`monitor changes` 说 change、`search` 说 row —— 量词要对得上语境"""
    assert "change(s)" in _limit_notice_text(50, 200, 50, "change")
    assert "row(s)" in _limit_notice_text(50, 200, 50)


# ── 五、`--json` 那条路故意不动 ──

def test_json_stays_machine_parseable(ws):
    """`--json` 不加那句人话

    JSON 消费方要能直接 parse。加进去就破坏可解析性。
    这条把「故意不修」钉住,免得下一轮有人好心加。
    """
    cases = [(("search", "sites", "probe-marker", "--format", "json"), 50),
             (("query", "sites", "--limit", "10", "--format", "json"), 10)]
    for argv, n_expected in cases:
        out = _run(*argv)
        payload = json.loads(out)          # 解析失败会直接抛
        assert isinstance(payload, list) and len(payload) == n_expected, (
            f"{argv} 应该给出 {n_expected} 条,实际 {len(payload)}")
        assert "[i]" not in out, f"JSON 里混进了人类可读的那行提示:{argv}"
