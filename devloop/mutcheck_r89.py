"""r89 变异测试:验「把 25 秒硬等换成轮询到目录出现就掐掉」没让判据变弱。

## 主题

r88 量出来一个真风险:全量耗时贴着 `test_baseline` 门禁的 600s 硬上限
(实测 476–565s,余量 6%~11%),而这台机器上还跑着别的项目,于是门禁会间歇
性假红。

先用 `--durations` 找肥肉 —— 结论是**没有单一热点**,30 条测试占了全量的
一半,分布很集中。真正离谱的是这两条:

    25.44s  test_watch_start_without_w_still_uses_default
    25.19s  test_watch_start_writes_into_the_named_workspace

`watch start` 是前台同步调度器,按设计跑到 Ctrl+C 才停。而这两条测试是
`subprocess.run(timeout=25)` + `except TimeoutExpired: pass` —— **等满
25 秒,把超时当正常路径接住**。而它们的 docstring 自己写着「验证方式刻意
**不依赖它跑完**」:意图和机制正好相反。

实测那个工作区目录 **0.39 秒**就建好(terminate+wait 0.01 秒)。所以真正
该等的是目录出现,不是时间耗完。两条合计 50.6s,占全量 10.6%。

## 换了机制之后,判据有没有变弱?三条注入实测

删掉一个 25 秒的等待,最容易出的事就是「测试其实一直在被超时接住,
现在没人接了,于是它其实什么都没验」。所以逐条注入:

| 注入 | 期望 | 实测 |
|---|---|---|
| helper 根本不调起命令 | 红 | **红**(`2 failed`) |
| 注入 r70 真 bug(`watch start` 无视 `-w`) | 红 | **红**(`没写进 teamA,建出的是:[]`) |
| 等一个永远不会出现的目录 | 红 | **绿** ← 见下 |

第三条绿了,但**绿得对**:那个路径只是**等待条件**,断言主体是
「建出了哪些工作区」。等错路径只让它多花 15 秒,不改变判定结果。
真正该问的是「断言还在不在、还承不承重」,前两条回答了。

r89 记录这一点,是因为它正是这轮最容易糊弄过去的地方:注入绿了,
一句话「实测通过」就能盖过去。绿要问**绿得对不对**。

## 变异清单

实现变异(期望全被杀):
  M1 helper 直接 return,命令压根不跑   → 两条断言全红
  M2 注入 r70 真 bug:watch start 无视 -w → named_workspace 那条红
  M3 进程起来就 terminate,不等目录出现   → named_workspace 那条红
  M4 deadline 提前到 0(不等就掐)        → 同上

覆盖变异(期望全存活):
  C1 把「等一个不存在的目录」也当成变异   → 存活,且**绿得对**(见上)

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量。锚点必须**括号配平且唯一**(r89 踩过:含
`args.workspace` 的两行在 CLI 里出现 4 次,只照抄那两行会指错函数)。
所有变异统一走 _write_checked,写盘前 ast.parse。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_watch_workspace_arg.py"
IMPL = REPO / "arl_lite" / "cli.py"

TARGET = [
    "tests/test_watch_workspace_arg.py::test_watch_start_writes_into_the_named_workspace",
    "tests/test_watch_workspace_arg.py::test_watch_start_without_w_still_uses_default",
]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 判据侧锚点 ──
#
# 锚点必须**括号配平**且**唯一**(r81 / r89 各踩过一次):
#  - 只抄 `proc = subprocess.Popen(\n` 是半句,判成 bug 是对的
#  - 只抄 `args.workspace` 那两行,在 cli.py 里出现 4 次,会指错函数

HELPER_POPEN = (
    "    proc = subprocess.Popen(\n"
    '        [sys.executable, "-B", "-m", "arl_lite.cli", *argv],\n'
    "        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=REPO,\n"
    '        env=dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1"),\n'
    "    )\n"
)
HELPER_NO_RUN = "    return  # 变异:根本不跑命令\n" + HELPER_POPEN

HELPER_POLL_LOOP = "        while time.monotonic() < deadline:\n"
HELPER_NO_WAIT = "        while False:  # 变异:不等目录出现就掐\n"

HELPER_DEADLINE = "        deadline = time.monotonic() + timeout\n"
HELPER_DEADLINE_ZERO = "        deadline = time.monotonic() + 0  # 变异:不等就掐\n"

# ── 实现侧锚点:连函数头一起照抄,保证唯一 ──

IMPL_HEAD = (
    'def cmd_watch_start(args) -> int:\n'
    '    """启动 watch 调度器(同步跑,Ctrl+C 退出)"""\n'
)
IMPL_R70_BUG = IMPL_HEAD + '    args.workspace = "default"  # 变异:还原 r70 的 bug,无视 -w\n'

WAIT_TEAMA = '                          home / ".arl-lite" / "workspaces" / "teamA")\n'
WAIT_TEAMA_NEVER = '                          home / ".arl-lite" / "workspaces" / "teamA_NEVER")\n'

# CLAIMS 里**只许写字面量**。这一条 r86 写过、r88 又犯了一次、r89 第三次 ——
# 三次都是「图省事写成名字引用」,三次都被 `ast.literal_eval` 当场拦下。
# 拦得住说明这条规矩有效,也说明它值得反复写进 docstring。
CLAIMS = {
    # M1/M2 都是**纯插入**(在原语句前面加一行),原语句改后仍然在 ——
    # must_not 只能留空,否则 `_verify_claim` 会报「仍在」,把变异判成
    # BAD-MUTANT。跟 r88 的 M1 同一个坑,第二次栽在同一处。
    "M1-helper根本不调起命令": (
        ["return  # 变异:根本不跑命令"],
        [],
    ),
    "M2-注入r70真bug(watch-start无视-w)": (
        ['args.workspace = "default"  # 变异:还原 r70 的 bug,无视 -w'],
        [],
    ),
    "M3-进程起来就terminate不等目录出现": (
        ["while False:  # 变异:不等目录出现就掐"],
        ["        while time.monotonic() < deadline:\n"],
    ),
    "M4-deadline提前到0不等就掐": (
        ["deadline = time.monotonic() + 0  # 变异:不等就掐"],
        ["        deadline = time.monotonic() + timeout\n"],
    ),
    "C1-等一个永远不出现的目录": (
        ['"workspaces" / "teamA_NEVER"'],
        ['"workspaces" / "teamA")'],
    ),
}

MUTANTS = [
    ("M1-helper根本不调起命令",
     lambda p: _apply(p, HELPER_POPEN, HELPER_NO_RUN), False, (CRIT,)),
    ("M2-注入r70真bug(watch-start无视-w)",
     lambda p: _apply(p, IMPL_HEAD, IMPL_R70_BUG), False, (IMPL,)),
    ("M3-进程起来就terminate不等目录出现",
     lambda p: _apply(p, HELPER_POLL_LOOP, HELPER_NO_WAIT), False, (CRIT,)),
    ("M4-deadline提前到0不等就掐",
     lambda p: _apply(p, HELPER_DEADLINE, HELPER_DEADLINE_ZERO), False, (CRIT,)),
]

COVERAGE_MUTANTS = [
    ("C1-等一个永远不出现的目录",
     lambda p: _apply(p, WAIT_TEAMA, WAIT_TEAMA_NEVER), True, (CRIT,)),
]


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    for piece in CLAIMS[name][1]:
        n = original.count(piece)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {piece!r} 在改前出现 {n} 次 —— 指不准位置")


def _verify_claim(name: str, mutated: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    missing = [s for s in must_have if s not in mutated]
    leftover = [s for s in must_not if s in mutated]
    if missing or leftover:
        raise AssertionError(
            f"变异 {name!r} 的替换没有做到它名字声称的事: 缺 {missing} 仍在 {leftover}")


def _write_checked(path: pathlib.Path, out: str) -> None:
    import ast
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=600,
    )


def _sweep(mutants) -> list:
    out = []
    for name, mutate, expect, targets in mutants:
        backups = {t: t.read_bytes() for t in targets}
        try:
            try:
                _check_claim_points_at_one_place(
                    name, "\n".join(t.read_text(encoding="utf-8") for t in targets))
                for t in targets:
                    mutate(t)
                _verify_claim(name, "\n".join(
                    t.read_text(encoding="utf-8") for t in targets))
            except AssertionError as e:
                out.append((name, "BAD-MUTANT", str(e)[:400]))
                continue
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if any(bad in outp for bad in COLLECTION_FAILED):
                out.append((name, "BAD-SYNTAX", outp[-400:]))
                continue
            killed = r.returncode != 0
            detail = "killed" if killed else "survived"
            if killed == expect:
                detail = f"UNEXPECTED-{detail}"
            out.append((name, detail, outp[-500:] if killed == expect else ""))
        finally:
            for t, data in backups.items():
                t.write_bytes(data)
    return out


def main() -> int:
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r89 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:38s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr89 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
