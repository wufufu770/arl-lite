"""arl_lite.integrations

外部工具适配层(subprocess / HTTP API):
- subfinder: 子域枚举
- crtsh: 证书透明度查询(纯 HTTP,无需外部工具)
- nmap: 端口扫描(Phase 2)
- httpx: 站点探测(Phase 2)
"""


import re

# 单个 DNS label:字母/数字/连字符/下划线(_dmarc 等 SRV/DKIM 标签合法)
_LABEL_RE = re.compile(r"^[a-z0-9_\-]{1,63}$", re.IGNORECASE)


def valid_hostname(host: str, max_len: int = 253) -> bool:
    """上游数据入库前的统一防线。

    超长/空/含 null 会让 add_domain/add_host 抛 ValueError;这里额外
    拒绝非法字符和 URL 混入(http://、空格等脏值不该进 domains 表)。
    注:不校验总长以外的 RFC 细节(纯数字/IP 混入由各源语义决定)。
    """
    if not isinstance(host, str):
        return False
    host = host.strip()
    if not host or len(host) > max_len or "\x00" in host:
        return False
    if "://" in host or " " in host:
        return False
    stripped = host.rstrip(".")
    if not stripped:
        return False
    return all(_LABEL_RE.match(label) for label in stripped.split("."))
