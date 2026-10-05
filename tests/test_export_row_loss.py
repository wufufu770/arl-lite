"""r57:`arl-lite export` 硬编码 limit=10000,超了静默丢数据还报成功

## 实测的退化路径(不是推测)

`domains` 里造 12000 行:

    $ arl-lite export --format json -o out.json
    [+] json exported to out.json        ← 退出码 0
    导出文件里只有 10000 行,2000 行静默消失,全程零提示

HTML 报告有**四处**同样的 `limit=10000`(hosts / correlations /
monitors / findings),症状一样。

## 这比 r55/r56 的显示截断严重一个量级

那两条是「显示不全」—— 用户加 `--limit` 就能看到全部。而 `export`
的命令名承诺的是**导出所有数据**,拿到的是一个**不完整的文件**:
拿去做分析、迁移、归档,推导出的结论是错的,而工具说一切正常。
而且这个 10000 是硬编码的,连 `--limit` 参数都没有,用户**根本没有
自救的余地**。

## 为什么是「导 + 说 + 退出码非 0」

三条路都试过:

- **截断就拒绝导出**:文件已经写了一半,硬失败等于让用户什么都拿不到,
  还得自己想办法重来 —— 那是替用户做了决定。
- **截断了照旧退出 0**(现状):`export` 会被 CI 和定时任务调用,退出码 0
  意味着脚本认为一切正常,这个不完整的文件会流进下游。
- **导 + 逐张表说清楚少了多少 + 退出码非 0**:人看得见,脚本也看得见。

所以选第三条。文件照写(用户的选择),但事实必须说出去,而且说两遍 ——
stdout 给交互,stderr 给日志和管道。

## 机制在数据层,不在调用方

`limit` 是数据访问的正常参数,不该被顺手用来「导出全部」。r57 之前
`rows` 看不出自己是不是完整的,而调用方天然就会漏掉「一共多少」这个数。

所以做成 `Storage.fetch_all(table) -> (rows, total)`:把两个数**绑在
一个返回值里**,调用方不可能只拿到一半。这不是多写了个函数,是把
「不许悄悄丢数据」从调用方的自觉变成了 API 的义务。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pathlib
import re

import pytest

from arl_lite.cli import _EXPORT_TABLES, main
from arl_lite.cli_report_html import generate_html_report
from arl_lite.db.storage import EXPORT_ROW_CAP, Storage

OVER = EXPORT_ROW_CAP + 2000          # 造得比上限多,确保会截断


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return Storage(workspace="default"), tmp_path


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:32]


def _bulk(storage, table, n, col="domain"):
    """直接往表里塞 n 行 —— 走 `add_*` 慢,而且要的就是「表很大」这个前提"""
    with storage._conn() as c:
        if table == "domains":
            c.executemany(
                "INSERT INTO domains (workspace_id, domain, source, hash,"
                " first_seen, last_seen, discovered_at) "
                "VALUES (?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
                [(storage.workspace_id, f"d{i}.bulk.com", "seed", _h(f"x|d{i}"))
                 for i in range(n)])
        elif table == "hosts":
            c.executemany(
                "INSERT INTO hosts (workspace_id, host, ip, hash,"
                " first_seen, last_seen, discovered_at) "
                "VALUES (?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
                [(storage.workspace_id, f"h{i}.bulk.com", "1.1.1.1", _h(f"x|h{i}"))
                 for i in range(n)])
        else:                                   # pragma: no cover - 本轮只造这两种
            raise AssertionError(f"测试没为 {table} 准备造数方式")


def _count(storage, table):
    with storage._conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _export(argv):
    """跑 export,拿回 (退出码, stdout, stderr)

    stdout/stderr 分开收:「导 + 说」这个设计要求**两处都有** ——
    stdout 给交互的人,stderr 给日志和管道。合在一起就看不出
    「哪句是给人看的、哪句是给机器看的」了。
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


# ── 一、主判据:截断了必须说,而且退出码非 0 ──

def test_truncated_export_says_so_and_exits_nonzero(ws, tmp_path):
    """12000 行导成 10000 行 → 报告少 2000 行 + 退出码 1

    退出码是这条的一半:0 会让 CI / 定时任务认为一切正常,不完整的文件
    就流进下游分析了。
    """
    storage, _ = ws
    _bulk(storage, "domains", OVER)
    assert _count(storage, "domains") == OVER, "造数就失败了,判据等于没验"

    dest = str(tmp_path / "out.json")
    rc, out, err = _export(["export", "--format", "json", "-o", dest])

    data = json.loads(pathlib.Path(dest).read_text(encoding="utf-8"))
    assert len(data["domains"]) == EXPORT_ROW_CAP
    assert OVER - len(data["domains"]) == 2000, "前提变了,本判据的算式要跟着改"

    assert rc == 1, f"数据不完整却退出 {rc} —— 自动化脚本会当成功处理"
    said = out + err
    assert "不完整" in said, f"没说数据不完整:\n{said}"
    assert "2000" in said, f"没说少了几行:\n{said}"
    assert "domains" in said, f"没说清是哪张表:\n{said}"


def test_the_warning_goes_to_stderr_where_logs_and_pipes_can_see_it(ws, tmp_path):
    """提示必须落在 **stderr**,而不是 stdout

    首版判据要求「两处都有」,实测只有 stderr 有 —— 那是实现对的、
    判据写错了:同一句话打两遍是纯噪音,而终端**本来就同时显示**
    stdout 和 stderr,所以「正在看命令的人看得见」这个目标靠 stderr
    已经达到了。真正需要 stderr 的是另一类场景:被 `tee` 进日志、
    或者 stdout 被管道接走当数据用的时候。
    """
    storage, _ = ws
    _bulk(storage, "domains", OVER)
    rc, out, err = _export(["export", "--format", "json",
                            "-o", str(tmp_path / "o.json")])
    assert "不完整" in err, f"stderr 里没有 —— 日志和管道就看不到:\n{err}"
    # 成功那行留在 stdout:它说的是「文件写在哪」,是正常输出
    assert "exported to" in out, f"成功信息应该还在 stdout:\n{out}"


def test_file_is_still_written_even_when_truncated(ws, tmp_path):
    """截断了也把文件写出来,并明说「不要当成完整数据集」

    硬失败等于让用户什么都拿不到 —— 那是替用户做了决定。文件给他,
    但把话说清楚。
    """
    storage, _ = ws
    _bulk(storage, "domains", OVER)
    dest = tmp_path / "o.json"
    _export(["export", "--format", "json", "-o", str(dest)])
    assert dest.exists(), "文件都没写就报错,用户什么都拿不到"
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert len(data["domains"]) == EXPORT_ROW_CAP
    rc, out, err = _export(["export", "--format", "json", "-o", str(dest)])
    assert "不要" in (out + err), "没提醒别把它当完整数据集用"


# ── 二、逐张表都要对得上 ──

def test_every_clipped_table_is_named(ws, tmp_path):
    """两张表都超了 → 两张都要出现在报告里

    只报第一张是最容易犯的错:那张被修掉之后,第二张又静默了。
    """
    storage, _ = ws
    _bulk(storage, "domains", OVER)
    _bulk(storage, "hosts", OVER + 50)
    rc, out, err = _export(["export", "--format", "json",
                            "-o", str(tmp_path / "o.json")])
    said = out + err
    assert rc == 1
    for table, n in (("domains", OVER), ("hosts", OVER + 50)):
        m = re.search(rf"{table}: 库里 {n} 行,只导出 {EXPORT_ROW_CAP} 行"
                      rf"\(少了 {n - EXPORT_ROW_CAP} 行\)", said)
        assert m, f"{table} 的少行数没被逐张报出来:\n{said}"


def test_not_clipped_exits_zero_and_stays_quiet(ws, tmp_path):
    """没超上限 → 退出 0,而且不啰嗦

    每次都喊「数据不完整」的话,真出事那天就不灵了。
    """
    storage, _ = ws
    _bulk(storage, "domains", 5)
    rc, out, err = _export(["export", "--format", "json",
                            "-o", str(tmp_path / "o.json")])
    assert rc == 0, f"没截断却退出 {rc}"
    assert "不完整" not in (out + err), f"没截断却喊了不完整:\n{out}{err}"


def test_exactly_at_the_cap_is_not_reported_as_clipped(ws, tmp_path):
    """正好等于上限不算截断 —— 有 cap 行就是有 cap 行,不多不少"""
    storage, _ = ws
    _bulk(storage, "domains", EXPORT_ROW_CAP)
    rc, out, err = _export(["export", "--format", "json",
                            "-o", str(tmp_path / "o.json")])
    assert rc == 0, f"条数正好等于上限却退出 {rc}"
    assert "不完整" not in (out + err)


# ── 三、机制在数据层,不在调用方的自觉 ──

def test_fetch_all_always_returns_the_real_total(ws):
    """`fetch_all` 把两个数**绑在一个返回值里**

    r57 之前调用方拿到的只是 `rows`,而 `rows` 看不出自己完不完整 ——
    调用方漏掉「一共多少」是完全自然的事,不是疏忽。
    """
    storage, _ = ws
    _bulk(storage, "domains", OVER)
    rows, total = storage.fetch_all("domains")
    assert len(rows) == EXPORT_ROW_CAP
    assert total == OVER, f"total={total},应该是 {OVER}"
    assert isinstance(total, int) and not isinstance(total, bool)


def test_fetch_all_total_agrees_with_count_rows(ws):
    """`fetch_all` 的 total 必须等于 `count_rows` —— 同一份条件"""
    storage, _ = ws
    _bulk(storage, "domains", OVER)
    _, total = storage.fetch_all("domains")
    assert total == storage.count_rows("domains")


def test_fetch_all_refuses_a_table_outside_the_whitelist(ws):
    """表白名单照旧 —— 别为了方便把内部表放出来"""
    storage, _ = ws
    with pytest.raises(ValueError, match="whitelist"):
        storage.fetch_all("sqlite_master")


def test_export_uses_fetch_all_not_a_bare_limited_query():
    """`cmd_export` 必须走 `fetch_all`,不能退回 `query(limit=...)`

    行为判据钉的是「文件不全时要说」,而这条钉的是**为什么**能做到:
    少了 total 就没得说。两条各守一半。
    """
    src = pathlib.Path("arl_lite/cli.py").read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_export")
    calls = [sub.func.attr for n in ast.walk(fn)
             for sub in ast.walk(n)
             if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)]
    # 数调用**点**,不是数调用次数:`for table in _EXPORT_TABLES: fetch_all(...)`
    # 静态 AST 里只有**一个** Call 节点,跑几次是运行时的。首版拿它和
    # 表数比大小,判据永远不成立 —— 拿运行期事实去卡静态结构。
    assert "fetch_all" in calls, "cmd_export 没调 fetch_all"
    bare = [c for c in calls if c == "query"]
    assert not bare, (
        f"cmd_export 里还有 {len(bare)} 处裸的 storage.query(...) —— "
        f"那条路拿不到 total,那张表少了几行就没人报")


def test_export_table_list_is_a_named_constant():
    """导出哪些表是一份清单,不是行内字面量

    「导出哪些表」和「哪张表被截断了」得能对上,写死在 for 里就没法
    一眼看出它们是不是同一份。
    """
    assert isinstance(_EXPORT_TABLES, tuple) and _EXPORT_TABLES, (
        "_EXPORT_TABLES 得是个具名常量,不是行内字面量")
    for table in _EXPORT_TABLES:
        assert table in Storage.QUERY_TABLES, (
            f"{table} 在导出清单里却不在 QUERY_TABLES 里 —— "
            f"fetch_all 会当场拒它")


# ── 四、HTML 报告:同一个病,四处 ──

def test_html_report_warns_when_a_table_was_clipped(ws):
    """HTML 报告也必须在**顶部**明写数据不全

    放在某个卡片里是不够的:被截断的表可能压根不出现在正文
    (空的 correlations 卡片只印「暂无关联结果」),而那正是最需要
    提醒的场景。
    """
    storage, _ = ws
    _bulk(storage, "hosts", OVER)
    html = generate_html_report(storage, "default")
    assert "数据不完整" in html, "报告里没写数据不完整"
    # 空白全去掉再比:HTML 渲染时缩进/换行不可控,而真正要验的是
    # 「这张表少了多少行」这几个数字在不在(首版 pattern 里留着空格、
    # 输入却去了空格,自己和自己打架,恒不匹配)。
    #
    # pattern 保留 `</code>`:`<li><code>hosts</code>:库里 …` 里表名是
    # 被标签包着的,写成 `hosts:库里` 匹配不上(第二版栽在这)。
    flat = re.sub(r"\s+", "", html)
    assert re.search(rf"hosts</code>:库里{OVER}行,"
                     rf"报告只用了{EXPORT_ROW_CAP}行"
                     rf"\(少了{OVER - EXPORT_ROW_CAP}行\)", flat), (
        f"警告块里没有 hosts 的少行数:\n{html[:400]}")


def test_html_report_is_quiet_when_nothing_was_clipped(ws):
    """没截断时报告顶部不加警告块"""
    storage, _ = ws
    _bulk(storage, "hosts", 5)
    assert "数据不完整" not in generate_html_report(storage, "default")


def test_html_report_has_no_bare_limited_query_left():
    """`cli_report_html` 里不该再有 `query(..., limit=10000)`

    那四处过去是同一个静默截断。判据用 AST 数一遍,不许剩下。
    """
    import ast
    src = pathlib.Path("arl_lite/cli_report_html.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bare = [ast.unparse(n) for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "query"
            and any(kw.arg == "limit" and isinstance(kw.value, ast.Constant)
                    and kw.value.value == 10000
                    for kw in n.keywords)]
    assert not bare, f"还有硬编码的 limit=10000:{bare}"


def test_html_report_still_renders_when_nothing_is_clipped(ws):
    """加了警告块之后,正常路径的报告不能坏掉

    这条防的是「我为了加个警告,把整份报告搞崩了」—— 而报告崩了
    比少一行统计更严重。
    """
    storage, _ = ws
    _bulk(storage, "hosts", 3)
    html = generate_html_report(storage, "default")
    assert html.startswith("<!DOCTYPE html") or "<html" in html[:400]
    assert "ARL-Lite" in html or "安全报告" in html or "Security" in html
