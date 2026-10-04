"""`test_baseline` 超时时必须说清「上次跑成多久」—— 否则没法判断是挂了还是变慢

## 背景:600s 是挂死检测器,不是性能预算

`arl_lite/devloop/gates.py` 里那行 `timeout=600` 的注释写得很明确:
「兜底,防止 pytest 卡死」。它要回答的是「**卡住了吗**」。

但它原来的报错只有一句 `pytest timed out after 600s`。真挂死和
「这台机器今天慢」在读起来**完全一样**。

r88 实测吃过这个亏:全量实测 476.37s / 531.52s / 564.61s 三次都对得上
600s,首跑超时、单独重跑就过了。诊断它花了整整一轮,第一反应还是
「是不是我这一轮改慢了」—— 那个 6%~11% 的余量当时**根本没被记录**,
想查也没得查。

## 这条判据守什么

超时文案必须**带上一次成功用时**,并且据此给出倾向性判断:

  - 上次 473s,这次 600s 没完 → 差 126s,**多半是变慢**,先看有没有别的
    进程抢 CPU
  - 上次 80s,这次 600s 没完 → 差 520s,**更像真卡住**,去看卡在哪条
  - 基线里压根没有用时 → **明说分不清**,而不是假装知道

最后一条最重要:「没有数据就说没有」,而不是给一个听起来很确定的结论。
一个凭空而来的判断比没有判断更坏。

## 没有把 600s 调大

提额要走 `devloop accept`,那不是改一句报错文案能顺带决定的事。
本轮只做**诊断能力**:不改阈值,不加门禁,只让红的时候能看懂。

## 判据不许恒真

三条:
  - 合成的三种情形各自必须给出**不同的**文案(否则等于没分情况)
  - 没有用时的情况下,文案里不许出现任何具体秒数(不许编)
  - 判据自己被拆掉时,`tests/test_gates_verify_effects.py` 那种
    「门禁会不会说自己有效」的问题由本文件的 `_timeout_detail` 直接验
"""
from __future__ import annotations

import ast
import pathlib

from arl_lite.devloop.gates import _timeout_detail

REPO = pathlib.Path(__file__).resolve().parents[1]
GATES = REPO / "arl_lite" / "devloop" / "gates.py"


def test_slow_but_merely_slow_is_named_as_slow():
    """上次 473s → 这次 600s,差 ~127s:该说「多半是变慢」"""
    msg = _timeout_detail(473.0)
    assert "473s" in msg, f"没带上上次用时:{msg}"
    # 判的是**倾向词**,不是字面缺席:文案本来就该出现「卡死」二字
    # (「多半是机器变慢而不是卡死」),拿缺席当判据是判据自己写错了。
    assert "多半是机器变慢" in msg, f"差 127s 却没说多半是变慢:{msg}"
    assert "更像真的卡住" not in msg, f"差 127s 却判成卡住,判断反了:{msg}"


def test_a_real_hang_is_named_as_a_hang():
    """上次 80s → 这次 600s,差 520s:该说「更像真卡住」"""
    msg = _timeout_detail(80.0)
    assert "80s" in msg, f"没带上上次用时:{msg}"
    assert "更像真的卡住" in msg, f"差 520s 却没判成卡住:{msg}"
    assert "多半是机器变慢" not in msg, f"差 520s 还说变慢,判断反了:{msg}"


def test_the_two_verdicts_really_differ():
    """两种倾向必须给出**不同**的文案 —— 否则等于没分情况"""
    slow = _timeout_detail(473.0)
    hung = _timeout_detail(80.0)
    assert slow != hung, "两种情形给出了同一句话"


def test_missing_baseline_says_it_cannot_tell():
    """没有用时数据时:明说分不清,而且**不许编一个秒数出来**"""
    for bad in (None, 0, -5, "abc", []):
        msg = _timeout_detail(bad)
        assert "分不清" in msg, f"{bad!r} 的情形没明说分不清:{msg}"
        # 600 是阈值本身,不是「上次用时」;除了它不许出现别的秒数
        assert msg.count("s,") == 0 and msg.count("s。") == 0, (
            f"{bad!r} 的文案里出现了不该有的秒数(在编数据):{msg}")


def test_the_timeout_is_actually_wired_to_the_handler():
    """不许只写好辅助函数却没接上 —— 判据最常见的死法"""
    src = GATES.read_text(encoding="utf-8")
    assert "detail=_timeout_detail(prev_seconds)" in src, (
        "超时分支没调用 _timeout_detail —— 新写的辅助函数没人用")
    assert 'measured: dict = {"failed": failed, "passed": passed,' in src, (
        "measured 里没有记用时 —— 下一个超时就还是分不清")
    assert 'prev_seconds = base.get("seconds")' in src, (
        "上一次用时没从 baseline 里读出来")
