"""TLS 证书采集与 DER 解析测试

这轮实现踩了三个真实的坑, 每个都固化成测试:

1. `ssl.getpeercert()`(字典) 在 CERT_NONE 下返回**空 dict** ——
   而我们必须用 CERT_NONE 才能采到自签/过期站的证书。所以走自研 DER 解析。
2. GeneralName 的 context-specific tag 映射凭记忆写会**整体错位一位**。
   RFC 5280 §4.2.1.6: dNSName 是 [2]=0x82, iPAddress 是 [7]=0x87。
   写错的表现是"SAN 全是 IP", 看起来像解析失败其实是类型错。
3. issuerUniqueID/subjectUniqueID 是**可选**字段, 无条件 idx+=1 会把
   extensions 一起跳过去 —— 表现是"其他字段都正常但 SAN 永远空",
   一个静默的解析器 bug。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from arl_lite.integrations.der_parse import (
    parse, DerError, _read_tlv, _iter_tlv, _decode_oid, _SAN_TAGS,
)
from arl_lite.integrations.tls_cert import CertInfo


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "pyproject.toml").is_file() and (cand / "arl_lite").is_dir():
            return cand
    raise RuntimeError("找不到项目根")


REPO = _find_repo_root()
# 测试证书: 自签, 由 openssl 生成并签入仓库, 不依赖网络
FIXTURE = REPO / "tests" / "fixtures" / "sample_cert.der"


class TestDerBasics(unittest.TestCase):

    def test_read_tlv(self):
        tlv = _read_tlv(b"\x02\x01\x2a", 0)      # INTEGER 42
        self.assertEqual(tlv.tag, 0x02)
        self.assertEqual(tlv.value, b"\x2a")
        self.assertEqual(tlv.end, 3)

    def test_long_form_length(self):
        payload = b"\x04" + b"\x82\x01\x00" + b"x" * 256
        tlv = _read_tlv(payload, 0)
        self.assertEqual(len(tlv.value), 256)

    def test_truncated_raises(self):
        with self.assertRaises(DerError):
            _read_tlv(b"\x30\x10\x01", 0)         # 声明 16 字节只有 1 字节

    def test_oid_decode(self):
        # 2.5.29.17 = subjectAltName
        self.assertEqual(_decode_oid(bytes([0x55, 0x1D, 0x11])), "2.5.29.17")
        # 2.5.4.3 = commonName
        self.assertEqual(_decode_oid(bytes([0x55, 0x04, 0x03])), "2.5.4.3")

    def test_general_name_tags(self):
        """GeneralName 映射不能错位——这是本轮最大的坑"""
        self.assertEqual(_SAN_TAGS[0x82], "DNS",     "dNSName 是 [2]=0x82")
        self.assertEqual(_SAN_TAGS[0x87], "IP",      "iPAddress 是 [7]=0x87")
        self.assertEqual(_SAN_TAGS[0x81], "email",   "rfc822Name 是 [1]=0x81")
        self.assertEqual(_SAN_TAGS[0x86], "URI")


class TestParseFixture(unittest.TestCase):
    """用签入的固定证书测试——不依赖网络, CI 才能跑"""

    @classmethod
    def setUpClass(cls):
        if not FIXTURE.exists():
            raise unittest.SkipTest(f"fixture 不存在: {FIXTURE}")
        cls.der = FIXTURE.read_bytes()
        cls.x = parse(cls.der)

    def test_subject_issuer(self):
        self.assertEqual(self.x.subject_cn, "*.test.example.com")
        self.assertEqual(self.x.issuer_cn, "arl-lite Test CA")

    def test_validity_parsed(self):
        """时间必须解析出来——空值会让 days_left 变 None, 过期检测失效"""
        self.assertRegex(self.x.not_before, r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(self.x.not_after, r"^\d{4}-\d{2}-\d{2}$")

    def test_san_not_empty(self):
        """SAN 空 = 索引推进跳过了 extensions(本轮踩过的坑)"""
        self.assertTrue(self.x.san_dns, "SAN 为空——解析器可能又跳过了 extensions")
        self.assertIn("*.test.example.com", self.x.san_dns)

    def test_san_types_decoded(self):
        """每个 SAN 都要带类型前缀, 不能全当 DNS"""
        types = {s.split(":", 1)[0] for s in self.x.san}
        self.assertIn("DNS", types)

    def test_sha256_stable(self):
        self.assertEqual(len(self.x.sha256), 64)
        self.assertEqual(self.x.sha256, parse(self.der).sha256, "指纹应稳定")

    def test_garbage_raises(self):
        with self.assertRaises(DerError):
            parse(b"\x99\x99\x99not-a-cert")


class TestWildcardMatching(unittest.TestCase):
    """通配符必须按 DNS 标签逐级匹配

    退化路径: 有人用 `host.endswith(suffix)` 实现通配符 ——
    那会让 `*.example.com` 匹配 `a.b.example.com`, 而 RFC 6125 规定
    `*` 不跨 `.`。资产归属判定用错会直接放行非目标资产。
    """

    def setUp(self):
        self.c = CertInfo(subject_cn="c", san=["*.example.com", "example.com"])

    def test_single_label_subdomain(self):
        self.assertTrue(self.c.covers("a.example.com"))
        self.assertTrue(self.c.covers("www.example.com"))

    def test_apex(self):
        self.assertTrue(self.c.covers("example.com"))

    def test_wildcard_does_not_cross_dot(self):
        self.assertFalse(self.c.covers("a.b.example.com"),
                         "*.example.com 不应覆盖 a.b.example.com")

    def test_different_domain(self):
        self.assertFalse(self.c.covers("example.org"))
        self.assertFalse(self.c.covers("notexample.com"),
                         "后缀相同但主域不同, 不能匹配")

    def test_empty_hostname(self):
        self.assertFalse(self.c.covers(""))

    def test_case_insensitive(self):
        self.assertTrue(self.c.covers("A.EXAMPLE.COM"))

    def test_naive_endswith_would_be_wrong(self):
        """证明朴素实现会错, 免得后人"优化"回去"""
        host, suffix = "a.b.example.com", ".example.com"
        self.assertTrue(host.endswith(suffix), "朴素 endswith 会误判为匹配")
        self.assertFalse(self.c.covers(host), "但正确实现必须拒绝")


class TestCertInfoFields(unittest.TestCase):

    def test_weak_flag(self):
        self.assertTrue(CertInfo(expired=True).is_weak)
        self.assertTrue(CertInfo(self_signed=True).is_weak)
        self.assertFalse(CertInfo().is_weak)

    def test_to_dict_roundtrip(self):
        c = CertInfo(sha256="a" * 64, san=["x.com"], days_left=30)
        d = c.to_dict()
        self.assertEqual(d["sha256"], "a" * 64)
        self.assertEqual(d["san"], ["x.com"])
        self.assertEqual(d["days_left"], 30)

    def test_empty_cert_is_not_crash(self):
        """空 SAN 时 covers 应返回 False 而不是抛异常"""
        self.assertFalse(CertInfo().covers("example.com"))


class TestFailureIsQuiet(unittest.TestCase):
    """采集失败必须符合三态纪律: 返回 None, 不抛异常"""

    def test_unreachable_host_returns_none(self):
        from arl_lite.integrations.tls_cert import fetch
        # 保留地址段, 保证不可达(RFC 6761)
        self.assertIsNone(fetch("this-host-does-not-exist.invalid", 443, timeout=3))

    def test_bad_port_returns_none(self):
        from arl_lite.integrations.tls_cert import fetch
        self.assertIsNone(fetch("127.0.0.1", 1, timeout=2))


if __name__ == "__main__":
    unittest.main()
