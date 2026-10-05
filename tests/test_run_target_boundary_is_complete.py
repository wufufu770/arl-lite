"""`run -t` 的 target 边界必须完整 —— 绝不可能出现在域名/IP/URL 里的字符要提前拒。

背景:`cmd_run` 本来就有一段「防御:target 边界」,拦空、超长、含 null byte,
同段的注释说明了用意 —— 别让一个手误白跑一次完整扫描。但它漏了一种:
target 里含**绝不可能出现在主机名、IP、URL 里的字符**时(空格、`<`、`"`、反引号…),
命令照跑不误,一路到网络层才失败。这类输入一定是手误(URL 里要出现这些字符
也只会是 %20 / %22 这种百分号编码形态)。

边界刻意取**可证明的最小集**。这很重要,反例都是真实用例:
  192.0.2.1 / 2001:db8::1  合法 IP,含冒号
  localhost / intranet-host  内网单标签,单标签侦察的正当目标
  my_service               内网 DNS 常见的下划线
  *.example.com            通配写法
按「像不像域名」去写正则,这一个都拦得住吗?拦不住。所以只拦可证明的。

判据是双向的:能拒的必须拒(牙齿),合法的必须放行(防过度修正)。
「合法形态不误伤」这条用 `-m nosuchmodule` 走快路径 —— target 检查在
module 检查之前,能落到 module 报错就证明 target 检查放行了,不必真出网。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REJECT_MARK = "cannot appear in a domain"

# 逐个字符钉死:每个都必须单独被拒。把清单钉死,是为了防止有人为了让测试变绿
# 而悄悄把字符集缩小(Key Decision 11:列表内容本身要钉死)。
IMPOSSIBLE = [
    ("空格", "a b"),
    ("制表符", "a\tb"),
    ("换行", "a\nb"),
    ("回车", "a\rb"),
    ("尖括号 <", "a<b"),
    ("尖括号 >", "a>b"),
    ("双引号", 'a"b'),
    ("单引号", "a'b"),
    ("反引号", "a`b"),
]

# 合法形态清单同样钉死。这些都是真实用例,不是凑数。
LEGIT = [
    "example.com",
    "www.sub.example.co.uk",
    "xn--fsq.com",
    "192.0.2.1",
    "2001:db8::1",                    # IPv6,带冒号
    "https://example.com/path?q=1",  # URL
    "http://10.0.0.1:8080/admin",     # URL + 端口
    "localhost",                      # 内网单标签
    "intranet-host",                  # 内网单标签
    "my_service",                     # 内网 DNS 的下划线
    "*.example.com",                  # 通配
]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = tempfile.mkdtemp(prefix="targetguard")
    env["PYTHONPATH"] = REPO
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HTTP_PROXY"] = "http://127.0.0.1:9"
    env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    return subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", *argv],
        env=env, cwd=tempfile.gettempdir(),
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("label,target", IMPOSSIBLE, ids=[c[0] for c in IMPOSSIBLE])
def test_impossible_character_is_rejected_before_the_scan(label, target):
    """含不可能字符的 target 必须在扫描前就 rc=2,并点名是哪个字符。"""
    p = _run(["run", "-t", target])
    assert p.returncode == 2, (
        f"{label}: 期望 rc=2 提前拒绝,实际 rc={p.returncode}"
        f"(说明它一路跑进了扫描)"
    )
    assert REJECT_MARK in p.stderr, (
        f"{label}: 报错没说是「域名/IP/URL 里不可能出现的字符」:{p.stderr[-300:]}"
    )
    assert repr(target[1]) in p.stderr, (
        f"{label}: 报错没点名具体是哪个字符:{p.stderr[-300:]}"
    )


@pytest.mark.parametrize("target", LEGIT)
def test_legit_target_is_never_blocked(target):
    """合法形态一律不得被这条检查拦下(防过度修正)。

    没有这一条,把检查写成「非空即拒」也能让上面那组全绿。
    """
    p = _run(["run", "-t", target, "-m", "nosuchmodule"])
    assert REJECT_MARK not in p.stderr, (
        f"合法 target {target!r} 被 target 边界检查误伤了:{p.stderr[-300:]}"
    )
    # 能走到 module 检查报错,就证明 target 检查放行了
    assert "unknown module" in p.stderr, (
        f"合法 target {target!r} 应当放行到 module 检查,实际:{p.stderr[-300:]}"
    )


def test_rejection_shows_what_a_target_should_look_like():
    """报错必须给出目标该长什么样,只说「非法」等于没给出路。"""
    p = _run(["run", "-t", "a b"])
    tail = p.stderr
    for shape in ("example.com", "192.0.2.1"):
        assert shape in tail, (
            f"报错里没有给出合法 target 的形态示例 {shape!r}:{tail[-300:]}"
        )


@pytest.mark.parametrize("argv,mark", [
    (["run", "-t", ""], "must not be empty"),
    (["run", "-t", "a" * 1001], "too long"),
])
def test_preexisting_boundary_checks_still_hold(argv, mark):
    """扩展防御块不能把原有两条挤掉 —— 它们是同一个块里的邻居。"""
    p = _run(argv)
    assert p.returncode == 2, f"{argv} 期望 rc=2,实际 {p.returncode}"
    assert mark in p.stderr, f"{argv} 的原有检查失效了:{p.stderr[-300:]}"


def test_null_byte_check_still_holds_but_not_via_argv(capsys):
    """null byte 那条**经命令行不可达**,只能在进程内测,所以这里换一种测法。

    Linux 的 execve 参数是 NUL 结尾的 C 字符串:`run -t 'a\\x00b'` 在到达
    Python 之前就被截断成 `a`。首版判据拿 subprocess 去测它,实测拿到的是
    「target 变成 a、扫描照跑」,红得莫名其妙 —— 那是判据测不到,不是代码坏了。
    写测试不能假装端到端能覆盖一条表面走不到的分支。
    """
    from arl_lite.cli import cmd_run

    class _Args:
        target = "a\x00b"
        workspace = "default"
        modules = None
        preset = None

    rc = cmd_run(_Args())
    assert rc == 2, f"含 null byte 的 target 应当 rc=2,实际 {rc}"
    assert "null byte" in capsys.readouterr().err, "报错没点名是 null byte"
