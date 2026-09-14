"""arl_lite.integrations

外部工具适配层(subprocess / HTTP API):
- subfinder: 子域枚举
- crtsh: 证书透明度查询(纯 HTTP,无需外部工具)
- nmap: 端口扫描(Phase 2)
- httpx: 站点探测(Phase 2)
"""


def valid_hostname(host: str, max_len: int = 253) -> bool:
    """上游数据入库前的统一防线。

    超长/空/含 null 的 hostname 会让 add_domain/add_host 抛 ValueError,
    而模块包装层的循环没有兜底——一条脏记录曾导致整模块 crash 且
    found 计数与已入库行不一致。
    """
    if not isinstance(host, str):
        return False
    host = host.strip()
    if not host or len(host) > max_len or "\x00" in host:
        return False
    return True
