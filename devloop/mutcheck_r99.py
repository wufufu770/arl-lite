"""r99 变异测试:验「默认门限下通知发得出去」「失败不许说谎」在守。

## 主题

r99 实测复现:

    dataclass 默认门限: high
    hit 有 severity 吗: False
    _notify_correlations 返回: '[i] notified 0/1 correlation(s) (1 failed)'
    有 [local webhook] 吗: 没出现 = 一条都没发出去

三处错叠在一起:默认门限高于所有发送方实际发的 severity;汇总行把
「压根没发出去」说成 failed;`notify test` 走的是另一个默认值,报
「sent OK」—— 而它是用户唯一的验证手段。

## 为什么这轮用变异测试代替「回到修复前跑一遍」

r97 做过那次 stash 对照,这轮**做不成**:判据 import 了修复才引入的
`SEVERITY_ORDER`,对旧代码直接 ImportError 收集失败 —— 那证明的是
「符号不存在」,不是「判据有区分力」。

所以这里用变异实现同样的目的,而且更强:M1 就是**把 bug 原样改回去**
(`DEFAULT_MIN_SEVERITY` 从 "info" 改回 "high")。判据必须红。

## 变异清单

实现变异(期望全被杀):
  M1 `DEFAULT_MIN_SEVERITY` 改回 "high"     → r99 那个 bug 原样回来 → 红
  M2 argparse 默认改回手写字面量 "high"     → 又一个来源 → 红
  M3 `SEVERITY_RANK` 改回手写字典           → 不再由等级表推导 → 红
  M4 删掉 skipped 区分(被挡下的也算 failed) → 谎话回来 → 红
  M5 url 检查改回无条件(local 被判死)        → 红
  M6 watcher 又把 notify 的返回值丢掉        → 红

覆盖变异(期望全存活):
  C1 拆掉「任务完成通知发得出去」那条主判据
  C2 拆掉「低于门限不许说 failed」那条
  C3 拆掉「local 不许因没 url 被判死」那条
  C4 拆掉「watcher 不许丢返回值」那条

## 本轮判据自己坏过两次,都在这里记账

1. monkeypatch 打在子模块 `webhook.notify_correlation`,而
   `_notify_correlations` 是 `from .notify import ...` —— 从**包**上取名,
   补丁完全不起作用。判据测的是「补丁没生效时的行为」,看起来像实现没做
   (决策 #3)。
2. `sent = dropped = 0` 是**一个** Assign 带两个 target,判据只取
   `targets[0]`,于是在**正确的代码**上报红。
3. `parser._subparsers._group_actions[...]` 是 argparse 私有结构,
   版本一换就碎 —— 已改成读 `--help` 的公开输出。

三次都是「判据自己坏掉」,而它报出来的错看起来像被测代码坏了。
**这比判据太松更危险**:它会让人去改本来正确的实现。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
HOOK = REPO / "arl_lite" / "notify" / "webhook.py"
CLI = REPO / "arl_lite" / "cli.py"
WATCHER = REPO / "arl_lite" / "core" / "watcher.py"
CRIT = REPO / "tests" / "test_notify_default_gate_can_actually_fire.py"

TARGET = ["tests/test_notify_default_gate_can_actually_fire.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")


def _run_itself_broke(outp: str) -> bool:
    lines = _RUN_SUMMARY_RE.findall(outp)
    if not lines:
        return True
    tail = lines[-1]
    if "no tests ran" in tail:
        return True
    if _ERROR_IN_SUMMARY_RE.search(tail):
        return "AssertionError" not in outp
    return False


# ── M1:r99 的 bug 原样改回来 ──
A_DEFAULT = 'DEFAULT_MIN_SEVERITY = "info"\n'
A_DEFAULT_M = ('DEFAULT_MIN_SEVERITY = "high"  # 变异 M1:r99 那个 bug 原样回来\n'
               "SEVERITY_ORDER = (\"info\", \"low\", \"medium\", \"high\", \"critical\")\n")

A_SEV_ORDER = ('SEVERITY_ORDER = ("info", "low", "medium", "high", "critical")\n'
               "SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITY_ORDER)}\n")
A_RANK_LITERAL = ('SEVERITY_ORDER = ("info", "low", "medium", "high", "critical")\n'
                  'SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3,'
                  ' "critical": 4}  # 变异 M3:又手抄一张表\n')

# ── M2:argparse 又手写默认值 ──
# 锚点必须是**整条语句**(含结尾括号)—— 首版只照抄到 `default=...`
# 就结束了,那是半句,`test_mutcheck_anchors_are_whole_statements` 当场报红。
# 替换后括号会散,所以两侧都得是整句(r81 立的约定,本轮又犯一次)。
A_ARG_DEFAULT = ('    pnft.add_argument("--min-severity", choices=list(_SEVERITY_CHOICES),\n'
                 "                      default=_SEVERITY_CHOICES[0],\n"
                 "                      help=(f\"只发送不低于该等级的通知(默认 \"\n"
                 "                            f\"{_SEVERITY_CHOICES[0]},与 WebhookConfig 同一个来源)\"))\n")
A_ARG_M = ('    pnft.add_argument("--min-severity", choices=list(_SEVERITY_CHOICES),\n'
           '                      default="high",  # 变异 M2:又手写一个字面量\n'
           "                      help=(f\"只发送不低于该等级的通知(默认 \"\n"
           "                            f\"{_SEVERITY_CHOICES[0]},与 WebhookConfig 同一个来源)\"))\n")

# ── M4:删掉 skipped 区分 ──
A_SKIP_GUARD = ("        if not cfg.should_notify(payload.get(\"severity\", \"info\")):\n"
                "            skipped += 1\n"
                "            continue\n")
A_SKIP_GONE = ("        # 变异 M4:被门限挡下的也算 failed\n"
               "        if not cfg.should_notify(payload.get(\"severity\", \"info\")):\n"
               "            skipped = 0\n")

# ── M5:url 检查改回无条件 ──
A_URL_CHECK = ('    if not getattr(cfg, "url", "") and getattr(cfg, "provider", "")'
               ' != "local":\n')
A_URL_M = '    if not getattr(cfg, "url", ""):  # 变异 M5:local 又被判死\n'

# ── M6:watcher 又丢掉返回值 ──
A_WATCH_CALL = ("            if notify_task_done(\n"
                "                self.webhook,\n"
                "                task_id=0,  # watch 不绑定 task\n"
                "                target=wt.target,\n"
                "                found=new_total,\n"
                "                duration_seconds=duration,\n"
                "            ):\n"
                "                sent += 1\n")
A_WATCH_M = ("            notify_task_done(  # 变异 M6:又丢掉返回值\n"
             "                self.webhook,\n"
             "                task_id=0,  # watch 不绑定 task\n"
             "                target=wt.target,\n"
             "                found=new_total,\n"
             "                duration_seconds=duration,\n"
             "            )\n")

# ── 覆盖变异 ──
C1_BODY = "def test_task_done_notification_can_actually_be_sent(caplog):"
C1_END = "def test_task_done_with_errors_can_also_be_sent(caplog):"
C2_BODY = "def test_below_the_threshold_is_reported_as_skipped_not_failed(caplog):"
C2_END = "def test_a_real_send_failure_is_still_counted_as_failed(monkeypatch):"
C3_BODY = "def test_a_local_provider_config_is_not_rejected_for_having_no_url():"
C3_END = "def test_a_non_local_provider_still_requires_a_url():"
C4_BODY = "def test_watcher_does_not_throw_away_the_notify_results():"
C4_END = "def test_watcher_records_whether_notifications_went_out():"

CLAIMS = {
    "M1-默认门限改回high": (
        ['DEFAULT_MIN_SEVERITY = "high"  # 变异 M1:r99 那个 bug 原样回来'],
        ['DEFAULT_MIN_SEVERITY = "info"\n'],
    ),
    "M2-argparse又手写默认值": (
        ['                      default="high",  # 变异 M2:又手写一个字面量'],
        ["                      default=_SEVERITY_CHOICES[0],"],
    ),
    "M3-等级表又手抄": (
        ['SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3,'
         ' "critical": 4}  # 变异 M3:又手抄一张表'],
        ["SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITY_ORDER)}"],
    ),
    # `skipped += 1\n            continue\n` 在 cli.py 里出现**两次** ——
    # 另一处在 `watch start` 的 `_run()` 里(跳过非法 entry)。
    # 首版 must_not 就写了这四行,自检当场报「出现 2 次」。
    # **锚点不唯一 = 变异不精确 = 这一轮白跑。**
    "M4-被挡下的也算failed": (
        ["        # 变异 M4:被门限挡下的也算 failed"],
        ['        if not cfg.should_notify(payload.get("severity", "info")):\n'
         "            skipped += 1\n            continue\n"],
    ),
    "M5-local又被判死": (
        ['    if not getattr(cfg, "url", ""):  # 变异 M5:local 又被判死'],
        ['and getattr(cfg, "provider", "") != "local":'],
    ),
    "M6-watcher又丢返回值": (
        ["            notify_task_done(  # 变异 M6:又丢掉返回值"],
        ["            if notify_task_done("],
    ),
    "C1-拆掉任务完成能发那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆掉不许说failed那条": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆掉local免死那条": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆掉watcher不许丢那条": (["    pass  # 变异 C4:整条判据没了"], []),
}

MUTANTS = [
    ("M1-默认门限改回high", lambda p: _apply(p, A_DEFAULT, A_DEFAULT_M), False, (HOOK,)),
    ("M2-argparse又手写默认值", lambda p: _apply(p, A_ARG_DEFAULT, A_ARG_M), False, (CLI,)),
    ("M3-等级表又手抄", lambda p: _apply(p, A_SEV_ORDER, A_RANK_LITERAL), False, (HOOK,)),
    ("M4-被挡下的也算failed", lambda p: _apply(p, A_SKIP_GUARD, A_SKIP_GONE), False, (CLI,)),
    ("M5-local又被判死", lambda p: _apply(p, A_URL_CHECK, A_URL_M), False, (CLI,)),
    ("M6-watcher又丢返回值", lambda p: _apply(p, A_WATCH_CALL, A_WATCH_M), False, (WATCHER,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉任务完成能发那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆掉不许说failed那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
    ("C3-拆掉local免死那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT,)),
    ("C4-拆掉watcher不许丢那条",
     lambda p: _replace_fn(p, C4_BODY, C4_BODY + "\n    pass  # 变异 C4:整条判据没了\n",
                           C4_END), True, (CRIT,)),
]


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    if not must_have and not must_not:
        raise AssertionError(f"变异 {name!r} 的声明是空的 —— 等于没声明")
    for piece in must_not:
        n = original.count(piece)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {piece!r} 在改前出现 {n} 次 —— 指不准位置")
    for piece in must_have:
        if piece in original:
            raise AssertionError(
                f"变异 {name!r} 的 must_not/must_have 写反了:{piece!r} 改前就存在")


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
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    import ast
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(
            f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    n = src.count(old)
    if n != 1:
        raise AssertionError(
            f"变异锚点在 {path.name} 出现 {n} 次(必须恰好 1 次):{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _replace_fn(path: pathlib.Path, start: str, stub: str, end: str) -> None:
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    if src.count(end) != 1:
        raise AssertionError(f"变异结束标记没唯一命中 {path.name}:{end!r}")
    i = src.index(start)
    j = src.index(end, i)
    if j <= i:
        raise AssertionError(
            f"结束标记 {end!r} 出现在起点**之前** —— 标记写反了,不是判据的问题")
    _write_checked(path, src[:i] + stub + src[j:])


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q",
         "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
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
            except Exception as e:
                # **harness 里一个没接住的异常,和一条变异没被杀,
                # 是同一种浪费**:都让这一轮白跑(r96 栽过)。
                out.append((name, "BAD-MUTANT",
                            f"变异器自己抛了 {type(e).__name__}: {e}"[:400]))
                continue
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if _run_itself_broke(outp):
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
        print(f"\n=== r99 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:26s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr99 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
