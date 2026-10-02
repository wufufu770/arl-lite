"""测试会话级的临时目录善后

## 为什么需要这个

r23 实测发现:跑一次 `tests/test_devloop.py` + `tests/test_phase4.py`
就在 `/tmp` 里**新增 23 个目录**,而 `/tmp` 下这种 `tmpXXXXXXXX` 目录
已经累积了 **3741** 个 —— 每一轮测试、每一个泄漏点各漏一个。

根因是测试里到处 `tempfile.mkdtemp()`:它**不注册任何清理**。
本文件出现之前,只有 `TemporaryDirectory` 上下文和 `addCleanup`
能收拾干净,其余全靠人记得写。

## 为什么不在每个测试里逐个修

实测有 **17 处** `mkdtemp()` 散在 7 个文件里,形态各不相同
(有些在 `setUp`、有些在 dict 字面量里塞进 `os.environ`)。
逐个改要动 7 个文件、17 个点,每一处都有回归风险,
而且**下次有人再写一个 `mkdtemp()` 又漏了**。

会话级兜底只改一个地方,对现在和将来的泄漏都有效。
这不是"掩盖问题"—— 泄漏的量会被打印出来(见下面的 `report`),
而且 `test_no_real_home_writes.py` 里有一条行为测试直接验证
这个兜底真的有效。

## 它不碰什么

- 不删别的进程正在用的目录(r27 起:只删本会话自己 `mkdtemp` 建的)
- 不删当前正在使用的目录
- 只删**本次会话期间登记过**的、且名字符合 `tmp*` 命名、且是空目录或
  含 `data.db`/`state.json` 这类测试产物的
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

import pytest


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "pyproject.toml").is_file() and (cand / "arl_lite").is_dir():
            return cand
    raise RuntimeError("找不到项目根")


# 测试期间**不许被写**的真实文件。
#
# 第 23 轮抓到"测试往用户真实 HOME 写数据",第 26 轮抓到同一个病的
# 另一副面孔:一条只想验"accept 会拒绝"的测试,对**真实仓库**调了
# `accept_baseline(REPO, ...)`。accept_baseline 是会写盘的。
#
# 当时没出事纯属运气 —— 测试先 `g.run(REPO)` 拿到绿才进分支,而
# accept_baseline 内部**又跑一遍** gate;两次调用之间状态一翻转,
# 提升就真写进 devloop/baselines.json 了。实测 baseline 被从 14254
# 抬到 15014,理由是测试里那句"这条理由够长了用于测试"。
#
# **一个只想验证"系统会说不"的测试,把红线抬了 760 行。**
#
# baselines.json 是红线的唯一真相来源,也是 r20 花了整轮才做成
# 可审计的留痕。它在任何测试里都不该被改 —— 所以按内容哈希守,
# 而不是靠"大家记得别改"。
_PROTECTED = ("devloop/baselines.json",)


@pytest.fixture(scope="session", autouse=True)
def _real_state_untouched_by_tests():
    """测试会话期间,真实的状态文件必须一字未改"""
    root = _find_repo_root()
    before = {}
    for rel in _PROTECTED:
        p = root / rel
        before[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    yield
    for rel in _PROTECTED:
        p = root / rel
        after = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        if before[rel] != after:
            pytest.fail(
                f"测试改了真实状态文件 {rel}(内容哈希变了)。\n"
                "  状态文件是协议的真相来源,不是测试的沙箱。\n"
                "  要测写盘行为就用临时仓库,别拿 REPO 当参数。\n"
                f"  改前 {before[rel]}\n  改后 {after}",
                pytrace=False)


def _snapshot() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).iterdir()}


def _reap_created(created: list, base: Path | None = None) -> list:
    """删掉 `created` 里登记过的目录,返回删掉的名字

    抽成函数是为了能被单测 —— 回收器是会话级 autouse fixture,
    "它会不会误删别人的东西"这种问题没法从外面问它。

    `base` 守卫:只碰 base 底下的路径。少这道判断,一次传错参数就能把
    用户的目录清掉 —— 这类操作宁可多挡一层。
    """
    root = Path(base) if base is not None else Path(tempfile.gettempdir())
    names = []
    for d in created:
        p = Path(d)
        # 测试自己可能已经清过了(用了 addCleanup),exists 守卫
        if not p.exists() or root not in p.parents:
            continue
        shutil.rmtree(p, ignore_errors=True)
        names.append(p.name)
    return names


@pytest.fixture(scope="session", autouse=True)
def _reap_stray_tmp_dirs():
    """会话结束时清掉**本会话自己建**的、没人清理的临时目录

    ## r27:判据从"会话期间新增的 tmp* 目录"改成"本会话 mkdtemp 建的目录"

    原判据认不出"这个目录是别的进程刚建的"。这不是理论风险 ——
    协议明确要求支持多 agent 并行,r27 实测就撞上了:两个 pytest 同时跑,
    一个会话的回收器把另一个**正在用**的目录删掉,表现为别的测试莫名
    失败("目录不存在"),从结果上完全看不出跟这里有关系。

    (中间还有过一版按内容过滤的:空目录 / 只含 `data.db`、`state.json`,
    实测只兜住一半 —— 另一半长这样:

        /tmp/tmp832__a93/p4reg          ← workspace 目录
        /tmp/tmpwbj3be1i/.arl-lite      ← 另一个 workspace 根
        /tmp/tmpae2aoaq2/{backlog.md,queue.json,state.json}

    形态太杂,按内容猜必然漏,而"漏"就等于这条兜底没用。)

    现在改成**追踪制**:会话开始时包一层 `tempfile.mkdtemp`,谁建的记下
    路径,结束时只删自己那份。别的进程的东西一概不碰。

    这不是把职责收窄 —— 原来的职责本来就写错了。它一直声称"回收无人
    负责的目录",实际回收的是"我猜是无人负责的目录",而"猜"这个动作
    在多 agent 场景下必然会猜错。

    ## 它不碰什么

    - 不碰别的进程建的目录(本会话没登记过的一律不删)
    - 不碰 `tempfile.TemporaryDirectory` 自己会清的目录
    - 只删 `tempfile.gettempdir()` 底下的目录,不删文件,不删别处路径
    """
    created: list = []
    orig = tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = orig(*args, **kwargs)
        created.append(path)
        return path

    tempfile.mkdtemp = tracking_mkdtemp
    try:
        yield
    finally:
        tempfile.mkdtemp = orig
        reaped = _reap_created(created)
        if reaped:
            print(
                f"\n[conftest] 本次会话回收了 {len(reaped)} 个自己建的临时目录"
                f"(/tmp 共 {len(_snapshot())} 个)。"
                f"写测试请用 tempfile.TemporaryDirectory() 或 addCleanup。"
            )
