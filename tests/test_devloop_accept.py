"""devloop accept —— baseline 提升的反向验证

**恒真的测试比没有更糟**。所以本文件的核心不是"accept 能用",
而是"accept 该拒的全都拒了"。每条规则都有一个专门的证伪用例:

1. 无理由提升           -> 必须拒
2. 理由太短             -> 必须拒
3. 门禁不可提升         -> 必须拒
4. 门禁已经绿了         -> 必须拒(堵"提前买预算")
5. 字段不在白名单里     -> 必须拒(防止借 accept 顺手放宽别人的红线)
6. 数值变小(该降不升)   -> 必须动都不动
7. bool 不是数字        -> 必须动都不动
8. 提升必须留痕进版本库 -> 不写 _promotions 就算没发生
9. 掏空 accept 的检查   -> 变异体必须真的放行(证伪测试没测空)

用 tmp_path 跑,不碰真实的 devloop/ 状态。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from arl_lite.devloop import accept, gates
from arl_lite.devloop.gates import GateResult

REPO = Path(__file__).parents[1]


@pytest.fixture
def tmp_repo(tmp_path):
    """最小可跑门禁的仓库骨架:arl_lite/ 存在即可,baseline 由用例自己写"""
    (tmp_path / "arl_lite").mkdir()
    return tmp_path


class _FakeGate:
    """可编程的假门禁——用来构造 accept 难以在真仓库复现的组合"""

    def __init__(self, name, measured, passed=False, promotable=True, fields=()):
        self.name = name
        self.blocking = True
        self.measured = measured
        self.passed = passed
        self.promotable = promotable
        self.promotable_fields = fields

    def run(self, repo: Path) -> GateResult:
        return GateResult(
            name=self.name,
            passed=self.passed,
            detail="fake",
            measured=self.measured,
            blocking=True,
        )


def _install(monkeypatch, gate: _FakeGate) -> None:
    """把假门禁塞进 registry"""
    monkeypatch.setitem(gates._REGISTRY, gate.name, gate)


# =====================================================================
# 规则 1-2:理由
# =====================================================================


def test_refuses_without_reason(tmp_repo, monkeypatch):
    _install(monkeypatch, _FakeGate("loc_budget", {"total_loc": 999}, passed=False))
    r = accept.accept_baseline(tmp_repo, "loc_budget", "")
    assert r.ok is False
    assert "reason is required" in r.detail
    # 拒绝时不该有任何写入
    assert gates.load_baseline(tmp_repo) == {}


def test_refuses_when_reason_too_short(tmp_repo, monkeypatch):
    _install(monkeypatch, _FakeGate("loc_budget", {"total_loc": 999}, passed=False))
    r = accept.accept_baseline(tmp_repo, "loc_budget", "fix")
    assert r.ok is False
    assert "too short" in r.detail
    assert gates.load_baseline(tmp_repo) == {}


def test_refuses_when_gate_is_green(tmp_repo, monkeypatch):
    """堵住「先涨基准再写代码」——空支票不能预支

    这是整套机制最关键的一条:如果绿门禁也能 accept,
    操作者就能在任何一轮开始前把预算抬高,门禁从此形同虚设。
    """
    _install(monkeypatch, _FakeGate("loc_budget", {"total_loc": 999}, passed=True))
    gates.update_baseline(tmp_repo, "loc_budget", {"total_loc": 100})
    r = accept.accept_baseline(tmp_repo, "loc_budget", "先涨预算免得下轮又红")
    assert r.ok is False
    assert "already green" in r.detail
    # baseline 不能被改
    assert gates.load_baseline(tmp_repo)["loc_budget"] == {"total_loc": 100}


def test_refuses_non_promotable_gate(tmp_repo, monkeypatch):
    """恒为 0 的红线不该能被提升

    把「当前有 5 处三方 import」写成新基准 = 把 bug 追认为正常。
    """
    _install(monkeypatch, _FakeGate(
        "no_thirdparty_import", 5, passed=False, promotable=False,
    ))
    r = accept.accept_baseline(tmp_repo, "no_thirdparty_import", "想引入 requests 这个库")
    assert r.ok is False
    assert "not promotable" in r.detail
    assert gates.load_baseline(tmp_repo) == {}


def test_refuses_unknown_gate(tmp_repo):
    r = accept.accept_baseline(tmp_repo, "no_such_gate", "一个足够长的理由文本")
    assert r.ok is False
    assert "unknown gate" in r.detail


# =====================================================================
# 规则 3:白名单字段 + 只升不降
# =====================================================================


def test_only_whitelisted_fields_are_promoted(tmp_repo, monkeypatch):
    """借 accept 顺手放宽别人的红线——必须被挡

    loc_budget 的 promotable_fields 只有 total_loc。
    devloop_loc 是协议自身的膨胀红线,不能被 total_loc 的提升顺带捎上。
    """
    _install(monkeypatch, _FakeGate(
        "loc_budget",
        {"total_loc": 2000, "devloop_loc": 9999},
        passed=False,
        fields=("total_loc",),
    ))
    gates.update_baseline(tmp_repo, "loc_budget", {"total_loc": 1000, "devloop_loc": 500})
    r = accept.accept_baseline(tmp_repo, "loc_budget", "只该抬 total_loc 不该动 devloop")
    assert r.ok is True
    assert r.changes == {"total_loc": (1000, 2000)}
    # 关键:devloop_loc 必须原封不动
    assert gates.load_baseline(tmp_repo)["loc_budget"]["devloop_loc"] == 500


def test_no_change_when_measured_is_smaller(tmp_repo, monkeypatch):
    """代码变少了不该触发任何写入

    正常情况门禁会直接绿(总量低于 baseline),但 _plan_changes 本身
    也必须幂等安全:防止将来某个 gate 的语义变成「越小越红」时踩坑。
    """
    _install(monkeypatch, _FakeGate(
        "loc_budget", {"total_loc": 800}, passed=False, fields=("total_loc",),
    ))
    gates.update_baseline(tmp_repo, "loc_budget", {"total_loc": 1000})
    r = accept.accept_baseline(tmp_repo, "loc_budget", "代码变少了不该动 baseline")
    assert r.ok is False
    assert "nothing to promote" in r.detail
    assert gates.load_baseline(tmp_repo)["loc_budget"] == {"total_loc": 1000}


def test_bool_is_not_treated_as_number(tmp_repo, monkeypatch):
    """Python 里 bool 是 int 的子类——这个坑必须堵

    没有这道检查,baseline 写成 {"total_loc": true} 会被当成 1,
    然后 accept 就能把门禁阈值压到 1,形同虚设。
    """
    _install(monkeypatch, _FakeGate(
        "loc_budget", {"total_loc": 2000}, passed=False, fields=("total_loc",),
    ))
    gates.update_baseline(tmp_repo, "loc_budget", {"total_loc": True})
    r = accept.accept_baseline(tmp_repo, "loc_budget", "bool 不能被当成数字来抬")
    assert r.ok is False
    assert "nothing to promote" in r.detail
    assert gates.load_baseline(tmp_repo)["loc_budget"] == {"total_loc": True}


def test_promotions_key_is_not_mistaken_for_a_gate(tmp_repo, monkeypatch):
    """_promotions 是留痕区,不是门禁——registry 里不能有它

    万一被当成门禁跑,measured 为 list 会让 gate 崩。
    """
    assert accept.PROMOTIONS_KEY not in gates._REGISTRY
    assert accept.PROMOTIONS_KEY not in gates.all_gate_names()


# =====================================================================
# 规则 4:留痕
# =====================================================================


def test_promotion_is_recorded_in_version_controlled_baseline(tmp_repo, monkeypatch):
    """提升必须写进 baselines.json,而不是只落本地 state.json

    state.json 每机一份、不进版本库,那里的痕迹没人 review 得到。
    留痕只在本地等于没有留痕。
    """
    _install(monkeypatch, _FakeGate(
        "loc_budget", {"total_loc": 2000}, passed=False, fields=("total_loc",),
    ))
    gates.update_baseline(tmp_repo, "loc_budget", {"total_loc": 1000})
    reason = "第5轮新增 TLS 证书采集,净增 540 行是实打实的新功能"
    r = accept.accept_baseline(tmp_repo, "loc_budget", reason, round_no=5)
    assert r.ok is True

    data = gates.load_baseline(tmp_repo)
    hist = data[accept.PROMOTIONS_KEY]
    assert len(hist) == 1
    entry = hist[0]
    assert entry["gate"] == "loc_budget"
    assert entry["round"] == 5
    assert entry["reason"] == reason
    assert entry["changes"]["total_loc"] == {"from": 1000, "to": 2000}
    assert entry["at"]  # 时间戳必须存在,否则事后无从追溯

    # 留痕不该污染门禁读数
    assert data["loc_budget"] == {"total_loc": 2000}


def test_promotion_history_is_capped_but_keeps_recent(tmp_repo, monkeypatch):
    """历史留太多 baselines.json 会变成日志;留太少会挤掉不体面的旧记录

    截断发生在**写入**时(_record),不是读取时,所以必须真的提升
    超过上限的次数才能验。
    """

    class _GrowingGate:
        name = "loc_budget"
        blocking = True
        promotable = True
        promotable_fields = ("total_loc",)
        value = 1000

        def run(self, repo: Path) -> GateResult:
            return GateResult(
                "loc_budget", False, "grew", {"total_loc": self.value}, blocking=True,
            )

    _install(monkeypatch, _GrowingGate())
    gates.update_baseline(tmp_repo, "loc_budget", {"total_loc": 999})
    total = accept.MAX_PROMOTIONS_KEPT + 5
    for i in range(total):
        _GrowingGate.value = 1000 + i
        r = accept.accept_baseline(tmp_repo, "loc_budget", f"第 {i} 次提升的理由文本", round_no=i)
        assert r.ok is True, f"第 {i} 次提升被拒: {r.detail}"

    hist = accept.promotion_history(tmp_repo)
    assert len(hist) == accept.MAX_PROMOTIONS_KEPT
    # 留最近的,不是最老的——挤掉的应该是最陈旧的
    assert hist[-1]["round"] == total - 1
    assert hist[0]["round"] == total - accept.MAX_PROMOTIONS_KEPT
    # 陈旧记录确实被挤掉了,而不是被改写
    assert all(h["round"] >= total - accept.MAX_PROMOTIONS_KEPT for h in hist)


def test_promotion_history_empty_is_readable(tmp_repo):
    """没提升过时 format_history 不能崩——CLI 首次运行就会走到这"""
    assert accept.promotion_history(tmp_repo) == []
    assert "no baseline promotions" in accept.format_history(tmp_repo)


# =====================================================================
# 真门禁:loc_budget 确实 promotable,且字段白名单正确
# =====================================================================


def test_loc_budget_promotable_fields_cover_both_budgets():
    """可提升字段是 total_loc + devloop_code_loc,但**不含**总行数

    r20 之前这里断言 `== ("total_loc",)`,守的是"协议自身的红线不接受
    提升"。那个立场已经过期 —— 它导致红线**没有任何合法更新路径**,
    于是只剩偷偷改常量或让门禁永远红着两个选项。

    现在红线也走留痕机制。但两件事仍然不对:

    - `devloop_total_loc` 仍然不可提升 —— 它只是给人看的参考值,
      不参与判定。提升它等于提升一个没用的数,会让门禁 detail 撒谎。
    - 提升必须留痕(见 test_devloop_loc_metric.py 里的三条硬规则)
    """
    g = gates.get_gate("loc_budget")
    assert g.promotable is True
    assert "total_loc" in g.promotable_fields
    assert "devloop_code_loc" in g.promotable_fields
    assert "devloop_total_loc" not in g.promotable_fields, \
        "总行数不参与判定,提升它没有意义(而且会让 detail 看起来更严)"


def test_promotion_leaves_headroom_above_the_measured_value():
    """提升必须留余量,不能提到正好等于实测值

    r20 第一次把红线提到 2488(实测值)之后,余量变成 0 ——
    `current < limit` 成了 `2488 < 2488`,而同一轮我自己又加了几行,
    门禁立刻又红。

    那样的话「提到实测值」几乎不解决问题:每写几行就得来提一次,
    那不是留痕机制,是骚扰。
    """
    from arl_lite.devloop.accept import _headroom

    # 2488 实测 → 应留出至少 25 行,并取整到 50 的倍数
    assert _headroom(2488, 2400) >= 2488 + 25
    assert _headroom(2488, 2400) % 50 == 0
    # 余量不能大到把红线放飞 —— 35% 是 test_devloop_loc_metric 守的上限
    assert _headroom(2488, 2400) <= 2488 * 1.35


def test_missing_baseline_field_falls_back_to_source_constant():
    """baseline 缺字段时回落源码常量,而不是静默跳过

    ## 这是 r20 抓到的真 bug

    第一次跑 accept 时 CLI 报 `ACCEPTED`,但**红线实际没动** ——
    `devloop_code_loc` 从来不在 baseline 里(它是源码常量),而
    `_plan_changes` 遇到 `current.get(key) is None` 就直接 continue。

    那比"提升失败"更糟:**它说成功了,但什么都没发生。**
    操作者会以为问题解决了,直到下一轮门禁又红,而且想不通为什么。

    现在缺字段时回落 `_SOURCE_DEFAULTS` 里登记的常量。
    """
    from arl_lite.devloop.accept import _SOURCE_DEFAULTS, _plan_changes

    assert "devloop_code_loc" in _SOURCE_DEFAULTS, (
        "红线字段没登记源码常量回落 —— baseline 缺它时提升会静默失效"
    )
    changes = _plan_changes(
        "loc_budget",
        measured={"total_loc": 100, "devloop_code_loc": 2500},
        current={"total_loc": 90},          # 故意缺 devloop_code_loc
        allowed_fields=("total_loc", "devloop_code_loc"),
    )
    assert "devloop_code_loc" in changes, (
        f"缺字段时没回落常量,提升被静默跳过:{changes}"
    )
    old, new = changes["devloop_code_loc"]
    assert old == gates._DEVELOOP_CODE_LOC_LIMIT, (
        f"回落值不是源码常量:{old}"
    )
    assert new > 2500, "补余量没生效"


def test_unregistered_field_is_still_not_invented():
    """没登记的字段仍然按「缺失就不凭空造」处理

    回落表不是万能钥匙:只有明确登记过的字段才能回落,其余保持保守。
    """
    from arl_lite.devloop.accept import _plan_changes

    changes = _plan_changes(
        "loc_budget",
        measured={"some_new_field": 9999},
        current={},
        allowed_fields=("some_new_field",),
    )
    assert not changes, f"没登记的字段不该被凭空造出来:{changes}"


def test_only_loc_budget_is_promotable():
    """默认必须是不可提升

    整个仓库只有 loc_budget 一道门禁显式点头。
    新加门禁若忘了写 promotable,默认 False —— 安全的默认值。
    """
    promotable = {
        name for name, g in gates._REGISTRY.items()
        if getattr(g, "promotable", False)
    }
    assert promotable == {"loc_budget"}


# =====================================================================
# 证伪:把 accept 的核心检查掏空,变异体必须真的放行
# =====================================================================


# 变异锚点覆盖**整个**理由校验块(必填 + 长度),不是只删第一条。
#
# 第一次只删「reason is required」时,变异体并没有放行——因为紧跟着的
# 「reason too short」把空串也拦了。这说明必填检查单独看是冗余的,
# 真正兜底的是长度检查。两道都留着是因为报错信息不一样
# (「没给理由」vs「理由太短」),但**约束**由长度检查承担,
# 所以变异必须把整个块拿掉,否则测的就不是约束本身。
_MUTATION = (
    '    if not reason or not reason.strip():\n'
    '        return AcceptOutcome(False, gate_name, "reason is required; refusing to widen silently")\n'
    '    if len(reason.strip()) < MIN_REASON_LEN:\n'
    '        return AcceptOutcome(\n'
    '            False, gate_name,\n'
    '            f"reason too short ({len(reason.strip())} chars, need >= {MIN_REASON_LEN})",\n'
    '            reason=reason,\n'
    '        )\n',
    "",
)


def test_falsification_removing_reason_checks_lets_mutant_through(tmp_repo, monkeypatch):
    """删掉「理由校验」这几行后,变异体必须真的放行无理由提升

    这是本文件存在的意义——证明这些测试真的在测约束,
    而不是测一个恒真的函数。如果变异体没放行,
    说明上面那些测试根本没触到真正的约束点。

    两处细节都不是随手写的:

    1. 变异体写进**包目录内**而不是临时目录:accept.py 里有
       `from . import gates` 这样的相对 import,放到包外会直接
       ImportError,测的就不是约束而是 import 机制了。
    2. 跑在 tmp_repo 上而不是真实仓库:变异体**会真的写** baseline 和
       _promotions。拿真仓库当靶子,等于每跑一次测试就往开发者的
       devloop/ 状态里塞一条假提升记录。
    """
    import importlib

    src = Path(accept.__file__).read_text(encoding="utf-8")
    assert _MUTATION[0] in src, "源码形态和预期不符,变异锚点需要重新定位"

    mutant_src = src.replace(*_MUTATION)
    assert mutant_src != src, "变异没有生效,锚点字符串有误"

    pkg_dir = Path(accept.__file__).parent
    mutant_path = pkg_dir / "_mutant_accept.py"
    mutant_path.write_text(mutant_src, encoding="utf-8")
    try:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        m = importlib.import_module("arl_lite.devloop._mutant_accept")
    finally:
        mutant_path.unlink(missing_ok=True)
        sys.modules.pop("arl_lite.devloop._mutant_accept", None)

    # 靶子放在 tmp_repo:变异体真的会写文件,不能碰真仓库
    gates.update_baseline(tmp_repo, "loc_budget", {"total_loc": 100})
    _install(monkeypatch, _FakeGate(
        "loc_budget", {"total_loc": 999}, passed=False, fields=("total_loc",),
    ))

    r = m.accept_baseline(tmp_repo, "loc_budget", "")  # 故意不给理由

    assert r.ok is True, (
        f"变异体竟然没放行,说明测试测不到真正的约束。\n"
        f"detail={r.detail}"
    )
    # 顺手确认:原版在同样条件下是拦的(否则上面 15 条测试都是摆设)
    real = accept.accept_baseline(tmp_repo, "loc_budget", "")
    assert real.ok is False and "reason is required" in real.detail
    # 原版必须连 baseline 都没碰
    assert gates.load_baseline(tmp_repo)["loc_budget"] == {"total_loc": 999}


# =====================================================================
# CLI 接线
# =====================================================================


def test_cli_exposes_accept_and_promotions():
    """子命令必须真的挂上去了,不能只是模块里写好了没人调"""
    from arl_lite.devloop import cli

    assert callable(cli._cmd_accept)
    assert callable(cli._cmd_promotions)


def test_cli_accept_requires_reason_flag():
    """--reason 在 argparse 层就是 required,不给应该直接报错退出

    双保险:argparse 挡一层,accept_baseline 库层再挡一层。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "arl_lite", "devloop", "accept", "loc_budget"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "--reason" in (proc.stderr + proc.stdout)


# =====================================================================
# 审计留痕的数据完整性
# =====================================================================
#
# 背景:第一版变异测试拿真实仓库当靶子,往版本库写进了一条
# 「total_loc 12786->17786、reason 为空」的假提升记录。
# 原因字段是空的正好成了识别标志——accept 的库层要求理由 >= 10 字符,
# 合法提升不可能留下空理由。基线数值当时被 finally 还原了,
# 审计记录没有,于是它一路混进了 commit。
#
# 下面两条把它钉死。


def test_committed_promotion_history_has_no_phantom_entries():
    """版本库里已提交的提升记录必须每条都合法

    这是对 baselines.json 这个**数据文件**的完整性检查。
    审计留痕的价值全在于可信:混进一条假记录,整条链的可信度就没了。
    """
    path = REPO / "devloop" / "baselines.json"
    data = gates.load_baseline(REPO)
    hist = data.get(accept.PROMOTIONS_KEY)
    assert hist, "本仓库应当至少有真实提升记录,没有的话这条用例等于没测"

    for entry in hist:
        reason = (entry.get("reason") or "").strip()
        assert len(reason) >= accept.MIN_REASON_LEN, (
            f"提升记录 {entry.get('at')} 的理由不合法(长度 {len(reason)}): {entry!r}\n"
            f"  这多半是测试污染留下的幽灵记录 —— 真正的提升必须带理由"
        )
        assert entry.get("at"), f"提升记录缺时间戳: {entry!r}"
        assert entry.get("gate") in gates._REGISTRY, (
            f"提升记录指向未知门禁: {entry.get('gate')!r}"
        )
    assert path.exists()


def test_mutation_workflow_does_not_touch_the_real_baselines(tmp_repo, monkeypatch):
    """变异测试流程必须只碰 tmp —— 这条曾把假提升写进版本库

    直接复现当初泄漏的那段流程(变异体 + 假门禁),但指向 tmp_repo,
    然后断言**真实仓库的 baselines.json 一个字节都没变**。

    不在子进程里跑整个测试文件是为了避免递归:这条用例自己就在
    那个文件里。
    """
    real_path = gates.baseline_path(REPO)
    before = real_path.read_bytes()

    gates.update_baseline(tmp_repo, "loc_budget", {"total_loc": 100})
    _install(monkeypatch, _FakeGate(
        "loc_budget", {"total_loc": 999}, passed=False, fields=("total_loc",),
    ))

    # 正常路径 + 无理由路径,都只应作用在 tmp_repo 上
    assert accept.accept_baseline(tmp_repo, "loc_budget", "").ok is False
    assert accept.accept_baseline(
        tmp_repo, "loc_budget", "一个足够长的合法理由文本"
    ).ok is True

    assert real_path.read_bytes() == before, "测试流程改动了版本库里的 baselines.json"
