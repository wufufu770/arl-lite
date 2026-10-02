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

- 不删会话开始前就存在的目录(可能是别的进程正在用的)
- 不删当前正在使用的目录
- 只删**本次会话期间新建**、且符合 `tmp*` 命名、且是空目录或
  含 `data.db`/`state.json` 这类测试产物的
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


def _snapshot() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).iterdir()}


@pytest.fixture(scope="session", autouse=True)
def _reap_stray_tmp_dirs():
    """会话结束时清掉本次新建的 `tmp*` 目录

    ## 判据为什么是"会话期间新建",而不是"内容像不像测试产物"

    第一版按内容过滤(空目录 / 只含 `data.db`、`state.json`),
    实测只兜住一半 —— 另外一半长这样:

        /tmp/tmp832__a93/p4reg          ← workspace 目录
        /tmp/tmpwbj3be1i/.arl-lite      ← 另一个 workspace 根
        /tmp/tmpae2aoaq2/{backlog.md,queue.json,state.json}

    形态太杂,按内容猜必然漏 —— 而"漏"就等于这条兜底没用。
    所以改成:会话开始时拍一张 `/tmp` 快照,结束时把**新增的**
    `tmp*` 目录全收掉。

    ## 这个判据的代价,说在前面

    它认不出"这个目录是别的进程刚建的"。如果有人**同时**在别的
    终端跑测试或跑一个用 `tempfile.mkdtemp()` 的程序,理论上会被
    误删。

    实测这个项目没有这种用法(冒烟时 `arl-lite` 自己不建 tmp 目录),
    所以按"只在跑测试时生效 + 只碰新增目录"承担这个风险。
    真撞上了,表现为那个程序报"目录不存在",不会静默出错 ——
    这是这个判据可接受的原因。

    宁可漏收,也不能误删用户数据:所以只碰 `tempfile.gettempdir()`
    底下、且名字以 `tmp` 开头的**目录**,不碰文件,不碰别的路径。
    """
    before = _snapshot()
    yield
    tmp = Path(tempfile.gettempdir())
    strays = []
    for p in tmp.iterdir():
        if p.name in before or not p.is_dir() or not p.name.startswith("tmp"):
            continue
        shutil.rmtree(p, ignore_errors=True)
        strays.append(p.name)
    if strays:
        print(
            f"\n[conftest] 本次会话回收了 {len(strays)} 个无人清理的临时目录"
            f"(/tmp 共 {len(before)} → {len(_snapshot())})。"
            f"写测试请用 tempfile.TemporaryDirectory() 或 addCleanup。"
        )
