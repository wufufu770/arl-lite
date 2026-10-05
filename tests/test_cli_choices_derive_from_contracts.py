"""r45:CLI 的 `choices` 是一份第三份硬编码词表,已经过期

## 实测过的后果

    $ arl-lite monitor changes --change-type ADDRESS_CHANGED
    arl-lite monitor changes: error: argument -c/--change-type: invalid choice:
    'ADDRESS_CHANGED' (choose from 'NEW_ASSET', 'DISAPPEARED', 'TITLE_CHANGED',
                       'TECH_CHANGED', 'FINGERPRINT_CHANGED', 'STATUS_CHANGED')
    exit 2

`cli.py` 的 `choices` 是一份手写的 6 项列表,而 `CHANGE_TYPES` 已经有 7 种。
后果不是「少一个选项」这么轻:用户按文档用新类型过滤会被 CLI 直接顶回来,
而错误信息只列那 6 个旧值,**看起来像是这个类型根本不存在**。

## 这是同一张契约表的第三份来源

r42 立了 `CHANGE_TYPES`,并在测试里断言「schema.sql 的注释 == CHANGE_TYPES」,
把注释从死文档变成了契约。那时候这张表有两个来源:`monitor.py`(真的)和
`schema.sql` 注释(被测试钉住)。`cli.py` 的 choices 是第三份 —— 没人管,
于是 r44 加了 `ADDRESS_CHANGED` 之后它就过期了。

同一张表有两个来源,迟早会漂;三个只是漂得更晚一点。

## 断言要**双向**

只查一个方向会漏掉最典型的那种错:

- 只查「CLI 接受的都在契约表内」→ 抓不到「加了类型但忘了加 CLI 选项」;
- 只查「契约表里的 CLI 都接受」→ 抓不到「CLI 多写了一个不存在的类型」。

两条都得有。`--type` 同理,真值是 `Monitor._ASSET_TABLES`。
"""
from __future__ import annotations

import argparse

import pytest

from arl_lite.cli import build_parser
from arl_lite.core.monitor import CHANGE_TYPES, Monitor


@pytest.fixture(scope="module")
def parser():
    return build_parser()


def _choices(parser, *argv) -> list[str]:
    """从 parser 里把某个参数的 choices 挖出来"""
    for action in parser._subparsers._group_actions[0].choices["monitor"]._subparsers._group_actions[0].choices["changes"]._actions:
        if argv and action.dest in argv:
            return list(action.choices or [])
    raise AssertionError(f"没找到 argv={argv} 对应的参数")


# ── 方向一:契约表里的每个取值,CLI 都接受 ──

@pytest.mark.parametrize("ct", list(CHANGE_TYPES))
def test_every_change_type_is_accepted_by_cli(parser, ct):
    """契约表里加一种类型,CLI 不该还在拒绝它

    这正是 r44 之后没被抓住的那一类:类型加了,choices 没跟上。
    """
    args = parser.parse_args(["monitor", "changes", "--change-type", ct])
    assert args.change_type == ct


@pytest.mark.parametrize("t", sorted(Monitor._ASSET_TABLES))
def test_every_asset_type_is_accepted_by_cli(parser, t):
    args = parser.parse_args(["monitor", "changes", "--type", t])
    assert args.type == t


# ── 方向二:CLI 接受的每个取值,都在契约表里 ──

def test_cli_offers_nothing_outside_change_types(parser):
    """CLI 不会多给一个不存在的类型

    只查方向一的话,这里写错一个 `--change-type NOPE` 也照样绿 ——
    而用户会拿它去查库,查出来永远是空。
    """
    assert set(_choices(parser, "change_type")) == set(CHANGE_TYPES)


def test_cli_offers_nothing_outside_asset_types(parser):
    assert set(_choices(parser, "type")) == set(Monitor._ASSET_TABLES)


# ── choices 的校验力不能因为派生而丢掉 ──

@pytest.mark.parametrize("bad", ["不存在的类型", "new_asset", "NEW_ASSET ", "ADDRESS_CHANGE"])
def test_nonsense_change_type_is_still_rejected(parser, bad):
    """从契约表派生不等于放行一切

    `choices` 还在,所以拼错的、大小写错的、前后带空格的都该被拒。
    如果哪天改成不校验了,拼错会变成「查出来是空的」这种沉默的错。
    """
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["monitor", "changes", "--change-type", bad])
    assert ei.value.code == 2


# ── 根因回归:别把手写列表加回来 ──

def test_cli_source_has_no_hardcoded_change_type_list():
    """`cli.py` 里不该再出现手写的变更类型字面量列表

    查的是**结构**而不是文本:`choices=[...]` 里如果全是裸字符串常量,
    那就是手写的;如果引用了 `CHANGE_TYPES` 之类的名字,就是派生的。
    这条防的是「先派生、后有人手改回去」—— 那是 r45 这个 bug 的原始形状。
    """
    import ast
    import inspect
    from pathlib import Path

    import arl_lite.cli as cli_mod

    src = inspect.getsource(cli_mod)
    tree = ast.parse(src)
    literals: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "choices":
            continue
        if isinstance(node.value, ast.List) and node.value.elts:
            if all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                   for e in node.value.elts):
                literals.append([e.value for e in node.value.elts])
    # 只盯**变更类型**和**资产类型**这两份。别的 choices 确实没有对应的
    # 契约表,手写是对的 —— 比如 `query` 的表名列表是复数(domains/hosts/…)
    # 且含 tasks / correlations / asset_changes 这些非资产表,那是另一份
    # 词表,不在本条范围里。探针用**单数**的资产类型名,正是为了不和它混淆。
    asset_probe = {"domain", "host", "port", "site", "finding"}
    for vals in literals:
        assert not ({"NEW_ASSET", "DISAPPEARED"} & set(vals)), (
            f"cli.py 里又出现了手写的变更类型列表:{vals}"
        )
        assert not (asset_probe <= set(vals)), (
            f"cli.py 里又出现了手写的资产类型列表:{vals}"
        )
    assert Path(cli_mod.__file__).is_file()


# ── 报错信息里要列全 ──

def test_rejection_message_lists_every_contract_type(parser, capsys):
    """被拒时列出来的可选值必须是**全的**

    r45 之前那个错误信息只列 6 个,用户据此会以为新类型不存在。
    这一条盯的是「提示本身也要跟契约一致」—— choices 对了但 help 文本
    还写着一份旧的,那等于只修了一半。
    """
    with pytest.raises(SystemExit):
        parser.parse_args(["monitor", "changes", "--change-type", "NOPE"])
    err = capsys.readouterr().err
    for ct in CHANGE_TYPES:
        assert ct in err, f"报错信息里少了 {ct}(用户会以为它不存在)"


def test_help_text_lists_every_contract_type(parser, capsys):
    """`--help` 里列出来的可选值也必须是全的

    choices 对了而 help 写死一份旧列表,等于只修了一半:用户翻帮助时
    看到的还是那份过期词表。r45 之前 argparse 的**报错信息**就是这么
    骗人的(只列 6 个),help 是同一条路上的下一站。
    """
    with pytest.raises(SystemExit):
        parser.parse_args(["monitor", "changes", "--help"])
    out = capsys.readouterr().out
    for ct in CHANGE_TYPES:
        assert ct in out, f"--help 里少了 {ct}(用户会以为它不存在)"
    for t in Monitor._ASSET_TABLES:
        assert t in out, f"--help 里少了资产类型 {t}"
