"""r27: `TestBaselineGate` 身份判定的单元测试

7.18 节那个失效形态(总数持平 -> 放行新回归)必须被钉死在这份文件里。
所以这里 mock 掉 `subprocess.run`,直接喂构造的 pytest 摘要,毫秒级跑完 ——
不跑真 pytest,因为要验的是**判定逻辑**,不是 pytest 跑不跑得动。

真实场景的 2 分钟代价由 7.18 节那次四场景端到端验证付过了,这里只钉规则。
"""
from __future__ import annotations

import subprocess

import pytest

from arl_lite.devloop import gates

KNOWN = [
    "tests/test_phase1.py::test_3state_discipline",
    "tests/test_phase1.py::test_base_module_3state",
]
KNOWN += [f"tests/test_phase2.py::test_async_{i}" for i in range(7)]  # 共 9 条
FIXED = "tests/test_phase1.py::test_already_fixed_by_someone"
NEWCOMER = "tests/test_new.py::test_freshly_broken"


def _pytest_output(failed_ids, n_failed, n_passed, with_ids=True):
    lines = ["=========================== short test summary info ============================"]
    if with_ids:
        lines += [f"FAILED {i} - Failed: async def functions are not natively supported."
                  for i in failed_ids]
    lines.append(f"{n_failed} failed, {n_passed} passed, 369 warnings in 1.00s")
    return "\n".join(lines)


@pytest.fixture
def gate(monkeypatch):
    """一个门禁 + 两个开关:喂什么输出、基线长什么样"""
    box = {"output": "", "baseline": {}, "cmd": None}

    def fake_run(cmd, **kw):
        box["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, box["output"], "")

    monkeypatch.setattr(gates.subprocess, "run", fake_run)
    monkeypatch.setattr(
        gates, "load_baseline",
        lambda repo: {"test_baseline": box["baseline"]} if box["baseline"] else {},
    )
    box["feed"] = lambda failed_ids, nf, npx, ids=True: box.__setitem__(
        "output", _pytest_output(failed_ids, nf, npx, ids))
    return box


def _run(box):
    return gates.TestBaselineGate().run(gates.Path("."))


# --- 核心:总数持平也必须拦住新回归(7.18 的失效形态)---

def test_new_failure_is_caught_even_when_the_count_matches(gate):
    """总数 10==10,但那 10 条里有一条不在名单上 -> 必须报红"""
    gate["baseline"] = {"failed": 10, "passed": 433,
                        "allowed_failures": KNOWN + [FIXED]}
    gate["feed"](KNOWN + [NEWCOMER], 10, 433)
    r = _run(gate)
    assert not r.passed, "总数持平就放行了新回归 —— 计数式判定的老毛病回来了"
    assert NEWCOMER in r.detail, f"没点名是哪条失败,没法修: {r.detail}"


def test_all_known_failures_passes(gate):
    """对照组:失败全在名单里就该绿 —— 否则上面的红可能只是门禁坏了"""
    gate["baseline"] = {"failed": 9, "passed": 433, "allowed_failures": KNOWN}
    gate["feed"](KNOWN, 9, 433)
    assert _run(gate).passed


def test_fixed_failures_are_reported_as_shrinkable(gate):
    """修好了要看得见,否则名单只增不减,最后什么都挡不住"""
    gate["baseline"] = {"failed": 9, "passed": 433, "allowed_failures": KNOWN + [FIXED]}
    gate["feed"](KNOWN, 9, 433)
    r = _run(gate)
    assert r.passed
    assert "shrink" in r.detail and FIXED in r.detail, f"修好了却没提示收紧: {r.detail}"


# --- 门禁不许自己改名单 ---

def test_gate_never_shrinks_its_own_allowlist(gate):
    """门禁若自动收紧名单,就等于可以用"把失败写进基线"让自己变绿

    那样它就不再是防线,而是漏洞。所以名单只能由人改。
    """
    gate["baseline"] = {"failed": 9, "passed": 433, "allowed_failures": KNOWN + [FIXED]}
    gate["feed"](KNOWN, 9, 433)
    r = _run(gate)
    assert set(r.measured["allowed_failures"]) == set(KNOWN + [FIXED]), (
        "门禁自己把 FIXED 从名单里摘掉了 —— 没人批准过这件事"
    )


# --- 旧格式基线的迁移 ---

def test_migration_round_still_reports_a_regression(gate):
    """建立名单那一轮若总数在涨,必须照样报红

    否则「升级基线」就成了掩盖新回归的后门:任何时候都可以先让总数涨,
    再建立名单,红变绿。
    """
    gate["baseline"] = {"failed": 9, "passed": 293}   # 旧格式:只记总数
    gate["feed"](KNOWN + [NEWCOMER], 10, 433)
    assert not _run(gate).passed, "迁移轮把总数上涨当成了建立基线,放行了新回归"


def test_migration_round_records_identities(gate):
    """没有名单的第一次跑:记下身份,并如实写进 measured"""
    gate["baseline"] = {"failed": 9, "passed": 293}
    gate["feed"](KNOWN, 9, 433)
    r = _run(gate)
    assert r.passed
    assert set(r.measured["allowed_failures"]) == set(KNOWN)


# --- 兜底 ---

def test_falls_back_to_counting_when_no_identities_parsed(gate):
    """拿不到身份时退回计数判定,且保守方向是红"""
    gate["baseline"] = {"failed": 9, "passed": 433, "allowed_failures": KNOWN}
    gate["feed"]([], 12, 433, ids=False)
    r = _run(gate)
    assert not r.passed, "拿不到身份时总数涨了却放行"
    assert "no per-test identities" in r.detail


def test_command_actually_asks_pytest_for_the_identities(gate):
    """命令里必须有 -rf,否则 pytest 根本不打印 FAILED 行

    这条是被变异测试逼出来的:把 -rf 从 cmd 里删掉,上面 7 条**全部照过** ——
    因为 mock 无视 cmd 内容,直接喂了带 FAILED 行的输出。它们验的是
    「解析逻辑对」,不是「门禁真的去要了那份数据」。少了 -rf,真实环境里
    判定会悄悄退回按总数数,判别力归零而测试全绿。
    """
    gate["baseline"] = {"failed": 9, "passed": 433, "allowed_failures": KNOWN}
    gate["feed"](KNOWN, 9, 433)
    _run(gate)
    assert "-rf" in (gate["cmd"] or []), (
        f"pytest 命令里没有 -rf,拿不到失败身份: {gate['cmd']}"
    )
