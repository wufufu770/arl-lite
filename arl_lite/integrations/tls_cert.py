"""arl_lite.integrations.tls_cert — TLS 证书采集(纯 stdlib)

为什么需要:资产归属判定是 SRC 场景最核心也最容易出错的问题。
"这个 IP 是不是目标公司的"——证书是最硬的证据之一, 但项目此前
只记 `tls_verified: bool`, 证书内容**完全丢弃**。

参照物: domain_hunter_pro 用证书匹配自动排除非目标资产
(`CertNotMatch` 标签), 那是人工打标时代的自动化。arl-lite 应该把
证书信息入库, 让关联规则能引用。

零依赖代价: 不能用 cryptography 库。但 stdlib 的 ssl 模块已经给了
需要的一切 —— `getpeercert(binary_form=True)` 给 DER,
`getpeercert()` 给已解好的 subject/issuer/SAN/有效期。
"""
from __future__ import annotations

import logging
import socket
import ssl
from dataclasses import dataclass, field, asdict

log = logging.getLogger("arl_lite.integrations.tls_cert")


@dataclass
class CertInfo:
    """一张证书的归属判定要素

    只保留"能用于判定归属"的字段, 不做通用证书解析——
    我们不是 CA, 不需要验签, 只需要知道"这张证书认领了哪些域名、
    谁签的、指纹是什么"。
    """
    # 指纹(SHA256, hex)—— 跨时间稳定, 可用于关联"同一张证书"
    sha256: str = ""
    # 序列号 —— 轮期时会变, 配合 not_after 判断是否续期过
    serial: str = ""
    # 签名者主体, 如 GlobalSign nv-sa
    issuer_cn: str = ""
    issuer_org: str = ""
    # 证书主体 CN
    subject_cn: str = ""
    # 主体声明的域名(CN + SAN.DNS)
    san: list[str] = field(default_factory=list)
    not_before: str = ""
    not_after: str = ""
    expired: bool = False
    self_signed: bool = False      # 自签证书 —— 归属判定时降权
    days_left: int | None = None   # 距离过期天数, None = 未知

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_weak(self) -> bool:
        """自签或已过期 —— 归属判定时不能作为高置信证据"""
        return self.self_signed or self.expired

    def covers(self, hostname: str) -> bool:
        """这张证书是否覆盖 hostname(含通配符)。

        通配符必须按 DNS 标签逐级匹配, 不能简单字符串包含:
            *.example.com  覆盖  a.example.com / b.a.example.com
            *.example.com  不覆盖 example.com(缺一级)
            *.example.com  不覆盖 example.org(主域不同)
        """
        if not hostname:
            return False
        host = hostname.lower().rstrip(".")
        for name in self.san or [self.subject_cn]:
            if not name:
                continue
            n = name.lower().rstrip(".")
            if n == host:
                return True
            if n.startswith("*."):
                # 通配符只吃掉最左边一级
                suffix = n[1:]              # ".example.com"
                if not host.endswith(suffix):
                    continue
                left = host[: -len(suffix)]  # "a" 或 "a.b"
                if left and "." not in left:
                    return True
        return False

def fetch(host: str, port: int = 443, timeout: float = 8.0,
          server_hostname: str | None = None) -> CertInfo | None:
    """取一张证书。失败返回 None —— **不抛异常**。

    采集层不该因为某个站点的 TLS 有问题就崩掉整轮扫描。
    调用方按三态纪律处理(None = 这个源没出力)。
    """
    sni = server_hostname or host
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # 自签/过期也要读, 不能因为验不过就放弃
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=sni) as tls:
                # 注意: CERT_NONE 下 getpeercert() 返回**空 dict**。
                # 只有证书验证通过时它才解出 subject/issuer/SAN。
                # 但 binary_form 始终给完整 DER —— 所以走自研解析。
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError, ValueError) as e:
        log.debug(f"cert fetch failed {host}:{port}: {type(e).__name__}: {e}")
        return None

    try:
        from .der_parse import parse as _parse_der
        x = _parse_der(der)
    except Exception as e:
        # DER 解析失败不该拖垮整轮扫描, 但必须留痕——静默吞掉会让人
        # 以为"这个站没有 SAN", 实际是我们没解析出来
        log.warning(f"DER parse failed for {host}:{port}: {type(e).__name__}: {e}")
        return None

    san = x.san_dns[:64]           # SAN 太多会撑爆 DB, 截断
    subject_cn = x.subject_cn
    if subject_cn and subject_cn not in san:
        san.insert(0, subject_cn)

    import datetime
    not_before, not_after = x.not_before, x.not_after
    days_left = None
    expired = False
    try:
        exp = datetime.datetime.strptime(not_after, "%Y-%m-%d")
        delta = exp - datetime.datetime.utcnow()
        days_left = delta.days
        expired = delta.total_seconds() < 0
    except (ValueError, TypeError):
        pass

    # 自签判定: 签发者与主体同时相等(比 CN + Org, 防同 CN 不同 Org)
    self_signed = bool(subject_cn) and (
        x.issuer_cn == subject_cn
        and x.issuer_org == x.subject.get("organizationName", "")
    )

    return CertInfo(
        sha256=x.sha256,
        serial=x.serial,
        issuer_cn=x.issuer_cn,
        issuer_org=x.issuer_org,
        subject_cn=subject_cn,
        san=san,
        not_before=not_before,
        not_after=not_after,
        expired=expired,
        self_signed=self_signed,
        days_left=days_left,
    )

