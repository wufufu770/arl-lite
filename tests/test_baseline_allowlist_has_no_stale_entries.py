"""基线不许记假账 —— 名单里每一条,现在都还真的是失败

## 起因:r93 修好了 9 条,名单却还挂着它们

r92/r93 先后把那 9 条从没执行过的 async 测试修好,七门禁实测
**1249 passed / 0 failed**。`test_baseline` 门禁当场提示:

    all 9 baseline failure(s) now pass, shrink allowed_failures: ...

提示得很响,但**没有人执行**。门禁自己也绝不自己改名单
(r93 的 `test_the_gate_still_never_shrinks_its_own_allowlist` 守着这条,
理由是:门禁若能自己收紧,就等于可以用「把失败写进基线」让自己变绿)。

于是名单就一直躺着。r94 实测它到底有多值钱 —— 把**已经修好的**
`test_e2e_chain` 再次弄坏:

    e2e_chain 再次被弄坏   passed=True | all 1 failure(s) are known baseline ones

**9 张免费通行证。** 门禁守的是「别引入名单外的新失败」,而这 9 条
曾经就是名单内的失败 —— 于是任何人对它们做的回归都会被静默吸收。
更阴险的是账面上完全看不出来:基线记着 `failed: 9`,运行确实是红的,
两边都「对得上」。

## 这一轮做了什么

**由人收紧基线**(手改 `devloop/baselines.json`,不是 `--update-baseline` ——
那个标志会把所有 gate 的 measured 写进去,包括 `loc_budget`,
等于绕过 `devloop accept` 偷偷抬红线)。收紧是**让门禁更严**:那 9 条
从此必须绿。

**以及这份判据**,让同样的假账**当场变红**,而不是躺在 detail 里等人想起来。

## 每条判据都跑两遍:一遍真基线,一遍合成坏样本

## 这是 r90 的教训,不重蹈

名单收紧之后它是**空的**。空名单下,「自洽吗」「有幽灵吗」这类问题
压根不会被触发 —— 判据恒过,于是任何削弱它的变异都能存活。
r90 实测过一模一样的形状:三条判据首版全存活,因为现场数据没触到
被削弱的规则;加 `root` 参数喂合成样本后全被杀。

所以这里把每条检查都抽成**纯函数**,判据既验真实基线干净,
也断言这些纯函数**对一份构造的坏样本有反应**。判据恒过是这里最大的
风险,不是「写得太严」。

## 顺带记一个正控制逮到的真 bug

`_collected_identities` 第一版只认 `ast.FunctionDef`,漏了
`ast.AsyncFunctionDef` —— 于是那 9 条 `async def test_*`(正是 r92 接管
的那些)全被报成幽灵身份。名单收紧后这条判据恒过,bug 藏着;
一放回陈旧名单当正控制,立刻现形。判据 3 就是钉死这一点的。
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
BASELINES = REPO / "devloop" / "baselines.json"
TESTS = REPO / "tests"


# ── 纯函数:检查逻辑都在这里,判据拿它们跑真基线和合成样本 ──

def _is_test_fn(node: ast.AST) -> bool:
    """测试函数 —— **async def 也算**

    r94 正控制逮到的:第一版只认 `ast.FunctionDef`,于是那 9 条
    `async def test_*` 全被报成幽灵。名单收紧后判据恒过,bug 藏着。
    """
    return (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_"))


def _identities_of(tree: ast.AST, rel: str) -> set[str]:
    """一个模块里真实存在的测试身份 `rel::name` / `rel::Class::method`"""
    out: set[str] = set()
    for node in getattr(tree, "body", ()):
        if _is_test_fn(node):
            out.add(f"{rel}::{node.name}")
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if _is_test_fn(sub):
                    out.add(f"{rel}::{node.name}::{sub.name}")
    return out


def _consistency_problems(b: dict) -> list[str]:
    """`failed` 与名单长度对不上时的说明;空列表 = 没毛病"""
    allowed = b.get("allowed_failures") or []
    failed = b.get("failed", 0)
    return ([] if failed == len(allowed)
            else [f"failed={failed} 但名单有 {len(allowed)} 条"])


def _ghost_identities(b: dict, real: set[str]) -> list[str]:
    """名单里在仓库中**不存在**的身份"""
    return [i for i in (b.get("allowed_failures") or []) if i not in real]


def _stale_entries(listed: list[str], failed_ids: set[str]) -> list[str]:
    """名单里**已经不在失败**的条目

    pytest 打印的身份可能带参数化后缀(`t[a-b]`),按主干比。
    """
    return [i for i in listed
            if not any(f == i or f.startswith(i + "[") for f in failed_ids)]


def _baseline() -> dict:
    return json.loads(BASELINES.read_text(encoding="utf-8"))["test_baseline"]


def _allowed() -> list[str]:
    return list(_baseline().get("allowed_failures") or ())


def _collected_identities() -> set[str]:
    out: set[str] = set()
    for path in sorted(TESTS.rglob("test_*.py")):
        rel = path.relative_to(REPO).as_posix()
        out |= _identities_of(ast.parse(path.read_text(encoding="utf-8")), rel)
    return out


# ── 判据 1:内部自洽(真基线 + 合成假账)──

def test_failed_count_equals_the_length_of_the_allowlist():
    """`failed` 必须等于 `len(allowed_failures)` —— 两边对不上就是账做不平"""
    assert not _consistency_problems(_baseline()), (
        f"基线自己就对不上:{_consistency_problems(_baseline())}\n"
        f"门禁读的就是这两个字段,对不上时它拿到的是自相矛盾的基线"
    )
    # 合成样本:这条判据不许对假账无反应
    for bad in ({"failed": 3, "allowed_failures": ["a", "b"]},
                {"failed": 0, "allowed_failures": ["a", "b"]},
                {"failed": 1, "allowed_failures": []}):
        assert _consistency_problems(bad), (
            f"自洽检查对构造的假账没反应,它在这份样本上是恒过的:{bad}")


# ── 判据 2:不许有幽灵身份(真基线 + 合成幽灵)──

def test_every_allowlisted_identity_actually_exists():
    """名单里不许有**不存在**的测试 —— 幽灵条目永远匹配不到任何东西

    它不会造成假绿(匹配不到就等于没名单),但它让 `failed` 与现实脱钩:
    名单 10 条、实际只失败 2 条,门禁拿到的是一份自相矛盾的基线。
    """
    real = _collected_identities()
    assert not _ghost_identities(_baseline(), real), (
        f"基线里有幽灵身份(测试被删了或改名了,名单没跟上):"
        f"{_ghost_identities(_baseline(), real)}"
    )
    assert _ghost_identities(
        {"allowed_failures": ["tests/nope.py::test_gone", "tests/x.py::t"]},
        {"tests/x.py::t"},
    ), "幽灵检查对构造的样本没反应"


# ── 判据 3:身份收集器必须认得 async def(r94 正控制逮到的真 bug)──

def test_the_identity_collector_understands_async_tests(tmp_path):
    """`async def test_*` 也必须被收集到身份里

    第一版只认 `ast.FunctionDef`,于是 r92 接管的那些 `async def test_*`
    全被判成幽灵。**这份判据必须独立于基线内容** —— 名单收紧后它是空的,
    判据 2 恒过,bug 就藏在恒过里。判据 3 不看基线,所以它一直在守。
    """
    mod = tmp_path / "test_mixed.py"
    mod.write_text(
        "import pytest\n"
        "def test_plain(): pass\n"
        "async def test_coro(): pass\n"
        "def helper(): pass\n"
        "class TestGroup:\n"
        "    def test_m(self): pass\n"
        "    async def test_am(self): pass\n",
        encoding="utf-8",
    )
    got = _identities_of(ast.parse(mod.read_text(encoding="utf-8")), "m.py")
    want = {"m.py::test_plain", "m.py::test_coro",
            "m.py::TestGroup::test_m", "m.py::TestGroup::test_am"}
    assert got == want, (
        f"身份收集不对 —— async def 被漏掉了(那正是 r94 正控制逮到的):"
        f"\n  实际 {sorted(got)}\n  期望 {sorted(want)}")


# ── 判据 4:名单非空时真跑它们(真运行 + 合成解析)──

@pytest.mark.skipif(not _allowed(), reason="名单是空的,没有可验的条目")
def test_allowlisted_failures_are_still_actually_failing():
    """**真跑一遍**名单里那些,断言它们真的还在失败

    这是整份判据里唯一有牙齿的一条:前面几条都是读文件,这一条验行为。

    为什么要真跑:「名单里有一条已经不失败了」这件事,在账面上完全看不出来
    —— 基线说 `failed: 9`,运行也确实是红的,两边都「对得上」。r94 实测
    正是这样:9 条全部修好之后,`test_e2e_chain` 被再次弄坏,门禁报
    `passed=True` 并写明「all 1 failure(s) are known baseline ones」。

    成本:名单非空才跑。r94 正控制实测 87.75s(9 条);名单清空后本条 skip,
    一分钱不花。**成本恰好只在需要它的时候付。**
    """
    listed = _allowed()
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *listed,
         "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, timeout=900,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    out = r.stdout + r.stderr
    failed_ids = {ln.split()[1] for ln in out.splitlines()
                  if ln.startswith("FAILED ") and len(ln.split()) > 1}
    stale = _stale_entries(listed, failed_ids)
    assert not stale, (
        f"基线里有 {len(stale)} 条**已经不失败**了 —— 它们是假账,也是"
        f"免费通行证(把其中一条弄坏,门禁会因为「已知基线失败」而放行):\n  "
        + "\n  ".join(stale)
        + f"\n实测真的在失败的:{sorted(failed_ids)}")


def test_the_stale_entry_check_handles_parametrized_ids():
    """真跑出来的身份带参数化后缀,不能被误判成「已经不失败」

    判据 4 里那条「实测真的在失败的」解析,必须认得 `t[a-b]` 这种形状。
    认不得的话,参数化测试会被一律算成假账 —— 那是一条假红,比假绿还
    容易让人白查一轮。
    """
    listed = ["t.py::test_a", "t.py::test_b"]
    assert _stale_entries(listed, {"t.py::test_a", "t.py::test_b[x-y]"}) == [], (
        "参数化身份 t.py::test_b[x-y] 被误判成假账")
    assert _stale_entries(listed, {"t.py::test_a"}) == ["t.py::test_b"], (
        "真不失败的条目没被认出来")
    assert _stale_entries(listed, set()) == listed, (
        "一条都没失败时,名单应该整条都是假账")


# ── 判据 5:收紧得是「人」做的,不是门禁顺手改的 ──

def test_the_baseline_was_not_written_by_a_gate():
    """`loc_budget` 的基线不许被顺手带上 —— 那等于绕过 `devloop accept`

    收紧名单的正确做法是手改 `devloop/baselines.json` 的**一个**字段块。
    `devloop gates --update-baseline` 是另一回事:它会把**所有** gate 的
    measured 一起写进去,`devloop_code_loc` 也跟着变成当前值 ——
    红线就没有红线了(r91 差点这么干,已中止)。
    """
    data = json.loads(BASELINES.read_text(encoding="utf-8"))
    loc = data["loc_budget"]
    assert loc["devloop_code_loc"] == 2900, (
        f"devloop 红线被动过了:{loc['devloop_code_loc']}(应为 2900)—— "
        f"提额只能走 `devloop accept` 并留痕")
    assert loc["total_loc"] == 16269, (
        f"总行数基线被动过了:{loc['total_loc']}(应为 16269)")
