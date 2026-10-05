"""arl_lite.integrations.der_parse — 最小 X.509 DER 解析

为什么需要自己写:
    `ssl.getpeercert()`(字典形式)只在**证书验证通过**时才有内容。
    我们必须用 `CERT_NONE` 建连才能采到自签/过期站的证书
    (归属判定恰恰最需要这些), 此时字典是空的。
    但 `getpeercert(binary_form=True)` 仍给完整 DER。

    项目零依赖, 不能用 cryptography/asn1crypto。所以手写一个
    **只取归属判定需要字段** 的最小解析器——不是通用 X.509 库。

范围(够用即止):
    解析 TBSCertificate 里的: serial / issuer / subject / validity /
    SAN(extension) / issuer 唯一标识
    不做: 验签、密钥解析、完整 DN 规范化、证书链验证

X.509 结构(简化):
    Certificate ::= SEQUENCE {
        tbsCertificate       TBSCertificate,
        signatureAlgorithm   AlgorithmIdentifier,
        signatureValue       BIT STRING }

    TBSCertificate ::= SEQUENCE {
        version         [0] EXPLICIT Version DEFAULT v1,
        serialNumber        CertificateSerialNumber,
        signature           AlgorithmIdentifier,
        issuer              Name,
        validity            Validity,
        subject             Name,
        subjectPublicKeyInfo SubjectPublicKeyInfo,
        ... extensions [3] EXPLICIT Extensions OPTIONAL }
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

log = logging.getLogger("arl_lite.integrations.der_parse")

# DER 标签
_BOOLEAN, _INTEGER, _BIT_STRING, _OCTET_STRING = 0x01, 0x02, 0x03, 0x04
_NULL, _OID, _UTF8STRING, _SEQUENCE, _SET = 0x05, 0x06, 0x0C, 0x30, 0x31
_PRINTABLE, _T61STRING, _IA5STRING, _UTCTIME, _GENERALIZEDTIME = 0x13, 0x14, 0x16, 0x17, 0x18
_BMPSTRING, _UNIVERSALSTRING, _UTCSTRING = 0x1E, 0x1C, 0x19
_NUMERICSTRING, _VISIBLESTRING,  = 0x12, 0x1A


class DerError(ValueError):
    """DER 结构不合法"""


@dataclass
class TLV:
    """Tag-Length-Value"""
    tag: int
    value: bytes
    end: int          # value 结束位置(整个 TLV 结束位置)


def _read_tlv(buf: bytes, i: int) -> TLV:
    """从 buf[i] 读一个 TLV"""
    if i >= len(buf):
        raise DerError(f"TLV 越界 @ {i}")
    tag = buf[i]
    i += 1
    if i >= len(buf):
        raise DerError(f"缺长度字节 @ {i}")
    first = buf[i]
    i += 1
    if first & 0x80:
        n = first & 0x7F
        if n == 0 or n > 4:
            raise DerError(f"长度编码非法: first={first:#x}")
        if i + n > len(buf):
            raise DerError("长度字节越界")
        length = int.from_bytes(buf[i : i + n], "big")
        i += n
    else:
        length = first
    if i + length > len(buf):
        raise DerError(f"值越界: need={length} have={len(buf) - i}")
    return TLV(tag, buf[i : i + length], i + length)


def _iter_tlv(buf: bytes):
    """迭代一个 SEQUENCE/SET 里的所有 TLV"""
    i = 0
    while i < len(buf):
        tlv = _read_tlv(buf, i)
        yield tlv
        i = tlv.end


def _decode_string(tag: int, raw: bytes) -> str:
    """按标签选编码解码。未知标签退回 latin-1 尽力而为。"""
    if tag in (_UTF8STRING,):
        return raw.decode("utf-8", errors="replace")
    if tag in (_PRINTABLE, _IA5STRING, _VISIBLESTRING, _NUMERICSTRING):
        return raw.decode("ascii", errors="replace")
    if tag == _BMPSTRING:
        return raw.decode("utf-16-be", errors="replace")
    if tag == _UNIVERSALSTRING:
        return raw.decode("utf-32-be", errors="replace")
    if tag == _T61STRING:
        return raw.decode("latin-1", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _parse_name(buf: bytes) -> dict[str, str]:
    """Name ::= RDNSequence ::= SEQUENCE OF RelativeDistinguishedName

    每个 RDN 是 SET OF AttributeTypeAndValue ::= SEQUENCE { OID, value }
    OID 决定取哪个字段——只认常见的几个, 其余归入 'other'。
    """
    oid_map = {
        "2.5.4.3": "commonName",
        "2.5.4.10": "organizationName",
        "2.5.4.11": "organizationalUnitName",
        "2.5.4.6": "countryName",
        "2.5.4.7": "localityName",
        "2.5.4.8": "stateOrProvinceName",
        "2.5.4.5": "serialNumber",
        "1.2.840.113549.1.9.1": "emailAddress",
    }
    out: dict[str, str] = {}
    try:
        for rdn in _iter_tlv(buf):
            if rdn.tag != _SET:
                continue
            for atv in _iter_tlv(rdn.value):
                if atv.tag != _SEQUENCE:
                    continue
                parts = list(_iter_tlv(atv.value))
                if len(parts) < 2:
                    continue
                oid_tlv, val_tlv = parts[0], parts[1]
                if oid_tlv.tag != _OID:
                    continue
                oid = _decode_oid(oid_tlv.value)
                key = oid_map.get(oid, oid)
                text = _decode_string(val_tlv.tag, val_tlv.value)
                if key in out:
                    out[key] = f"{out[key]},{text}"
                else:
                    out[key] = text
    except DerError as e:
        log.debug(f"name parse failed: {e}")
    return out


def _decode_oid(raw: bytes) -> str:
    """OID: 第一个字节 = 40*a + b, 后续字节 base-128 变长"""
    if not raw:
        return ""
    vals = [raw[0] // 40, raw[0] % 40]
    cur = 0
    for b in raw[1:]:
        cur = (cur << 7) | (b & 0x7F)
        if not b & 0x80:
            vals.append(cur)
            cur = 0
    return ".".join(str(v) for v in vals)


def _parse_time(tag: int, raw: bytes) -> str:
    """UTCTime(YYMMDDHHMMSSZ) / GeneralizedTime(YYYYMMDDHHMMSSZ) -> YYYY-MM-DD"""
    try:
        s = raw.decode("ascii", errors="ignore")
        if tag == _UTCTIME:
            yy = int(s[0:2])
            year = 2000 + yy if yy < 50 else 1900 + yy
            return f"{year:04d}-{s[2:4]}-{s[4:6]}"
        if tag == _GENERALIZEDTIME:
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    except (ValueError, IndexError) as e:
        log.debug(f"time parse failed: {e}")
    return ""


# GeneralName ::= CHOICE (RFC 5280 §4.2.1.6), context-specific 隐式 tag = 0x80+CHOICE 序号
#   [0] otherName  [1] rfc822Name  [2] dNSName  [3] x400Address
#   [4] directoryName  [5] ediPartyName  [6] uniformResourceIdentifier
#   [7] iPAddress  [8] registeredID
#
# 这张表**不能凭记忆写**——写错一位就会把 dNSName 当成 iPAddress,
# 表现为"SAN 全是 IP", 看起来像解析失败其实类型错位。
# RFC 5280 附录 C.2 的实例佐证: rfc822Name 'end.entity@example.com' 是 [1]=0x81;
# 实际抓 pypi.org / github.com / baidu.com 三张证书, dNSName 都是 0x82。
_SAN_TAGS = {
    0x81: "email",   # rfc822Name
    0x82: "DNS",     # dNSName
    0x86: "URI",     # uniformResourceIdentifier
    0x87: "IP",      # iPAddress
}


def _parse_san(ext_value: bytes) -> list[str]:
    names: list[str] = []
    try:
        # GeneralNames ::= SEQUENCE OF GeneralName
        outer = _read_tlv(ext_value, 0)
        for gn in _iter_tlv(outer.value):
            kind = _SAN_TAGS.get(gn.tag)
            if kind:
                names.append(f"{kind}:{gn.value.decode('utf-8', errors='replace')}")
    except DerError as e:
        log.debug(f"SAN parse failed: {e}")
    return names


@dataclass
class X509:
    """归属判定需要的最小证书信息"""
    sha256: str = ""
    serial: str = ""
    issuer: dict[str, str] = field(default_factory=dict)
    subject: dict[str, str] = field(default_factory=dict)
    not_before: str = ""
    not_after: str = ""
    san: list[str] = field(default_factory=list)

    @property
    def issuer_cn(self) -> str:
        return self.issuer.get("commonName", "")

    @property
    def issuer_org(self) -> str:
        return self.issuer.get("organizationName", "")

    @property
    def subject_cn(self) -> str:
        return self.subject.get("commonName", "")

    @property
    def san_dns(self) -> list[str]:
        """只要 DNS 类型的 SAN, 去掉 'DNS:' 前缀"""
        return [n[4:] for n in self.san if n.startswith("DNS:")]


def parse(der: bytes) -> X509:
    """解析 DER 证书。失败抛 DerError —— 调用方按三态纪律处理。"""
    cert = _read_tlv(der, 0)
    if cert.tag != _SEQUENCE:
        raise DerError(f"顶层不是 SEQUENCE: {cert.tag:#x}")

    parts = list(_iter_tlv(cert.value))
    if not parts:
        raise DerError("Certificate 为空")
    tbs = parts[0]
    if tbs.tag != _SEQUENCE:
        raise DerError("tbsCertificate 不是 SEQUENCE")

    out = X509(sha256=hashlib.sha256(der).hexdigest())
    idx = 0
    fields = list(_iter_tlv(tbs.value))

    # [0] version(可选)
    if fields and fields[0].tag == 0xA0:
        idx = 1

    if idx < len(fields):                       # serialNumber
        out.serial = fields[idx].value.hex()
        idx += 1
    if idx < len(fields):                       # signature(AlgorithmIdentifier)
        idx += 1
    if idx < len(fields):                       # issuer
        out.issuer = _parse_name(fields[idx].value)
        idx += 1
    if idx < len(fields):                       # validity
        for vt in _iter_tlv(fields[idx].value):
            if vt.tag == _UTCTIME and not out.not_before:
                out.not_before = _parse_time(vt.tag, vt.value)
            elif vt.tag == _GENERALIZEDTIME and not out.not_before:
                out.not_before = _parse_time(vt.tag, vt.value)
            elif vt.tag in (_UTCTIME, _GENERALIZEDTIME) and not out.not_after:
                out.not_after = _parse_time(vt.tag, vt.value)
        idx += 1
    if idx < len(fields):                       # subject
        out.subject = _parse_name(fields[idx].value)
        idx += 1
    if idx < len(fields):                       # subjectPublicKeyInfo
        idx += 1

    # issuerUniqueID [1] / subjectUniqueID [2] 是**可选**字段——
    # 绝大多数证书没有。这里必须按 tag 判断再跳, 不能无条件 idx += 1:
    # 无条件跳会把紧随其后的 extensions [3] 一起跳过去, 表现为
    # "主体/签发者/有效期都解析正常, 但 SAN 永远是空" —— 一个
    # 看起来像"证书没有 SAN"实则是解析器 bug 的静默错误。
    if idx < len(fields) and fields[idx].tag in (0x81, 0x82):  # issuerUniqueID / subjectUniqueID
        idx += 1
    if idx < len(fields) and fields[idx].tag in (0x81, 0x82):
        idx += 1

    # extensions [3] EXPLICIT
    for extra in fields[idx:]:
        if extra.tag != 0xA3:
            continue
        try:
            ext_seq = _read_tlv(extra.value, 0)
            for ext in _iter_tlv(ext_seq.value):
                if ext.tag != _SEQUENCE:
                    continue
                ep = list(_iter_tlv(ext.value))
                if not ep or ep[0].tag != _OID:
                    continue
                if _decode_oid(ep[0].value) == "2.5.29.17" and len(ep) > 1:
                    # OCTET STRING 里裹着 DER
                    if ep[1].tag == _OCTET_STRING:
                        out.san = _parse_san(ep[1].value)
        except DerError as e:
            log.debug(f"extension parse failed: {e}")
    return out
