"""r46:变更事件记了 diff,`monitor changes` 却从不显示

## r45 顺带查实的

`cmd_monitor_changes` 每行只打印 `[类型] 资产类型 hash[:16] 时间`。
所以 r44 刚做出来的字段级变更,用户在 CLI 上看到的是

    [ADDRESS_CHANGED   ] host       68cd3922cdc45da2 2026-10-03T05:04:56

**看不出 IP 从哪个换到了哪个。** 存了 payload 却不给看,等于把「记下来」
当成了「告诉人」。

## 三种变更的 diff 形态并不一样(实测)

| 变更 | before_value | after_value | diff |
|---|---|---|---|
| `ADDRESS_CHANGED` 等字段级 | `{"ip": "1.1.1.1"}` | `{"ip": "2.2.2.2"}` | 有 |
| `NEW_ASSET` | NULL | 整行资产 | **NULL** |
| `DISAPPEARED` | 整行资产 | NULL | **NULL** |

后两种的 `diff` 是 NULL 属于**正常** —— 它们本来就只有单边快照。
所以渲染时不能只管「有 diff 就显示」:那样「本来就没有可比的」和
「本该存却没存下来」在输出上长得一模一样,而后者是 bug。
本文件把两者显式分开。

## 标识为什么查表优先

字段级变更的快照**只装变动的那一个字段**(`{"ip": "2.2.2.2"}`),
里面没有资产身份。只看快照的话,host 的标识会退化成 `ip` ——
而 ip 正是会变的那个字段,于是「标识」每次都跟着变,比给个稳定
hash 还误导人。所以顺序是 表 → 快照 → hash。

## `--json`

想接自动化的人现在只能去读 SQLite。JSON 走机器消费,payload 原样
带出,不经过给人看的那些渲染和截断。
"""
from __future__ import annotations

import contextlib
import io
import json

import pytest

from arl_lite.cli import main
from arl_lite.core.monitor import record_change
from arl_lite.db.storage import Storage


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """一个独立工作区,并让 `main()` 里的 Storage 也指到它

    `cmd_monitor_changes` 自己 `Storage(workspace=...)`,读的是
    `Path.home()/.arl-lite/workspaces` —— 不改 HOME 的话它会看
    另一个库,测试就变成在测一个空 workspace。
    """
    # 只改 HOME,**不要**给 fixture 的 Storage 传 workspace_root ——
    # `cmd_monitor_changes` 自己 `Storage(workspace=...)`,走的是
    # `Path.home()/.arl-lite/workspaces`。两边不是同一个目录的话,
    # 测试就在测一个空 workspace(实测:全部报 "no changes")。
    monkeypatch.setenv("HOME", str(tmp_path))
    st = Storage(workspace="t")
    return st, tmp_path


def _run(*argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(list(argv))
    return buf.getvalue()


def _seed(st) -> dict:
    """造三种形态各一条,外加一条真实的资产(供查表取标识)"""
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.add_site(1, "http://shop.example.com", "shop.example.com",
                "1.1.1.1", 80, "http", title="Shop")
    with st._conn() as c:
        hh = c.execute("SELECT hash FROM hosts").fetchone()["hash"]
        sh = c.execute("SELECT hash FROM sites").fetchone()["hash"]
    record_change(st, "host", "NEW_ASSET", "h-new",
                  after={"hash": "h-new", "host": "a.example.com"})
    record_change(st, "host", "DISAPPEARED", "h-gone",
                  before={"hash": "h-gone", "host": "b.example.com"})
    record_change(st, "host", "ADDRESS_CHANGED", hh,
                  before={"ip": "1.1.1.1"}, after={"ip": "2.2.2.2"})
    record_change(st, "site", "TITLE_CHANGED", sh,
                  before={"title": "Shop"}, after={"title": "Shop 2"})
    return {"host_hash": hh, "site_hash": sh}


# ── 主判据:变了什么,得看得见 ──

def test_field_change_shows_before_and_after(ws):
    """IP 从 1.1.1.1 换到 2.2.2.2,输出里必须看得到这两个值"""
    st, _ = ws
    _seed(st)
    out = _run("monitor", "changes", "-w", "t")
    assert "1.1.1.1" in out and "2.2.2.2" in out, (
        f"输出里看不到变更前后的值:\n{out}"
    )
    assert "ip:" in out, f"看不出是哪个字段变了:\n{out}"


def test_identity_is_readable_not_a_hash(ws):
    """标识要是人能认的东西,不是 hash 前缀

    `68cd3922cdc45da2` 对人没有意义,而 `web.example.com` 本来就在库里。
    """
    st, _ = ws
    _seed(st)
    out = _run("monitor", "changes", "-w", "t")
    assert "web.example.com" in out, f"标识还是认不出来:\n{out}"
    assert "http://shop.example.com" in out, f"site 的标识认不出来:\n{out}"


def test_identity_does_not_drift_with_the_changed_field(ws):
    """标识不能是「会变的那个字段的值」

    字段级变更的快照里只有变动的那一个字段(`{"ip": "2.2.2.2"}`),
    只看快照的话 host 的标识会退化成 ip —— 而 ip 正是会变的那个,
    于是「标识」每次都跟着变,比给个稳定 hash 还误导。
    这条把「必须先查表」钉住:快照里有 ip,表里有 web.example.com,
    输出该给后者。
    """
    st, _ = ws
    _seed(st)
    line = next(ln for ln in _run("monitor", "changes", "-w", "t").splitlines()
                if "ADDRESS_CHANGED" in ln)
    assert "web.example.com" in line, f"标识退化成了会变的字段值:{line}"
    assert "2.2.2.2" not in line.split("→")[0].split("]")[-1], (
        f"标识行里混进了字段值:{line}"
    )


# ── diff 为 NULL 的两种情形必须分得开 ──

def test_single_sided_snapshots_say_so(ws):
    """`NEW_ASSET` / `DISAPPEARED` 的 diff 是 NULL,但那是**正常**的

    输出要写明白「本来就没有可比的另一边」,而不是显示成空 ——
    空的那行,和「本该存却没存下来」长得一模一样。
    """
    st, _ = ws
    _seed(st)
    out = _run("monitor", "changes", "-w", "t")
    assert "NEW_ASSET" in out
    assert "DISAPPEARED" in out
    assert "只有 after 快照" in out, f"没写明 NEW_ASSET 为什么没有 diff:\n{out}"
    assert "只有 before 快照" in out, f"没写明 DISAPPEARED 为什么没有 diff:\n{out}"


def test_unexpected_missing_diff_is_called_out_as_anomaly():
    """该有 diff 却没存 —— 要说成异常,不能混进「本来就没有」那一堆

    方向很重要:把 bug 显示成设计,等于让 bug 永远不被发现。
    """
    from arl_lite.cli import _change_lines
    # ADDRESS_CHANGED 本该有 diff。_SINGLE_SIDED 里没有它 → 必须说异常。
    assert "ADDRESS_CHANGED" not in __import__(
        "arl_lite.cli", fromlist=["x"])._SINGLE_SIDED
    lines = _change_lines({"change_type": "ADDRESS_CHANGED",
                           "diff": None, "asset_type": "host"})
    assert len(lines) == 1 and "异常" in lines[0], lines


def test_unparseable_diff_does_not_print_empty(ws):
    """diff 坏掉时要说坏掉,不能静默显示成空"""
    from arl_lite.cli import _change_lines
    lines = _change_lines({"change_type": "TITLE_CHANGED",
                           "diff": "{不是 json", "asset_type": "site"})
    assert lines and "解析" in lines[0], lines


# ── --json:给机器消费的那条路 ──

def test_json_output_is_parseable_and_carries_the_payload(ws):
    st, _ = ws
    _seed(st)
    out = _run("monitor", "changes", "-w", "t", "--json")
    data = json.loads(out)
    assert len(data) == 4, data
    addr = next(r for r in data if r["change_type"] == "ADDRESS_CHANGED")
    assert addr["diff"] == {"ip": {"before": "1.1.1.1", "after": "2.2.2.2"}}, addr
    assert addr["label"] == "web.example.com", addr
    # payload 原样带出,不经过给人看的截断。**两侧都要**:
    # 只给 after 的话,消费方就看不出「原来是什么」,那等于半个 diff。
    assert addr["before_value"] == {"ip": "1.1.1.1"}, addr
    assert addr["after_value"] == {"ip": "2.2.2.2"}, addr
    new = next(r for r in data if r["change_type"] == "NEW_ASSET")
    assert new["diff"] == {} and new["after_value"]["host"] == "a.example.com", new
    gone = next(r for r in data if r["change_type"] == "DISAPPEARED")
    assert gone["before_value"]["host"] == "b.example.com", gone
    assert gone["after_value"] == {}, gone


def test_json_and_text_agree_on_the_number_of_changes(ws):
    """两条路说的是同一件事 —— 数字对不上就是有一路在撒谎"""
    st, _ = ws
    _seed(st)
    data = json.loads(_run("monitor", "changes", "-w", "t", "--json"))
    text = _run("monitor", "changes", "-w", "t")
    assert f"{len(data)} change(s)" in text, f"文本和 JSON 报的数量对不上:\n{text}"


def test_json_of_empty_workspace_is_an_empty_list(ws):
    """空结果输出 `[]`,不是 `no changes` 那种人话 —— 机器解析要得了"""
    ws[0].add_host(1, "x.example.com", ip="1.1.1.1")
    out = _run("monitor", "changes", "-w", "t", "--json")
    assert json.loads(out) == [], out


# ── 展示层不许把整个列表搞崩 ──

def test_broken_row_does_not_kill_the_whole_listing(ws):
    """一条坏数据不该让整份报表出不来

    展示层因为一条坏数据就崩,那是拿报表换进程 —— 剩下的变更
    恰恰是最该被看见的。
    """
    st, _ = ws
    _seed(st)
    with st._conn() as c:
        c.execute(
            "INSERT INTO asset_changes (workspace_id, asset_hash, asset_type,"
            " change_type, diff) VALUES (?,?,?,?,?)",
            (st.workspace_id, "broken", "site", "TITLE_CHANGED", "{坏的"))
        c.execute(
            "INSERT INTO asset_changes (workspace_id, asset_hash, asset_type,"
            " change_type, diff) VALUES (?,?,?,?,?)",
            (st.workspace_id, "broken2", "site", "TECH_CHANGED", "[]"))
    out = _run("monitor", "changes", "-w", "t")
    assert "解析" in out and "空的" in out, out
    assert "1.1.1.1" in out, "坏行把好行也带走了"
