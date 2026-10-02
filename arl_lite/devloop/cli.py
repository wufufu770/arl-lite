"""arl_lite.devloop.cli — devloop 命令入口

挂在主 CLI 下:`arl-lite devloop <subcmd>`

    arl-lite devloop status          看当前状态(轮次/门禁/下一步)
    arl-lite devloop test            只跑门禁,不记轮次
    arl-lite devloop round           跑一整轮
    arl-lite devloop plan            只做规划,看下一步
    arl-lite devloop history         看最近几轮
    arl-lite devloop add <id> <title> 手动加待办
    arl-lite devloop gate <name>     跑单个门禁
    arl-lite devloop accept <name> --reason "..."  显式提升 baseline
    arl-lite devloop promotions      看 baseline 提升历史

设计:所有子命令都不需要预先初始化,首次调用自动建目录和文件。
循环必须自持——没有"先手动 setup 一步"这种事。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _repo_root() -> Path:
    # 从 arl_lite/devloop/cli.py 上溯三层 = 项目根
    return Path(__file__).resolve().parents[2]


def _loop():
    from .protocol import Loop
    return Loop(_repo_root())


def cmd_devloop(args) -> int:
    """devloop 总入口"""
    sub = getattr(args, "devloop_sub", None)
    if sub == "status":
        return _cmd_status(args)
    if sub == "test":
        return _cmd_test(args)
    if sub == "round":
        return _cmd_round(args)
    if sub == "plan":
        return _cmd_plan(args)
    if sub == "history":
        return _cmd_history(args)
    if sub == "add":
        return _cmd_add(args)
    if sub == "gate":
        return _cmd_gate(args)
    if sub == "accept":
        return _cmd_accept(args)
    if sub == "promotions":
        return _cmd_promotions(args)
    print("[!] unknown devloop subcommand; use --help", file=sys.stderr)
    return 2


def _cmd_status(args) -> int:
    print(_loop().status_text())
    return 0


def _cmd_test(args) -> int:
    only = None
    if getattr(args, "gates", None):
        only = [g.strip() for g in args.gates.split(",") if g.strip()]
    outcome = _loop().test_only(only=only)
    return 0 if outcome.ok else 1


def _cmd_round(args) -> int:
    only = None
    if getattr(args, "gates", None):
        only = [g.strip() for g in args.gates.split(",") if g.strip()]
    outcome = _loop().round(only_gates=only)
    print(outcome.summary())
    # 门禁失败不是 CLI 错误——协议允许 DONE_WITH_FAILURES,如实报告即可
    return 0 if outcome.ok else 3


def _cmd_plan(args) -> int:
    lp = _loop()
    state = lp.status()
    r = lp.phase_plan(state)
    print(f"{r.phase}: {r.detail}")
    return 0 if r.ok else 1


def _cmd_history(args) -> int:
    state = _loop().status()
    n = getattr(args, "n", 10)
    if not state.history:
        print("[i] no rounds recorded yet")
        return 0
    print(f"last {min(n, len(state.history))} of {len(state.history)} rounds:\n")
    for rec in state.history[-n:]:
        mark = "OK " if rec.result in ("DONE", "DONE_WITH_FAILURES") else "-- "
        print(
            f"  {mark}r{rec.round:<4} {rec.result:<22} "
            f"{rec.gates_passed}P/{rec.gates_failed}F  {rec.duration:>6.1f}s  "
            f"{rec.item_id or '(none)'}"
        )
        if rec.blocking_failures:
            print(f"        blocking: {', '.join(rec.blocking_failures)}")
    return 0


def _cmd_add(args) -> int:
    from .queue import Item, Queue
    lp = _loop()
    q = Queue(lp.dev_dir / "queue.json")
    items = q.load()
    prio = getattr(args, "priority", 1)
    item = Item(
        id=args.item_id,
        title=args.title,
        detail=getattr(args, "detail", "") or args.title,
        priority=prio,
        kind=getattr(args, "kind", "change"),
        verify=getattr(args, "verify", "") or "",
    )
    q.add(item)
    q.save(items)
    print(f"[+] queued [{['P0','P1','P2','P3'][prio] if prio < 4 else prio}] {item.id} — {item.title}")
    return 0


def _cmd_gate(args) -> int:
    from . import gates
    try:
        gate = gates.get_gate(args.gate_name)
    except KeyError as e:
        print(f"[!] {e}", file=sys.stderr)
        print(f"    available: {', '.join(gates.all_gate_names())}", file=sys.stderr)
        return 2
    r = gate.run(_repo_root())
    mark = "OK" if r.passed else ("WARN" if not r.blocking else "FAIL")
    print(f"{mark:4} {r.name}: {r.detail}")
    if r.measured is not None:
        print(f"     measured={r.measured} baseline={r.baseline}")
    return 0 if r.passed else 1


def _cmd_accept(args) -> int:
    from . import accept as _accept
    r = _accept.accept_baseline(
        _repo_root(), args.gate_name, getattr(args, "reason", "") or ""
    )
    print(r.summary())
    if r.ok:
        # 立刻复跑,让人看到门禁真的绿了,而不是"我们说它绿了"
        from . import gates
        g = gates.get_gate(args.gate_name)
        res = g.run(_repo_root())
        mark = "OK" if res.passed else "FAIL"
        print(f"  recheck  : {mark:4} {res.name}: {res.detail}")
    return 0 if r.ok else 1


def _cmd_promotions(args) -> int:
    from . import accept as _accept
    print(f"baseline promotions in {gates_path_str()}\n")
    print(_accept.format_history(_repo_root()))
    return 0


def gates_path_str() -> str:
    from . import gates
    return str(gates.baseline_path(_repo_root()))


def add_devloop_parser(sub) -> None:
    """注册 `arl-lite devloop ...` 子命令树"""
    p = sub.add_parser(
        "devloop",
        help="自持迭代协议:构建→测试→改进→规划 带门禁与退路",
    )
    dsub = p.add_subparsers(dest="devloop_sub")

    dsub.add_parser("status", help="当前轮次/门禁/下一步")

    pt = dsub.add_parser("test", help="只跑门禁,不记轮次")
    pt.add_argument("--gates", help="逗号分隔的门禁名(默认全跑)")

    pr = dsub.add_parser("round", help="跑一整轮")
    pr.add_argument("--gates", help="逗号分隔的门禁名(默认全跑)")

    dsub.add_parser("plan", help="只做规划,看下一步")

    ph = dsub.add_parser("history", help="最近几轮")
    ph.add_argument("-n", type=int, default=10, help="显示最近 N 轮")

    pa = dsub.add_parser("add", help="手动加待办")
    pa.add_argument("item_id", help="短横线命名的 id,如 add-confidence")
    pa.add_argument("title", help="一句话描述")
    pa.add_argument("--detail", default="", help="具体做什么")
    pa.add_argument("--priority", type=int, default=1, help="0=P0 1=P1 2=P2 3=P3")
    pa.add_argument("--kind", default="change", help="change|test|doc|research|refactor")
    pa.add_argument("--verify", default="", help="怎么算做完")

    pg = dsub.add_parser("gate", help="跑单个门禁")
    pg.add_argument("gate_name")

    # accept 是唯一能让红门禁变绿的命令,签名跟 gate 对齐,
    # 免得操作者记混
    pa2 = dsub.add_parser(
        "accept",
        help="门禁红了且确认可以放宽时,显式提升 baseline(必须给理由)",
    )
    pa2.add_argument("gate_name")
    pa2.add_argument(
        "--reason", required=True,
        help="为什么可以放宽。会写进 baselines.json,进版本库、进 code review。",
    )

    dsub.add_parser("promotions", help="看 baseline 提升历史")

    p.set_defaults(func=cmd_devloop)
