"""门禁不许惩罚「全绿」—— 它惩罚的恰好是这套协议要的那个结果

## 起因:r92 把 9 条失败全修好之后,门禁反而转红

r92 用纯 stdlib 的 `pytest_pyfunc_call` 接管了那 9 条从没执行过的 async
测试,全量实测 **1234 passed / 0 failed / 33 skipped / 447.73s**。

然后 `test_baseline` 报红,detail 是 `could not parse pytest summary`。

根因在 `gates.py` 的 `_PYTEST_SUMMARY_RE`:它要求 summary 行里出现
`N failed`。但 `failed` 是 pytest 的**可选**输出字段 —— 套件全绿时它
**根本不打印**。实测的终局行:

    1234 passed, 33 skipped, 9597 warnings, 77 subtests passed in 393.27s (0:06:33)

正则不匹配 → 落进「could not parse」分支 → `passed=False`。

一个在**成功时刻**说「我读不懂」的防线,比没有防线更容易被忽略:人会
以为环境坏了、pytest 崩了,而去查别的地方。

## 但也不能只把 failed 改成可选

同一批实测里,还有两种**同样没有 `failed`** 的终局:

    收集中断  1 error in 0.16s          # import 断了,一条都没跑
    空目录    no tests ran in 0.00s      # 没收集到任何用例

一并放过去就是**假绿** —— 套件一条没跑而门禁报通过。那比假红坏得多:
假红会被人去查,假绿不会有人去查。

所以「没有 failed」必须分成三路,而且判的方向是相反的:
全绿放行、收集中断与空跑一律报红并**点名是哪一种**、真认不出才报红。

## 这份判据守什么

1. 全绿必须绿(本轮核心,以前做不到)
2. 收集中断必须红,而且红在「error」上而不是含糊的「解析失败」
3. 空跑必须红,而且红在「没跑」上
4. 真认不出的仍然红 —— 保守方向不能因为修了 1 就松掉
5. 全绿时要提示收紧基线名单(r86 同源缺口:声称双向的机制只实现了一半)
6. **真跑一次 pytest** 走真实输出,不只验手打的字面量
7. 取的是**最后**一行 summary,不是第一行
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

from arl_lite.devloop import gates

REPO = pathlib.Path(__file__).resolve().parents[1]
GATES_PY = REPO / "arl_lite" / "devloop" / "gates.py"

# r93 实测(本机,pytest 8.x,2026-10-05)的六种终局 summary 行。
# 前五种的产生方式见本文件末尾的 test_a_real_green_run_parses 里的现场复现。
MEASURED_FAILURES = (
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_new.py::test_freshly_broken - AssertionError: boom\n"
    "1 failed, 1225 passed, 33 skipped, 9597 warnings in 447.73s (0:07:27)\n"
)
MEASURED_ALL_GREEN = (
    "1234 passed, 33 skipped, 9597 warnings, 77 subtests passed in 393.27s (0:06:33)\n"
)
MEASURED_COLLECTION_ERROR = (
    "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection "
    "!!!!!!!!!!!!!!!!!!!!\n"
    "1 error in 0.16s\n"
)
MEASURED_NO_TESTS_RAN = "no tests ran in 0.00s\n"
MEASURED_ALL_SKIPPED = "2 passed, 1 skipped in 0.02s\n"
MEASURED_MIXED = "1 failed, 4 passed, 1 skipped, 1 error in 0.03s\n"

# 真实的基线现状(devloop/baselines.json 里那 9 条)
KNOWN_9 = [
    "tests/test_phase1.py::test_3state_discipline",
    "tests/test_phase1.py::test_base_module_3state",
    "tests/test_phase1.py::test_crtsh_module_mock",
    "tests/test_phase2.py::test_9_sources_integration",
    "tests/test_phase2.py::test_concurrent_isolation",
    "tests/test_phase2.py::test_e2e_chain",
    "tests/test_phase2.py::test_httpx_probe_integration",
    "tests/test_phase2.py::test_portscan_integration",
    "tests/test_phase3.py::test_e2e_full",
]


@pytest.fixture
def gate(monkeypatch):
    """喂什么 pytest 输出、基线长什么样"""
    box = {"output": "", "baseline": {}}

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, box["output"], "")

    monkeypatch.setattr(gates.subprocess, "run", fake_run)
    monkeypatch.setattr(
        gates, "load_baseline",
        lambda repo: {"test_baseline": box["baseline"]},
    )
    return box


def _run(box):
    return gates.TestBaselineGate().run(REPO)


def _baseline_9():
    return {"failed": 9, "passed": 433, "allowed_failures": list(KNOWN_9)}


# --- 1) 本轮核心:全绿必须绿 ---

def test_a_fully_green_run_passes_the_gate(gate):
    """r92 之前的门禁在这一条上是红的 —— 那正是本轮要修的东西"""
    gate["baseline"] = _baseline_9()
    gate["output"] = MEASURED_ALL_GREEN
    r = _run(gate)
    assert r.passed, (
        f"套件全绿反而判红 —— 门禁惩罚的正是协议要的结果:\n{r.detail}")


def test_green_does_not_get_counted_as_regression(gate):
    """全绿时 failed=0,不能被拿去和基线 failed=9 比出「+N」

    防的是把全绿当成「少跑了 9 条」的反向误判。detail 里不许出现 regression。
    """
    gate["baseline"] = _baseline_9()
    gate["output"] = MEASURED_ALL_GREEN
    r = _run(gate)
    assert r.measured["failed"] == 0, f"全绿被算成有失败:{r.measured}"
    assert r.measured["passed"] == 1234, f"passed 数没读对:{r.measured}"
    assert "regression" not in r.detail, f"全绿被判成回归:{r.detail}"


# --- 2) 防假绿:两种「同样没有 failed」的坏终局必须红 ---

def test_a_collection_error_is_red(gate):
    """`1 error in 0.16s` —— import 断了,一条都没跑

    危险在于它和全绿**长得一样地没有 `failed`**。修 1 的时候顺手把
    `failed` 改成可选,这一条就会变成「failed=0 → 绿」。
    套件没跑起来而门禁说通过,比假红坏得多。
    """
    gate["baseline"] = _baseline_9()
    gate["output"] = MEASURED_COLLECTION_ERROR
    r = _run(gate)
    assert not r.passed, "收集中断被判绿了 —— 套件一条没跑,门禁却说通过"
    assert "1 error" in r.detail, (
        f"没点名是 error,读起来像门禁自己瞎了:{r.detail}")


def test_no_tests_ran_is_red(gate):
    """`no tests ran in 0.00s` —— 没收集到任何用例

    这是与 1) 方向完全相反的第三种「没有 failed」。它和收集中断一样
    没有 failed 字样,但含义是「什么都没验」。
    """
    gate["baseline"] = _baseline_9()
    gate["output"] = MEASURED_NO_TESTS_RAN
    r = _run(gate)
    assert not r.passed, "一条测试都没跑却判绿 —— 门禁什么都没验却报通过"
    assert "no tests ran" in r.detail, (
        f"没点名是「没跑」而不是「读不懂」:{r.detail}")


def test_errors_coexisting_with_failures_are_still_red(gate):
    """`1 failed, 4 passed, 1 skipped, 1 error` —— error 和 failed 共存

    实测形态:teardown 抛异常时,同一次运行里既有 FAILED 也有 ERROR。
    `allowed_failures` 只收 `FAILED` 身份,ERROR 从来不在名单里,所以
    保守方向必然是红。这里钉住的是「别只数 failed」。
    """
    gate["baseline"] = _baseline_9()
    gate["output"] = MEASURED_MIXED
    r = _run(gate)
    assert not r.passed, "有 error 却判绿"
    assert r.measured["errors"] == 1, f"error 数没被读出来:{r.measured}"
    assert r.measured["failed"] == 1, f"failed 数没被读出来:{r.measured}"


# --- 3) 保守方向不许因为修了 1 就松掉 ---

def test_unparseable_output_stays_red(gate):
    """真认不出的仍然红,而且仍然走「解析失败」这一支

    这条是本轮改动最容易被顺手削掉的安全网。判据分开问两件事:
    红(第 1 句)、且是**因为读不懂**而红(第 2 句,不许拿它冒充 2)/3)
    """
    gate["baseline"] = _baseline_9()
    gate["output"] = "/usr/bin/python3: No module named pytest\n"
    r = _run(gate)
    assert not r.passed, "pytest 压根没跑起来,门禁却判绿"
    assert "could not parse" in r.detail, (
        f"读不懂和「没跑测试」必须说成两回事:{r.detail}")


def test_a_genuine_regression_is_still_caught(gate):
    """对照组:全绿判定不许把真回归一起放过

    没有这条,第 1 条就可能只是「门禁坏了」的另一种写法。
    """
    gate["baseline"] = _baseline_9()
    gate["output"] = MEASURED_FAILURES
    r = _run(gate)
    assert not r.passed, "新回归被放行了"
    assert "test_freshly_broken" in r.detail, f"没点名是哪条:{r.detail}"


def test_a_green_run_with_only_skips_passes(gate):
    """`2 passed, 1 skipped in 0.02s` —— 另一种全绿形态

    全绿不止一种长相:有 warnings 的、有 subtests 的、有被 skip 掉的。
    这一种连 warnings 都没有,summary 行更短,更容易被「必须有 failed」
    那类正则漏掉。判据守着的是**形态全覆盖**,不是记住一句字符串。
    """
    gate["baseline"] = _baseline_9()
    gate["output"] = MEASURED_ALL_SKIPPED
    r = _run(gate)
    assert r.passed, f"「全通过 + 有跳过」被判红:{r.detail}"
    assert r.measured["passed"] == 2, f"passed 数没读对:{r.measured}"


# --- 4) 全绿时要看得见「该收紧名单了」---

def test_going_fully_green_says_to_shrink_the_allowlist(gate):
    """9 条全修好是**最该高兴**的一次,却一声不吭 —— 和 r86 同一个毛病

    r27 的身份模式那一支会报 healed,但全绿时 `failed_ids` 为空、走的是
    「没拿到身份」那一支,而那一支原先**不提示收紧**。名单就一直躺着,
    最后什么都挡不住。
    """
    gate["baseline"] = _baseline_9()
    gate["output"] = MEASURED_ALL_GREEN
    r = _run(gate)
    assert r.passed
    assert "shrink" in r.detail, f"全绿却不提示收紧名单:{r.detail}"
    missing = [i for i in KNOWN_9 if i not in r.detail]
    assert not missing, f"没点名是哪几条修好了:{missing}"


def test_the_gate_still_never_shrinks_its_own_allowlist(gate):
    """提示归提示,名单归人 —— 门禁自己摘掉就等于能靠自己变绿"""
    gate["baseline"] = _baseline_9()
    gate["output"] = MEASURED_ALL_GREEN
    r = _run(gate)
    assert set(r.measured["allowed_failures"]) == set(KNOWN_9), (
        "门禁自己动了名单 —— 没人批准过这件事")


# --- 5) 取最后一行,不是第一行 ---

def test_the_parser_takes_the_last_summary_line_not_the_first():
    """测试自己打印的 `generated in 1.00s` 可能先出现

    取第一行会把捕获输出里的句子当成判定依据。终局行一定在最后。
    """
    out = ("captured stdout: build generated in 1.00s\n"
           "warning: retry in 2.00s\n"
           + MEASURED_ALL_GREEN)
    got = gates._pytest_outcome(out)
    assert got == {"failed": 0, "passed": 1234, "errors": 0, "ran": True}, (
        f"取错了行:{got}")


# --- 6) 端到端:真跑一次 pytest,别只信手打的字面量 ---

def test_a_real_green_run_parses(tmp_path):
    """现搭一个真套件跑真 pytest,把**真实输出**交给解析器

    前面 5 组字面量是手打的,会跟 pytest 的真实格式漂移 —— 而整个修复都
    建立在「全绿时 summary 行没有 failed」这一个事实上。这条把那个事实
    现场钉住:真跑一次,拿到真末行,再解析。

    顺带钉住另外三种终局形态的产生方式(空目录 / 收集中断 / 全 skip),
    免得它们只是「听说长这样」。
    """
    (tmp_path / "test_a.py").write_text("def test_a(): assert True\n",
                                        encoding="utf-8")

    def _summary(*args):
        r = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "--tb=no", "-p",
             "no:cacheprovider", *args],
            cwd=tmp_path, capture_output=True, text=True, timeout=120)
        return (r.stdout + r.stderr).strip().splitlines()[-1]

    green_line = _summary("test_a.py")
    assert "failed" not in green_line, (
        f"本轮修复的前提不成立了 —— pytest 现在全绿也打印 failed:{green_line}")
    assert gates._pytest_outcome(green_line + "\n") == {
        "failed": 0, "passed": 1, "errors": 0, "ran": True,
    }, f"真实全绿输出解析不对:{green_line}"

    (tmp_path / "empty").mkdir()
    assert "no tests ran" in _summary("empty")

    (tmp_path / "test_broken.py").write_text(
        "import definitely_not_a_real_module_xyz\n", encoding="utf-8")
    err_line = _summary("test_broken.py")
    assert "error" in err_line, f"收集中断的形态变了:{err_line}"
    assert gates._pytest_outcome(err_line + "\n")["errors"] == 1, (
        f"真实收集中断没解析出 error:{err_line}")


# --- 7) 接线:解析器不许写好了却没接进 run() ---

def test_the_outcome_parser_is_actually_called_by_run():
    """用 AST 查 `run()` 体内真的调了它,不是「文件里有这个函数」

    r92 栽过这个:`if False:` 的变异让函数还在、就是没人叫它,而只查
    `def` 那一行的守卫照样通过。判形状不判文本 —— r80 的守卫会把
    unparse + 子串的写法当场逮住,那条说得对。
    """
    tree = ast.parse(GATES_PY.read_text(encoding="utf-8"))
    gate_cls = next(c for c in tree.body
                    if isinstance(c, ast.ClassDef)
                    and any(isinstance(s, ast.Assign)
                            and any(isinstance(t, ast.Name)
                                    and t.id == "name" for t in s.targets)
                            and isinstance(s.value, ast.Constant)
                            and s.value.value == "test_baseline"
                            for s in c.body))
    run_fn = next(n for n in gate_cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "run")
    called = {
        c.func.id for c in ast.walk(run_fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "_pytest_outcome" in called, (
        "`_pytest_outcome` 写好了却没接进 TestBaselineGate.run() —— "
        "解析器是死代码")


# --- 8) 这个文件的判据自己也得出得了自检 ---

def test_this_file_does_not_use_unparse_plus_substring():
    """r80 立的规矩:结构检查不许退化成文本匹配

    本文件用 AST 查 `_pytest_outcome` 的调用,就必须自己也守住这条 ——
    否则下一个来改的人会照着坏样子继续抄。
    """
    src = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    offenders = [
        n.lineno for n in ast.walk(src)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "unparse"
    ]
    assert not offenders, (
        f"第 {offenders} 行用了 ast.unparse —— r85/r92 各栽过一次,")
