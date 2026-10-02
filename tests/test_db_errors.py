"""sqlite3 IntegrityError 分类 —— 反向验证

**恒真的测试比没有更糟**。这个文件的核心不是"新代码能正确分类",
而是"旧代码会错得很离谱,且新代码确实拦住了"。

全部用例**完全离线**:用 `:memory:` 库,不碰任何真实 schema,
也不需要网络。
"""
from __future__ import annotations

import sqlite3

import pytest

from arl_lite.db.errors import (
    CONSTRAINT, DUPLICATE, UNKNOWN, classify_integrity_error,
)


def _capture(conn, sql, params=()):
    """执行 SQL 并返回抛出的 IntegrityError;没抛就返回 None"""
    try:
        conn.execute(sql, params)
    except sqlite3.IntegrityError as e:
        return e
    return None


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("CREATE TABLE t (hash TEXT UNIQUE, v TEXT NOT NULL)")
    c.execute("CREATE TABLE w (hash TEXT, v TEXT, PRIMARY KEY(hash)) WITHOUT ROWID")
    c.execute("CREATE TABLE f (a TEXT, b TEXT REFERENCES t(hash))")
    c.execute("CREATE TABLE k (hash TEXT CHECK(hash <> 'bad'), v TEXT)")
    c.execute("INSERT INTO t VALUES (?, ?)", ("x", "1"))
    yield c
    c.close()


# =====================================================================
# 正向:五种约束各自归对类
# =====================================================================


def test_unique_index_violation_is_duplicate(conn):
    e = _capture(conn, "INSERT INTO t VALUES (?, ?)", ("x", "2"))
    assert e is not None
    assert classify_integrity_error(e) == DUPLICATE


def test_primary_key_violation_is_duplicate(conn):
    # 第一次插入成功,第二次才是冲突
    assert _capture(conn, "INSERT INTO w VALUES (?, ?)", ("p1", "2")) is None
    e = _capture(conn, "INSERT INTO w VALUES (?, ?)", ("p1", "3"))
    assert e is not None
    assert classify_integrity_error(e) == DUPLICATE


def test_not_null_violation_is_constraint(conn):
    e = _capture(conn, "INSERT INTO t VALUES (?, ?)", ("y", None))
    assert e is not None
    assert classify_integrity_error(e) == CONSTRAINT


def test_foreign_key_violation_is_constraint(conn):
    e = _capture(conn, "INSERT INTO f VALUES (?, ?)", ("zz", "2"))
    assert e is not None
    assert classify_integrity_error(e) == CONSTRAINT


def test_check_violation_is_constraint(conn):
    e = _capture(conn, "INSERT INTO k VALUES (?, ?)", ("bad", "2"))
    assert e is not None
    assert classify_integrity_error(e) == CONSTRAINT


# =====================================================================
# 核心:旧逻辑会错的三个场景
# =====================================================================


def _old_logic(exc) -> bool:
    """原 storage.py 的判定,照抄回来当靶子"""
    msg = str(exc).lower()
    return "unique" in msg or "conflict" in msg


def test_column_named_unique_flag_is_not_mistaken_for_duplicate():
    """NOT NULL 列名叫 unique_flag 时,旧逻辑误判成重复

    这是最要命的一个:误判 = 静默 skip = 丢数据还不报错。
    列名是人会起的,今天没有不代表明天没有。
    """
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE s (hash TEXT, unique_flag TEXT NOT NULL)")
    c.execute("CREATE UNIQUE INDEX s_uniq ON s(hash)")
    c.execute("INSERT INTO s VALUES (?, ?)", ("h1", "x"))
    e = _capture(c, "INSERT INTO s VALUES (?, ?)", ("h2", None))
    c.close()

    assert e is not None
    # 旧逻辑确实被骗了 —— 先证明这个 bug 真实存在
    assert _old_logic(e) is True, "旧逻辑没被骗,说明本用例没打到点上"
    assert "NOT NULL constraint failed" in str(e)
    # 新逻辑不上当
    assert classify_integrity_error(e) == CONSTRAINT


def test_column_named_conflict_state_is_not_mistaken_for_duplicate():
    """NOT NULL 列名叫 conflict_state —— 旧逻辑同样被骗"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE s (hash TEXT, conflict_state TEXT NOT NULL)")
    e = _capture(c, "INSERT INTO s VALUES (?, ?)", ("h1", None))
    c.close()

    assert e is not None
    assert _old_logic(e) is True
    assert classify_integrity_error(e) == CONSTRAINT


def test_check_expression_containing_unique_is_not_mistaken_for_duplicate():
    """CHECK 表达式里带 unique —— 旧逻辑被骗,新逻辑不上当"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE s (hash TEXT, v INT, CHECK(v <> 1 OR hash <> 'unique'))")
    e = _capture(c, "INSERT INTO s VALUES (?, ?)", ("unique", 1))
    c.close()

    assert e is not None
    assert _old_logic(e) is True
    assert classify_integrity_error(e) == CONSTRAINT


# =====================================================================
# 降级路径:没有错误码的老 Python
# =====================================================================


class _NoCodeError(sqlite3.IntegrityError):
    """模拟 Python < 3.11:`sqlite_errorcode` 拿不到

    3.11 起 sqlite3.Error 才带这个属性。老版本上 getattr 返回 None,
    classify_integrity_error 应当降级到锚定前缀的报文匹配。
    """

    sqlite_errorcode = None


def _no_code(msg: str) -> sqlite3.IntegrityError:
    """构造一个不带有效错误码的 IntegrityError"""
    return _NoCodeError(msg)


def test_fallback_classifies_unique_message_as_duplicate():
    assert classify_integrity_error(_no_code("UNIQUE constraint failed: t.hash")) == DUPLICATE


def test_fallback_anchors_on_prefix_not_anywhere_in_message():
    """降级路径必须锚在开头——列名只出现在 ': ' 之后

    报文的 ": " 之后是列名/约束表达式,SQLite 自己会原样填进去。
    只要锚在开头,列名叫 unique_flag / conflict_state 都碰不到判定。
    """
    got = classify_integrity_error(_no_code("NOT NULL constraint failed: s.unique_flag"))
    assert got != DUPLICATE, "列名里的 unique 把报文匹配骗过去了"
    assert got == UNKNOWN


def test_fallback_recognises_primary_key_wording():
    assert classify_integrity_error(_no_code("PRIMARY KEY must be unique")) == DUPLICATE


def test_fallback_is_whitespace_insensitive():
    """空白归一化:包装层重新格式化不该改变分类结果"""
    for variant in (
        "  UNIQUE constraint failed: t.hash  ",
        "unique  constraint   failed: t.hash",
        "unique\tconstraint\nfailed: t.hash",
    ):
        assert classify_integrity_error(_no_code(variant)) == DUPLICATE, variant


def test_fallback_matches_real_unique_message_exactly():
    """降级路径必须认得真 SQLite 的原文——不能只在自造的报文上work"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t (hash TEXT UNIQUE, v TEXT)")
    c.execute("INSERT INTO t VALUES (?, ?)", ("x", "1"))
    e = _capture(c, "INSERT INTO t VALUES (?, ?)", ("x", "2"))
    c.close()
    assert e is not None
    # 把真异常的码摘掉,只看报文
    assert classify_integrity_error(_no_code(str(e))) == DUPLICATE


def test_unknown_message_never_guesses_duplicate():
    """认不出来就归 UNKNOWN,绝不猜成重复

    猜错的代价不对称:误判成重复 = 静默丢数据;
    误判成真错 = 多记一条 error,调用方看得见。
    """
    got = classify_integrity_error(
        _no_code("something entirely unexpected happened here")
    )
    assert got == UNKNOWN
    assert got != DUPLICATE


def test_empty_message_is_unknown():
    assert classify_integrity_error(_no_code("")) == UNKNOWN


# =====================================================================
# 与 storage.bulk_insert 的集成
# =====================================================================


def _fresh_storage(tmp_path, ddl: str):
    """建一个 Storage,并把 sites 表换成测试指定的结构

    走真实的 Storage 构造(会初始化真 schema),再覆盖 sites —— 这样
    bulk_insert 读到的 valid_cols 来自真实 PRAGMA,而不是凭空捏的。
    """
    from arl_lite.db.storage import Storage

    st = Storage(workspace="test", workspace_root=tmp_path)
    c = sqlite3.connect(str(st.db_path))
    c.execute("DROP TABLE IF EXISTS sites")
    c.executescript(ddl)
    c.commit()
    c.close()
    return st


def test_bulk_insert_reports_not_null_error_instead_of_silently_skipping(tmp_path):
    """端到端:bulk_insert 必须把 NOT NULL 失败报进 errors

    这是整个修复的落点。旧代码在这里会返回 skipped=1, errors=[],
    调用方以为一切正常——数据就这么没了。
    """
    st = _fresh_storage(tmp_path, """
        CREATE TABLE sites (
            workspace_id INTEGER,
            hash TEXT,
            url TEXT,
            unique_flag TEXT NOT NULL
        );
        CREATE UNIQUE INDEX sites_uniq ON sites(workspace_id, hash);
    """)

    rows = [
        {"workspace_id": 1, "hash": "h1", "url": "http://a", "unique_flag": "ok"},
        # unique_flag 缺失 -> NOT NULL 失败,必须报出来
        {"workspace_id": 1, "hash": "h2", "url": "http://b"},
    ]
    res = st.bulk_insert("sites", rows, on_conflict="ignore")

    assert res["errors"], f"NOT NULL 失败被静默吞了,结果={res}"
    assert any("NOT NULL" in e for e in res["errors"]), res["errors"]
    # 关键:它不能被算成"重复"
    assert res["skipped"] == 0, f"NOT NULL 失败被误判成重复了,结果={res}"
    assert res["inserted"] == 1


def test_bulk_insert_still_skips_real_duplicates(tmp_path):
    """修 bug 不能把真重复也报成 error —— 那会让批量导入全线飘红"""
    st = _fresh_storage(tmp_path, """
        CREATE TABLE sites (
            workspace_id INTEGER,
            hash TEXT,
            url TEXT
        );
        CREATE UNIQUE INDEX sites_uniq ON sites(workspace_id, hash);
    """)

    row = {"workspace_id": 1, "hash": "h1", "url": "http://a"}
    r1 = st.bulk_insert("sites", [row], on_conflict="ignore")
    r2 = st.bulk_insert("sites", [row], on_conflict="ignore")

    assert r1["inserted"] == 1 and r1["errors"] == []
    assert r2["skipped"] == 1, f"真重复没被识别成重复,结果={r2}"
    assert r2["errors"] == [], f"真重复被误报成 error,结果={r2}"


def test_bulk_insert_old_logic_would_have_swallowed_the_error(tmp_path):
    """证伪:同一份数据,旧逻辑会判成重复,新逻辑判成真错

    证明"静默丢数据"不是想象出来的。
    """
    st = _fresh_storage(tmp_path, """
        CREATE TABLE sites (
            workspace_id INTEGER,
            hash TEXT,
            url TEXT,
            unique_flag TEXT NOT NULL
        );
    """)

    # 缺 unique_flag -> NOT NULL 失败
    c = sqlite3.connect(str(st.db_path))
    e = _capture(
        c, "INSERT INTO sites (workspace_id, hash, url) VALUES (?, ?, ?)",
        ("1", "h1", "http://a"),
    )
    c.close()

    assert e is not None
    assert _old_logic(e) is True, "旧逻辑没被骗,证伪用例没打到点上"
    assert classify_integrity_error(e) == CONSTRAINT
    assert st.bulk_insert("sites", [{"hash": "h1", "url": "http://a"}],
                          on_conflict="ignore")["errors"]


# =====================================================================
# 证伪:掏空修复,测试必须立刻红
# =====================================================================


def test_falsification_old_string_logic_breaks_the_column_name_test():
    """把 classify 换回旧的字符串逻辑,列名用例必须失败

    没有这条,前面那些分类测试可能只是在测一个恒真函数。
    """
    def old_classify(exc) -> str:
        msg = str(exc).lower()
        return DUPLICATE if ("unique" in msg or "conflict" in msg) else CONSTRAINT

    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE s (hash TEXT, unique_flag TEXT NOT NULL)")
    c.execute("INSERT INTO s VALUES (?, ?)", ("h1", "x"))
    e = _capture(c, "INSERT INTO s VALUES (?, ?)", ("h2", None))
    c.close()

    assert e is not None
    # 旧逻辑在这里给出错误答案
    assert old_classify(e) == DUPLICATE
    # 新逻辑给出正确答案
    assert classify_integrity_error(e) == CONSTRAINT
    # 两者确实不同 —— 说明测试不是恒真的
    assert old_classify(e) != classify_integrity_error(e)
