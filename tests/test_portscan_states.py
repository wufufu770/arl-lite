"""r104:端口扫描对「没探到」的端口完全静默 —— 和「探到关闭」长得一模一样

## 实测的退化路径(不是推测)

`tests/test_phase2.py::test_portscan_integration` 断言的是
**公网** `example.com:80 或 443 必须开着`。r103 收尾时全量 `test_baseline`
就红在这条,traceback 是 `got {8080}` 而 `etype is None`(扫描没报错)。
四组对照证明是外网波动不是代码回归:单跑 5 次全过、`tests/test_[a-p]*.py`
1093 条全过、全量 1387 条失败、**决定性的一组**是在
`pytest_collection_finish` 钩子里跑同一个 `scan()`(此时零个测试执行),
结果是 `[80, 8080]`,而同一份代码几分钟后实测是 `[80, 443, 8080]`。

排查过并**逐个实测排除**的三个方向,如实记着免得下一个人重走:
`ulimit -n` 是 1048576 不是 1024(fd 耗尽不成立);`tests/` 里没有任何测试
写 `os.environ` 里的 proxy(`test_run_target_boundary_is_complete.py:67`
那处是 `dict(os.environ)` 副本,只传给 subprocess);`conftest.py` 干净。

## 真问题不在那条测试,在它测的那个函数

`integrations/portscan.py::_probe_tcp` 把 `socket.timeout` /
`socket.gaierror` / `ConnectionRefusedError` / `OSError` **全部 catch 掉
返回 None**。于是三种完全不同的事实落进同一个结果:

| 实际情况 | 该说的是 | 实际被记成 |
|---|---|---|
| 连接成功 | 开着 | 开着 ✓ |
| `ECONNREFUSED`(真的关了) | 关闭 | 没结果 |
| 超时 / 被防火墙丢包 / DNS 失败 | **未判定**(服务可能在) | 没结果 |

实测把 `socket.getaddrinfo` 换成抛 `gaierror`,`scan()` 照样返回
`err=None, etype=None` —— 调用方和用户都以为扫完了,拿到的是残缺结果。
对安全工具来说这不是显示问题:**被防火墙挡在后面的服务会被报成不存在**。

r104 让它说出来:`etype="partial"` + 明说几个未判定、不等于端口关闭。

## 为什么测试必须换掉公网 fixture

一个会随机红的门禁等于没有门禁。基线是 0 失败,这种测试红一次就有人
得去提基线,而 `--update-baseline` 那个后门正是在这种时候最容易被用上。

**把尺子收起来不等于修东西** —— 所以 r104 同时做了两件:换 fixture(让门禁
恢复可信)+ 改实现(让 portscan 开口)。只有前者的话,门禁会绿而问题还在。

## 本文件只用本机地址

`127.0.0.1`(起/关 listener 造 open 和 closed)+ `10.255.255.1`
(RFC1918 不可路由,造 filtered/超时)。**不连任何公网主机** —— 一条
判据自己依赖外网,它的结论就只在网络好的时候成立。

## 本轮**没**做的(别以为已经全了)

- **nmap 分支没做**。`scan()` 上面 nmap 成功是直接 `return` 的,同一个洞
  还在。而 nmap 自己分得清 open/closed/filtered,是我们把非 open 的行滤掉
  了 —— 修法不一样,留给下一轮。
- **`_probe_tcp` 仍不返回三态**。r104 只在 `scan()` 层面对比
  `len(results)` 和 `len(port_list)`,说得出「有几个没判定」,说不出
  「是关闭还是被过滤」。
- **`test_phase2.py` 里还有三条真实网络测试**(`test_httpx_probe_integration`
  / `test_9_sources_integration` / `test_e2e_chain`),同样把公网当 fixture。
  本轮没动它们,它们仍是门禁上的定时炸弹。
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from arl_lite.integrations.portscan import scan


def _listener() -> tuple[socket.socket, int]:
    """起一个本机 listener,返回 (sock, 端口)。开着 = connect 会成功。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    return srv, srv.getsockname()[1]


def _closed_port() -> int:
    """bind 之后立刻 close —— 这个端口确定会被 RST 掉。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


async def _scan(host: str, ports: str, timeout: float = 1.0):
    return await scan(host, ports=ports, prefer="python", timeout_per_port=timeout)


# =====================================================================
# 核心:一个都没探到时,不许报成功
# =====================================================================


async def test_all_ports_closed_is_not_reported_as_a_clean_scan():
    """改前这里 etype 恒为 None —— 哪怕 2 个端口一个都没探到,照样「成功」。

    这是本轮最核心的一条:旧断言 `assert etype is None` 在退化实现下
    **恒真**,对着一个「什么都没扫到」的返回也照样绿。
    """
    closed = _closed_port()
    results, err, etype = await _scan("127.0.0.1", str(closed))

    assert results == [], f"关闭的端口不该出现在结果里:{results}"
    assert etype is not None, (
        f"探到 0/{1} 个端口,etype 却还是 None —— "
        f"调用方会以为这次扫描是完整的(err={err!r})"
    )
    assert err and "未判定" in err, (
        f"没探到的端口必须用「未判定」这个词说出来,不能只丢一个类型名:{err!r}"
    )


async def test_the_message_says_how_many_and_does_not_claim_they_are_closed():
    closed_a, closed_b = _closed_port(), _closed_port()
    _, err, _ = await _scan("127.0.0.1", f"{closed_a},{closed_b}")

    assert err and "2/2" in err, f"文案必须带上未判定的个数和分母:{err!r}"
    assert "不等于端口关闭" in err, (
        f"文案不能让人以为这 2 个端口确认关闭了:{err!r}"
    )


# =====================================================================
# 方向二:全开时别刷屏
# =====================================================================


async def test_all_ports_open_reports_no_problem():
    """反方向:不能改成无条件报 partial。

    只有一条方向的话,把 `if` 删掉让每次都报错也能全绿 —— 而那会让
    正常扫描每次都挂一个假警告,和原来的静默一样有害。
    """
    srv, open_port = _listener()
    try:
        results, err, etype = await _scan("127.0.0.1", str(open_port))
        assert [r["port"] for r in results] == [open_port], results
        assert etype is None, f"全开却报了问题:{etype} / {err!r}"
        assert err is None, f"全开却带了错误文案:{err!r}"
    finally:
        srv.close()


async def test_partial_scan_reports_exactly_the_missing_count():
    srv, open_port = _listener()
    try:
        closed = _closed_port()
        results, err, etype = await _scan("127.0.0.1", f"{open_port},{closed}")
        assert [r["port"] for r in results] == [open_port], results
        assert etype == "partial", f"一开一关该报 partial,实际 {etype!r}"
        assert err and "1/2" in err, f"该说 1/2 未判定:{err!r}"
    finally:
        srv.close()


# =====================================================================
# 核心信息损失点:被过滤的服务不许被说成「不存在」
# =====================================================================


async def test_an_unroutable_address_is_counted_as_unknown_not_as_closed():
    """10.255.255.1 是 RFC1918 不可路由地址,连过去会超时/不可达。

    改前它和「端口关闭」在结果里完全一样(都没有),用户拿到的结论是
    「这个服务不存在」—— 而真相是「探不到」。r104 之后它必须被算进
    「未判定」,因为**这正是最容易被漏报的那一类资产**。
    """
    results, err, etype = await _scan("10.255.255.1", "80,443", timeout=0.3)

    assert results == [], f"不可路由地址不该有 open 端口:{results}"
    assert etype == "partial", f"探不到必须算未判定,实际 etype={etype!r}"
    assert err and "2/2" in err, f"两个都没探到就该说 2/2:{err!r}"


async def test_a_filtered_port_is_not_reported_as_open():
    """反过来的护栏:探不到 ≠ 报成开着。

    这条和上面那条是一对。只写上面那条的话,把 `_probe_tcp` 改成
    「异常时返回一个 state=open 的假记录」也能过 —— 而那比静默更坏。
    """
    results, _, _ = await _scan("10.255.255.1", "22", timeout=0.3)
    assert not any(r["port"] == 22 and r["state"] == "open" for r in results), (
        f"探不到的端口被报成 open 了:{results}"
    )


# =====================================================================
# 端到端:这条文案得真能到用户眼前
# =====================================================================


async def test_the_partial_note_reaches_module_errors():
    """`PortscanModule.run` 已经有 `if err: all_errors.append(...)`,
    所以 partial 文案会自动进 `ModuleResult.errors`。这条钉住那个接线 ——
    改前 `scan()` 根本不返回 err,那行代码永远走不到。
    """
    from arl_lite.modules.recon.ports.portscan import PortscanModule

    class _St:
        workspace_id = 1
        task_id = 1

        def add_port(self, **kw):
            self.last = kw

    m = PortscanModule.__new__(PortscanModule)
    m.storage = _St()
    m.task_id = 1
    m.name = "portscan"
    m.config = {"ports": str(_closed_port())}

    closed = _closed_port()
    m.config = {"ports": str(closed)}
    res = await m.run(target="127.0.0.1", prefer="python", timeout_per_port=0.5)

    assert res.found == 0, f"关闭的端口不该入库:{res.found}"
    assert res.errors, "一个端口都没探到却 errors 为空 —— 用户看不到任何提示"
    assert "未判定" in res.errors[0], f"errors 里没有未判定的说明:{res.errors[0]!r}"


# =====================================================================
# 反向控制:解析端口这个纯函数不能被顺手改坏
# =====================================================================


def test_parse_ports_unchanged():
    from arl_lite.integrations.portscan import parse_ports

    assert parse_ports("80,443") == [80, 443]
    assert parse_ports("1-3") == [1, 2, 3]
    assert parse_ports("80,443,8000-8002") == [80, 443, 8000, 8001, 8002]
