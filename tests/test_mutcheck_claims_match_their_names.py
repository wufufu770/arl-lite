"""r83:变异必须真的实现它名字声称的语义(已栽 5 次)

## 事故与代价

r73 M2/M4、r74 M5、r78 M2、r82 M3 —— 连续五次出现「假变异」:
变异的**名字**是我凭印象描述的预期语义,**代码**却只写了个空壳。

r82 的 M3 是当场现形的:名字叫「判据换回 owner 进程已退出」,实现却写了个
不存在的属性 `owner_alive_dead_flag_placeholder`。那只测到「会崩」,
测不到「会误报」—— 而这条变异的全部价值就在「会不会误报」。

共同的诊断信号:**变异存活时,答案永远是「名字和实现不一致」,不是「判据错了」**。
但我每次都要重新推一遍才能确认是哪一边错了。这个重推成本,就是这条队列项
的验收标准要消掉的东西。

## 机制:每条变异带一份「替换后该出现/不该再出现什么」的声明

声明写在 `CLAIMS` 里,`名字: (必须出现, 必须消失)`。跑判据**之前**先验:

    _check_claim_points_at_one_place(名字, 改前的文件)   # 声明指得准吗
    mutate(目标)
    _verify_claim(名字, 改后的文件)                       # 替换真做到了吗

声明写成**文本**而不是语义断言:这里要验的是「我的替换有没有真的落到我说的
地方」,不是「那个语义对不对」。语义对不对由变异存不存活回答 —— 那是判据的
职责,不是声明的。

## 写这套声明时又栽了一次,值得单独记

C2 的 `must_not` 我第一版写成 `LEFTOVER_HINT not in out`,而这一句在同一个
文件里出现了**三次**。拆掉其中一条,另外两条照样在,于是声明永远不成立 ——
**声明自己被同名兄弟喂饱了**。

这跟 r80 那条教训是同一个:判据的判定依据被别处同名的东西喂饱。
所以加了一条 `_check_claim_points_at_one_place`:`must_not` 的每一串在改前
必须**只出现一次**。r83 写这条判据时,4 条声明(M3/M5/M6/C2)全是被它抓出来的。

## 阈值为什么是 r82 而不是 r41

机制从 r82 起强制(r82 的 8 条是**我这一轮亲手写、亲手验过差异**的,写得出来;
r76–r81 那 32 条我不记得每条当初到底验到了什么,事后补声明等于**伪造当时的
证据** —— 那比没有声明更坏:r80 那条教训就是「存在检查冒充行为检查」)。

历史脚本(r41–r81)按文件名轮次号跳过,与 r78 那条自检判据同一套做法,零维护。
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MUTCHECK_DIR = REPO / "devloop"

# 机制从这一轮起强制。往前的脚本不补声明 —— 理由见模块 docstring。
CLAIMS_SINCE_ROUND = 82


def _round_of(path: pathlib.Path) -> int:
    return int(path.stem[len("mutcheck_r"):])


def scripts() -> list[pathlib.Path]:
    return sorted(MUTCHECK_DIR.glob("mutcheck_*.py"))


def _module_globals(tree: ast.Module) -> tuple[dict[str, str], dict[str, list[str]], dict[str, ast.AST]]:
    """模块级字符串常量、字符串字典(CLAIMS)、以及目标文件常量"""
    consts: dict[str, str] = {}
    dicts: dict[str, list[str]] = {}
    others: dict[str, ast.AST] = {}
    for n in tree.body:
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)):
            continue
        name = n.targets[0].id
        if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            consts[name] = n.value.value
        elif isinstance(n.value, ast.Dict) and name == "CLAIMS":
            for k, v in zip(n.value.keys, n.value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                   and isinstance(v, (ast.Tuple, ast.List)):
                    dicts[k.value] = [ast.literal_eval(e) for e in v.elts]
        else:
            others[name] = n.value
    return consts, dicts, others


def mutant_names(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for n in tree.body:
        if (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in ("MUTANTS", "COVERAGE_MUTANTS")
                and isinstance(n.value, ast.List)):
            for e in n.value.elts:
                if isinstance(e, ast.Tuple) and e.elts and isinstance(e.elts[0], ast.Constant):
                    out.append(e.elts[0].value)
    return out


def _source(name: str) -> str:
    return (MUTCHECK_DIR / f"mutcheck_r{name}.py").read_text(encoding="utf-8")


def _original_texts(paths) -> list[str]:
    out = []
    for p in paths:
        out.append((REPO / p).read_text(encoding="utf-8"))
    return out


def _path_expr(node: ast.AST) -> str | None:
    """把 `REPO / "devloop" / "mutcheck_r82.py"` 这种表达式还原成相对路径

    ## r83:第一版用 `ast.literal_eval`,对 BinOp 直接抛 ValueError

    于是**每一条变异的目标都解析不出来**,判据里的「唯一定位」和
    「能区分改前改后」两条一直在**空转** —— 报绿不是因为检查通过了,
    是因为它什么都没检查。

    这是今晚第四次栽在同一个病上(探针静默漏报,然后我拿它的输出下结论)。
    所以下面 `test_every_mutant_target_is_resolvable` 专门钉住「不许有
    解析不出来的目标」—— 解析不出来必须**报错**,不能跳过。
    """
    if isinstance(node, ast.Name):
        # `REPO` 是仓库根,记成空前缀。r83 第一版这里返回 None,
        # 结果整条 BinOp 链都解析不出来,16 条变异全部被静默跳过。
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_expr(node.left)
        right = _path_expr(node.right)
        if left is None or right is None:
            return None
        return f"{left}/{right}" if left else right
    if isinstance(node, ast.Call):         # str(...) / Path(...)
        for a in node.args:
            v = _path_expr(a)
            if v is not None:
                return v
    return None


def _mutant_targets(tree: ast.Module, consts, others) -> list[tuple[str, list[str]]]:
    """`(变异名, 目标文件相对路径)`;解析不出目标的变异**不**出现在结果里"""
    out = []
    for n in tree.body:
        if not (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in ("MUTANTS", "COVERAGE_MUTANTS")
                and isinstance(n.value, ast.List)):
            continue
        for e in n.value.elts:
            if not (isinstance(e, ast.Tuple) and len(e.elts) == 4
                    and isinstance(e.elts[0], ast.Constant)
                    and isinstance(e.elts[3], ast.Tuple)):
                continue
            rel = []
            for t in e.elts[3].elts:
                expr = t if isinstance(t, (ast.BinOp, ast.Call)) else others.get(
                    getattr(t, "id", ""), None)
                got = _path_expr(expr) if expr is not None else None
                if got:
                    rel.append(got)
            if rel:
                out.append((e.elts[0].value, rel))
    return out


def _unresolved_targets(path: pathlib.Path) -> list[str]:
    """哪些变异的目标路径解析不出来 —— 判据不许在它们身上空转"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    _, _, others = _module_globals(tree)
    resolved = {n for n, _ in _mutant_targets(tree, {}, others)}
    return [n for n in mutant_names(tree) if n not in resolved]


def _must_enforce() -> list[pathlib.Path]:
    return [s for s in scripts() if _round_of(s) >= CLAIMS_SINCE_ROUND]


def _skip_names() -> set[str]:
    return {s.name for s in scripts() if _round_of(s) < CLAIMS_SINCE_ROUND}


# ── 判据 ──

def test_every_enforced_script_declares_claims():
    """从 r82 起,变异脚本必须带 CLAIMS 声明"""
    missing = [s.name for s in _must_enforce() if "CLAIMS" not in s.read_text(encoding="utf-8")]
    assert not missing, (
        f"这些脚本没有变异自检声明:{missing}\n"
        "变异存活时唯一能确定的就是「名字和实现不一致」,而每次重新推一遍"
        "是哪一边错了,就是这条机制要消掉的成本")


def test_every_mutant_has_a_claim():
    """每个变异都得有声明 —— 漏一条,那条就永远只能靠猜"""
    gaps = {}
    for s in _must_enforce():
        tree = ast.parse(s.read_text(encoding="utf-8"))
        _, claims, _ = _module_globals(tree)
        unclaimed = [n for n in mutant_names(tree) if n not in claims]
        if unclaimed:
            gaps[s.name] = unclaimed
    assert not gaps, f"这些变异没有声明:{gaps}"


def test_every_claim_has_a_mutant():
    """反过来也成立:每条声明都得有一个**真的存在的**变异

    r88 补的另一半。上面那条 `test_every_mutant_has_a_claim` 守的是
    「变异 ⊆ 声明」,方向是单向的 —— 于是「声明里有一条根本不对应任何变异」
    可以畅通无阻。

    r87 实测就是这么漏的:那个脚本把实现变异 M2 改名成 C2 挪进了覆盖变异,
    列表改对了,`CLAIMS` 忘了改,留下一条指向不存在变异的声明。当时
    全部判据全绿。

    这跟 r83 逮到的是同一个病 —— 文档/声明描述了代码里没有的东西 ——
    而且出在**专门治这个病的那套机制自己身上**。
    """
    ghosts = {}
    for s in _must_enforce():
        tree = ast.parse(s.read_text(encoding="utf-8"))
        _, claims, _ = _module_globals(tree)
        # 一个 CLAIM 键同时挂在两个列表上,也算「对应关系不实」
        claimed = [n for n in claims if n not in mutant_names(tree)]
        if claimed:
            ghosts[s.name] = sorted(claimed)
    assert not ghosts, (
        f"这些声明指向的变异不存在于 MUTANTS/COVERAGE_MUTANTS:{ghosts}\n"
        "多半是变异改名或挪了类别时忘了改声明。\n"
        "留着它不会让任何变异被逮住,只会让读脚本的人去找一条不存在的变异。"
    )


def test_claims_have_a_non_empty_side():
    """声明不能两边都空 —— 那等于没写"""
    empty = {}
    for s in _must_enforce():
        tree = ast.parse(s.read_text(encoding="utf-8"))
        _, claims, _ = _module_globals(tree)
        bad = [n for n, pair in claims.items() if not any(pair)]
        if bad:
            empty[s.name] = bad
    assert not empty, f"这些声明两边都是空的:{empty}"


def test_every_mutant_target_is_resolvable():
    """每条变异的目标路径都必须解析得出来 —— 解析不出就别想被检查

    r83 第一版用 `ast.literal_eval` 解析 `REPO / "..."`,对 BinOp 直接抛
    ValueError,于是**每一条变异都被静默跳过**,下面两条判据一直在空转。
    解析不出来必须报错,不能跳过。
    """
    unresolvable = {}
    for s in _must_enforce():
        missing = _unresolved_targets(s)
        if missing:
            unresolvable[s.name] = missing
    assert not unresolvable, (
        f"这些变异的目标路径解析不出来,判据在它们身上会空转:"
        f"{unresolvable}")


def test_claims_point_at_exactly_one_place():
    """「必须消失」的每一串,改前必须**只出现一次**

    r83 写这套声明时 C2 栽在这:must_not 用了 `LEFTOVER_HINT not in out`,
    而那一句在同一个文件里出现三次 —— 拆掉一条,另外两条照样在,
    声明永远不成立,**被同名兄弟喂饱**。跟 r80 那条是同一个病。
    """
    bad = {}
    for s in _must_enforce():
        src = s.read_text(encoding="utf-8")
        tree = ast.parse(src)
        _, claims, others = _module_globals(tree)
        targets = dict(_mutant_targets(tree, {}, others))
        for name, (must_have, must_not) in claims.items():
            for piece in must_not:
                where = targets.get(name)
                if not where:
                    continue
                n = sum(t.count(piece) for t in _original_texts(where))
                if n != 1:
                    bad.setdefault(s.name, []).append(
                        f"{name}: {piece!r} 出现 {n} 次")
    assert not bad, (
        f"这些声明串指不准被替换的那一处,会被同名兄弟喂饱:\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in bad.items()))


def test_claims_can_tell_before_from_after():
    """声明必须**能区分改前与改后** —— 改前也成立的声明是装饰不是证据"""
    flat = {}
    for s in _must_enforce():
        tree = ast.parse(s.read_text(encoding="utf-8"))
        _, claims, others = _module_globals(tree)
        targets = dict(_mutant_targets(tree, {}, others))
        for name, (must_have, must_not) in claims.items():
            where = targets.get(name)
            if not where:
                continue
            before = "".join(_original_texts(where))
            new_bits = [p for p in must_have if p not in before]
            gone_bits = [p for p in must_not if p in before]
            if not new_bits and not gone_bits:
                flat.setdefault(s.name, []).append(name)
    assert not flat, (
        f"这些声明在改前就成立,区分不了改前改后,等于没写:{flat}")


def _called_inside_sweep(path: pathlib.Path, func_name: str) -> bool:
    """`_sweep` 的函数体里**真的调用了** `func_name` 吗

    ## r83:第一版栽在这,而且是同一个病栽的第三次

    第一版写的是「文件里出现 `_verify_claim` 这个名字就算接上了」。
    于是把 `_sweep` 里那一处调用删掉之后,判据照样绿 —— 因为
    `_check_claim_points_at_one_place` 还留在文件里,名字还在。

    **存在检查冒充行为检查。** r80 那条教训是判据被注释喂饱,
    这里是判据被同文件的另一处同名调用喂饱。判定必须落在**代码**上:
    走 `_sweep` 的 AST,找那个函数体里的真实调用。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sweep = next((n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_sweep"), None)
    if sweep is None:
        return False
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == func_name for n in ast.walk(sweep))


def test_the_sweep_actually_calls_the_verification():
    """机制必须真的接在 `_sweep` 上 —— 定义了不调用等于没加

    而且不能只看「文件里有没有这个名字」:把 `_sweep` 里那一处删掉,
    同文件另一处同名调用还在,名字检查照样绿(r83 实测)。
    """
    not_wired, only_text = [], []
    for s in _must_enforce():
        src = s.read_text(encoding="utf-8")
        for fn in ("_verify_claim", "_check_claim_points_at_one_place"):
            if not _called_inside_sweep(s, fn):
                if fn in src:
                    only_text.append(f"{s.name}:{fn} 只在别处出现")
                else:
                    not_wired.append(f"{s.name}:{fn}")
    assert not not_wired, (
        f"这些脚本的 `_sweep` 没有调用声明校验:{not_wired}\n"
        "定义一个函数然后不调它,是最容易骗过自己的一种写法")
    assert not only_text, (
        f"这些校验只在 `_sweep` 之外出现,变异流程里其实没跑:{only_text}\n"
        "「文件里有这个名字」不等于「跑变异时会用到它」")


def test_the_mechanism_rejects_a_mutation_that_lies():
    """**正控制组**:名字说 A、实现做 B 的变异必须被机制挡下

    这就是 r82 的 M3 那个形状 —— 名字叫「判据换回 owner 进程已退出」,
    实现却写了个不存在的属性。机制要是挡不住它,前面所有判据都白写。
    """
    sys.path.insert(0, str(MUTCHECK_DIR))
    try:
        import mutcheck_r82 as M
    finally:
        sys.path.pop(0)

    name = "M3-判据换回owner进程已退出(恒红那版)"
    # 前提:判据里那个跨轮条件确实存在,变异有真东西可换
    assert "claimed_at < last_finished" in M.JUDGE_WHY_BLOCK, (
        "正控制组的前提没了:M3 要替换的那一行已经不在")

    # r82 那版假实现:名字没变,代码指向一个不存在的属性
    fake = (
        "                why = \"\"\n"
        "                if by_id and by_id.owner_alive_dead_flag_placeholder:\n"
        '                    why = "  ← owner 进程已退出:多半是认领完忘了 done-item"\n'
    )
    with pytest.raises(AssertionError) as e:
        M._verify_claim(name, fake)
    assert "没有做到它名字声称的事" in str(e.value), (
        f"机制是用别的理由挡下它的,那就不证明它能逮住假变异:{e.value}")

    # 反过来:真的按名字实现的那份必须过 —— 机制不能把好变异也毙了
    real = (
        "                why = \"\"\n"
        "                if by_id and by_id.owner:\n"
        "                    alive = self.queue_mod.Queue.owner_alive(by_id.owner)\n"
        "                    if alive is False:\n"
        '                        why = "  ← owner 进程已退出:多半是认领完忘了 done-item"\n'
    )
    M._verify_claim(name, real)   # 不抛异常即通过


def test_historical_scripts_are_skipped_not_silently_ignored():
    """跳过必须是**可见的**,而且只能是少数派

    ## 数量守卫为什么从 `>= 35` 改成比例

    原写法是 `assert len(skipped) >= 35` —— 它守的是「阈值没被写错成
    一个小数字、于是几乎所有脚本都被跳过」。但它把**当前总数**焊死了:
    r41–r73 那 33 个脚本被删掉之后,skipped 从 41 掉到 8,这条直接红。

    一个会因为「少了几份资产」而红的守卫,是在惩罚正确操作。所以改成
    比例:被跳过的最多只能占四分之一。阈值一旦写错(比如写成 74),
    40 个脚本会整批落进 skipped,`8 * 4 <= 40` 立刻不成立。
    **删资产不会红,阈值写错会红** —— 这才是它本来要守的东西。

    ## r41–r73 为什么被删

    那 33 个脚本被 `test_mutcheck_scripts_are_self_checking.py` 判为
    「历史运行记录,不改」而 **skip** 掉。skip 的意思是**从不验证**,
    不是「验证通过」。它们和 40 个真在跑的脚本混在同一个目录里,
    看目录分不出哪个是资产 —— 所以删掉,并用下面那条守住不许回流。
    """
    skipped = _skip_names()
    total = len(scripts())
    assert skipped, (
        "一个脚本都没跳过 —— 要么阈值写错成比最老脚本还大,"
        "要么下面的「最老轮次」那条该红了"
    )
    assert len(skipped) * 4 <= total, (
        f"{len(skipped)}/{total} 个脚本被判成历史记录,超过四分之一 —— "
        f"CLAIMS_SINCE_ROUND={CLAIMS_SINCE_ROUND} 很可能写错了。"
        "这条守卫以前写的是绝对数量(>=35),结果在删掉 r41–r73 时"
        "因为「少了资产」而红 —— 那是在惩罚正确操作"
    )
    enforced = {s.name for s in _must_enforce()}
    assert not (skipped & enforced), "同一个脚本既被跳过又被强制"


def test_no_unverified_historical_script_survives():
    """r41–r73 已删除:它们是**从不验证**的历史记录,不是验证过的资产

    这一条守住那次删除本身。原来没有这条,所以「删掉」和「从来没删过」
    在行为上没区别 —— 下一个照着 r60 抄一个 r60_2.py,没人会响。

    它与上面那条互补:那条守「跳过的比例不许过大」,这条守「被删的那批
    不许回来」。两条都不是恒真 —— 放回任何一个 r41–r73 的脚本,
    这里立刻报出它的轮次号。
    """
    oldest = min(_round_of(s) for s in scripts())
    assert oldest >= 74, (
        f"最老的 mutcheck 脚本是 r{oldest},但 r41–r73 已经删掉了 —— "
        "那批是判据明说「历史运行记录,不改」的未验证脚本。"
        "要放回来,先说清楚它凭什么算已验证"
    )
