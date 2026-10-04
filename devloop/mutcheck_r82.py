"""r82 变异测试:验「收尾必须能看出认领完忘了 done-item」在守。

主题:r77 我认领了 `devloop-unmark-drop-rc-inconsistent`、干完了、忘了跑
`done-item`,那条卡在 in_progress 两轮零信号。r79 又领一条变成 2 条,
`test_real_repo_queue_has_at_most_one_in_progress` 才报红。

r82 实测发现真正缺的不是检测,而是**区分**:
  - `devloop status` 早就会点名 in_progress 条目和年龄(`claimed` 段)
  - 但它写的是「N 条正在**别人**做」—— 单人 CLI 工作流里,我自己的在途
    条目跟上一轮的残留读起来一模一样。两件事读起来一样就等于没报。
  - 补的判据只用可判定的事实:条目 `claimed_at` < 最后一轮 `finished_at`
    ⟺ 有一轮是在它还开着的时候结束的

## 差点做出一个恒红的守卫(这轮最有价值的一条)

第一版想拿「owner 进程已退出」当告警条件。实测:

    in_progress: done-item-must-not-be-forgettable
      owner       = 'agent#271111'
      owner_alive = False
      is_stale_claim = (True, 'owner agent#271111 的进程已退出')

**我这条正在合法推进的条目,当场被判成陈旧。** `devloop claim` 是从
一次性的短命进程发的,命令跑完进程立刻退出,owner 里的 pid 永远是死的
—— `is_stale_claim` 对每一条 CLI 认领都返回「该复位」。拿它当门禁会
100% 常红,那不是守卫是个摆设。

所以判据只报跨轮事实,pid 只当**旁证**写进提示语,并且明确区分
「确认已退出」「仍在运行」「探测不到」三种。M3 专门验这条:
把判据换回 pid 判定,反例控制组必须立刻报红。

## 这轮自己栽的一次

第一版 `status_text` 里写了 `Queue.owner_alive(...)`,而 `Queue` 在那个
作用域没导入 —— 恰好在**检测到残留时**抛 NameError,把 `devloop status`
弄崩:特性唯一要帮忙的命令被特性自己弄崩,且只在它真该说话的时候崩。
`test_status_survives_a_leftover_claim` 钉的就是这条。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
所有变异统一走 _write_checked,写盘前 ast.parse。

全部变异:
  M1 跨轮判据放宽成恒真(谁都在途)      → 杀。反例控制组必须先红
  M2 收尾只报计数不点名                 → 杀
  M3 判据换回「owner 进程已退出」        → **杀**。这就是那版恒红实现:
                                       本轮在途的活也会被误报
  M4 收尾命令在报残留时崩掉(NameError) → 杀
  M5 遗留措辞弱化成泛泛的「请确认」      → 杀(措辞变弱就是守不住)
  M6 owner 存活/已退出合并成一种措辞    → 杀(信息变粗也是变弱)
  C1 判据的「必须点名是遗留」放宽      → 期望存活
  C2 判据的「不许把在途误报成遗留」放宽 → 期望存活

写 M3 时我又栽了一次「假变异」:第一版名字叫「换回 owner 进程已退出」,
实现却是个不存在的属性 `owner_alive_dead_flag_placeholder` —— 那只测到
「会崩」,测不到「会误报」。名字必须等于语义,否则变异存活时我永远只能
靠猜(这已经是第五次了)。"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

IMPL = REPO / "arl_lite" / "devloop" / "protocol.py"
CRIT = REPO / "tests" / "test_status_names_leftover_claims.py"

TARGET = ["tests/test_status_names_leftover_claims.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 实现侧锚点(整块照抄,含结尾括号) ──
# 整个 why 块(protocol.py 170-180 行)—— r81 立的规矩:锚点必须
# 整体照抄完整语句,只锚首行会留下孤儿行(这轮 M1 就栽在这)
JUDGE_WHY_BLOCK = (
    '                why = ""\n'
    '                if by_id and by_id.claimed_at and last_finished \\\n'
    '                        and by_id.claimed_at < last_finished:\n'
    '                    alive = self.queue_mod.Queue.owner_alive(owner)\n'
    '                    if alive is False:\n'
    '                        why = ("  ← 上一轮结束时它还开着,且 owner 进程已退出:"\n'
    '                               "多半是认领完忘了 done-item")\n'
    '                    elif alive is True:\n'
    '                        why = "  ← 上一轮结束时它还开着(owner 仍在运行,确认下是不是忘了收尾)"\n'
    '                    else:\n'
    '                        why = "  ← 上一轮结束时它还开着(owner 探测不到,确认下是不是忘了收尾)"\n'
)

JUDGE_CROSS_ROUND = (
    '                why = ""\n'
    "                if by_id and by_id.claimed_at and last_finished \\\n"
    "                        and by_id.claimed_at < last_finished:"
)
JUDGE_NAME_IT = (
    '                lines.append(f"                 {iid} ← {owner or \'(无主)\'}{age}{why}")'
)
JUDGE_ALIVE_CALL = "                    alive = self.queue_mod.Queue.owner_alive(owner)"
JUDGE_DEAD_WORD = (
    '                        why = ("  ← 上一轮结束时它还开着,且 owner 进程已退出:"\n'
    '                               "多半是认领完忘了 done-item")'
)


def _m4_status_crashes(path: pathlib.Path) -> None:
    """把 owner 存活探测改成一个不存在的名字 —— 复现 r82 第一版的 NameError

    它只在**检测到残留**时才被执行,所以平时跑不出来,
    只有真该说话的时候才炸:特性唯一要帮忙的那个命令被特性自己弄崩。
    判据里 `test_status_survives_a_leftover_claim` 钉的就是这一条。
    """
    _apply(path, JUDGE_ALIVE_CALL,
           "                    alive = Queue.owner_alive(owner)  # 变异:作用域里没有 Queue")


def _m3_back_to_pid_deadlock(path: pathlib.Path) -> None:
    """把判据换回「owner 进程已退出」—— r82 第一版的那个恒红实现

    这次是真的按名字实现:只看 owner 的 pid 活不活,不看跨不跨轮。
    于是一条**本轮刚领、正在做**的活(owner 是发命令的短命进程,已退出)
    也会被判成「忘了 done-item」—— 正是实测里那 100% 常红的形态。
    """
    pid_verdict = (
        "                why = \"\"\n"
        "                if by_id and by_id.owner:\n"
        "                    alive = self.queue_mod.Queue.owner_alive(by_id.owner)\n"
        "                    if alive is False:\n"
        '                        why = "  ← owner 进程已退出:多半是认领完忘了 done-item"\n'
    )
    _apply(path, JUDGE_WHY_BLOCK, pid_verdict)


MUTANTS = [
    ("M1-跨轮判据放宽成恒真", lambda p: _apply(
        p, JUDGE_WHY_BLOCK, "                why = \"\"\n"), False, (IMPL,)),
    ("M2-收尾只报计数不点名", lambda p: _apply(
        p, JUDGE_NAME_IT, "                pass  # 变异:不点名了"), False, (IMPL,)),
    ("M3-判据换回owner进程已退出(恒红那版)", _m3_back_to_pid_deadlock, False, (IMPL,)),
    ("M4-报残留时抛NameError", _m4_status_crashes, False, (IMPL,)),
    ("M5-遗留措辞弱化成泛泛的请确认", lambda p: _apply(
        p, JUDGE_DEAD_WORD,
        '                        why = "  ← 上一轮结束时它还开着,请确认一下"'),
     False, (IMPL,)),
    ("M6-owner存活与已退出合并成一种措辞", lambda p: _apply(
        p, JUDGE_DEAD_WORD,
        '                        why = "  ← 上一轮结束时它还开着,确认下是不是忘了收尾"'),
     False, (IMPL,)),
]

# ---- 覆盖变异:把判据自己改坏,期望**存活**(真实现本来就是对的) ----
JUDGE_CRIT_MUST_HINT = (
    '    assert LEFTOVER_HINT in out, (\n'
    '        f"收尾输出没说明这是「忘了 done-item」的形态:\\n{out}")'
)
JUDGE_CRIT_NO_FALSE_ALARM = (
    "    assert LEFTOVER_HINT not in out, (\n"
    '        f"把本轮在途的活误报成遗留了:\\n{out}")'
)

COVERAGE_MUTANTS = [
    ("C1-判据的「必须点名是遗留」放宽", lambda p: _apply(
        p, JUDGE_CRIT_MUST_HINT,
        '    assert "x-1" in out  # 变异:只验点名,不再验措辞'), True, (CRIT,)),
    ("C2-判据的「不许误报在途」放宽", lambda p: _apply(
        p, JUDGE_CRIT_NO_FALSE_ALARM,
        '    assert True  # 变异:反例控制组被拆掉'), True, (CRIT,)),
]


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
        capture_output=True, text=True, cwd=REPO, timeout=1200,
    )


def _sweep(mutants) -> list:
    out = []
    for name, mutate, expect, targets in mutants:
        backups = {t: t.read_bytes() for t in targets}
        try:
            for t in targets:
                mutate(t)
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if any(bad in outp for bad in COLLECTION_FAILED):
                out.append((name, "BAD-SYNTAX", outp[-400:]))
                continue
            killed = r.returncode != 0
            detail = "killed" if killed else "survived"
            if killed == expect:
                detail = f"UNEXPECTED-{detail}"
            out.append((name, detail, outp[-400:] if killed == expect else ""))
        finally:
            for t, data in backups.items():
                t.write_bytes(data)
    return out


def main() -> int:
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r82 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:36s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:400])
    print(f"\nr82 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
