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
    arl-lite devloop repair          修复队列里的重复 id
    arl-lite devloop unmark <id>     把误标的 done 改回 pending

多 agent 并行(三个命令构成"领活→干活→交活"的闭环):

    arl-lite devloop claim                    原子认领一条待办
    arl-lite devloop release <id>             放弃认领(挂掉/超时时)
    arl-lite devloop done-item <id>           交活,标 done

这三个必须走,不能直接改 queue.json —— 读-改-写整段不是原子的,
并发下会丢更新(实测见 devloop/lock.py 的模块 docstring)。

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


def _queue():
    """devloop 队列句柄。

    路径只在这里定义一次 —— 五个子命令都从它拿。早先每处各写一遍
    `Queue(_loop().dev_dir / "queue.json")`,改路径就得改五处,而漏掉
    那一处不会报错,只会让某个子命令读写**另一份队列**。那种 bug 极难查:
    命令返回成功,但干的事全落在没人看的地方。
    """
    from .queue import Queue
    return Queue(_loop().dev_dir / "queue.json")


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
    if sub == "repair":
        return _cmd_repair(args)
    if sub == "unmark":
        return _cmd_unmark(args)
    if sub == "drop":
        return _cmd_drop(args)
    if sub == "claim":
        return _cmd_claim(args)
    if sub == "release":
        return _cmd_release(args)
    if sub == "done-item":
        return _cmd_finish_item(args)
    print("[!] unknown devloop subcommand; use --help", file=sys.stderr)
    return 2


def _default_owner() -> str:
    """默认认领者标识:agent 名(有就用)+ pid

    pid 是必须的 —— 同一个 agent 名起两次也要能区分,
    否则 status 里分不清是谁占着。
    """
    import os
    name = os.environ.get("ARL_AGENT") or os.environ.get("AGENT_NAME") or "agent"
    return f"{name}#{os.getpid()}"


def _cmd_claim(args) -> int:
    """原子认领一条待办 —— 多 agent 并行的入口"""
    from .queue import Queue
    owner = getattr(args, "owner", "") or _default_owner()
    q = _queue()
    it = q.claim(
        owner=owner,
        item_id=getattr(args, "item_id", None),
    )
    if it is None:
        print("[i] nothing to claim (queue has no pending item)")
        return 1
    print(f"[+] claimed {it.id} — {it.title}")
    print(f"    priority={it.priority} kind={it.kind} attempts={it.attempts}")
    if it.verify:
        print(f"    verify: {it.verify}")
    return 0


def _cmd_release(args) -> int:
    """放弃认领(agent 挂掉/超时后用)"""
    q = _queue()
    # 参数名必须是 note:Queue.release 的签名是 (item_id, note="")。
    # 早先这里传的是 reason=,一调就 TypeError —— 命令看着存在,实际不可用。
    ok = q.release(args.item_id, note=getattr(args, "reason", "") or "")
    if not ok:
        print(f"[!] {args.item_id} is not claimed by anyone", file=sys.stderr)
        return 1
    print(f"[+] released {args.item_id}")
    return 0


def _cmd_finish_item(args) -> int:
    """收尾:标 done 或退回 pending

    走这个而不是直接改 queue.json,是为了拿到 owner 校验 ——
    万一两个 agent 领了同一条,后者不能覆盖前者的结果。
    """
    from .queue import Queue
    loop = _loop()
    q = Queue(loop.dev_dir / "queue.json")
    failed = bool(getattr(args, "fail", False))
    # done_round 取**当前轮次**。交活常常发生在两次 devloop round 之间
    # (agent 独立干活),写 None 等于丢掉"这件事是第几轮完成的",
    # 后面 history 里出现 done_round=null 就没法归因了。
    round_no = None if failed else loop.status().round
    ok = q.finish(
        args.item_id,
        ok=not failed,
        owner=getattr(args, "owner", "") or "",
        round_no=round_no,
    )
    if not ok:
        print(f"[!] finish failed for {args.item_id} "
              f"(条目不存在,或 owner 与认领者不符)", file=sys.stderr)
        return 1
    if failed:
        print(f"[+] {args.item_id} -> pending (退回,下轮可再领)")
    else:
        print(f"[+] {args.item_id} -> done (round {round_no})")
    return 0


def _cmd_drop(args) -> int:
    """把一条假活标成 dropped(必须给理由)"""
    q = _queue()
    ok, msg = q.drop(args.item_id, reason=getattr(args, "reason", "") or "")
    if not ok:
        print(f"[!] {msg}", file=sys.stderr)
        return 1
    print(f"[+] {msg}")
    return 0


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
    try:
        outcome = _loop().round(only_gates=only,
                                item_id=getattr(args, "item_id", None))
    except (LookupError, ValueError) as e:
        # 指了一条不存在的/不可做的待办 —— 这是**调用方**的错,不是协议的错。
        # 回 2(区别于 1/3 的门禁结果),让脚本能分辨"我指错了"和"门禁没过"。
        print(f"[!] {e}")
        return 2
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
        # 人工断言的完成要标出来,否则 history 读起来像是引擎核实过的
        src = " [op]" if rec.completion_source == "operator" and rec.item_id else ""
        print(
            f"  {mark}r{rec.round:<4} {rec.result:<22} "
            f"{rec.gates_passed}P/{rec.gates_failed}F  {rec.duration:>6.1f}s  "
            f"{rec.item_id or '(none)'}{src}"
        )
        if rec.blocking_failures:
            print(f"        blocking: {', '.join(rec.blocking_failures)}")
    return 0


def _cmd_add(args) -> int:
    from .queue import Item, Queue
    lp = _loop()
    q = Queue(lp.dev_dir / "queue.json")
    prio = getattr(args, "priority", 1)
    item = Item(
        id=args.item_id,
        title=args.title,
        detail=getattr(args, "detail", "") or args.title,
        priority=prio,
        kind=getattr(args, "kind", "change"),
        verify=getattr(args, "verify", "") or "",
    )
    # `(r23 修)` 原来这里还有 `items = q.load()` 在前、`q.save(items)` 在后。
    # 而 `Queue.add()` **自己已经读-追加-原子存盘了**,所以后面那句
    # `q.save(items)` 拿的是 add 之前读到的**旧快照**,一存就把刚写进去的
    # 条目覆盖掉了 —— 典型的 lost update。
    #
    # 症状极其恶劣:`devloop add` 打印 "[+] queued ...",退出码 0,
    # 看起来完全成功,而条目**根本没进队列**。
    # r23 实测抓到:登记的 `test-writes-real-home` 凭空消失,
    # 既不是 done 也不是 dropped。
    #
    # **登记工作的工具在静默丢弃工作。** 这比"队列空掉"严重得多。
    # 不变式 #8(读-改-写整段进临界区)本来管的就是这类事,
    # 只是这次发生在 CLI 层而不是引擎层。
    q.add(item)
    # 回读一次确认真的落盘了 —— 报告成功之前先验证,
    # 这正是本项目反复吃过亏的地方(r21 的无消费者、r22 的谎报文档)
    persisted = any(i.id == item.id for i in q.load())
    if not persisted:
        print(f"[!] add 失败:{item.id} 没能落盘到 {q.path}", file=sys.stderr)
        return 1
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


def _cmd_repair(args) -> int:
    """修复重复 id 的队列

    重复 id 会让引擎所有按 id 定位的操作全部命中第一条记录,
    循环会原地空转。历史上 seed_if_empty 真的造出过这种队列。

    --dry-run 先看会怎么改再落盘。这不是多余的功能:修复靠的是
    "保留进度最靠前的那条"这条启发式,而当两份副本**都是错的**时
    启发式必然选错 —— 实测就遇到过(引擎误标 done,副本 pending 是脏数据,
    启发式保留了 pending)。这种时候唯一的出路是拿 devloop history
    里的事实来源人工校正,所以必须先看清楚它打算改什么。
    """
    q = _queue()
    dupes = q.find_duplicates()
    if not dupes and not getattr(args, "force", False):
        print("[i] no duplicate ids; nothing to repair")
        return 0
    if dupes:
        print(f"[!] found {len(dupes)} duplicated id(s):")
        for iid, n in sorted(dupes.items()):
            print(f"    {iid} x{n}")
    if getattr(args, "dry_run", False):
        print("\n-- dry run: 以下判定需要人工核对 --")
        for iid in sorted(dupes):
            for it in q.load():
                if it.id == iid:
                    print(f"    {iid}: status={it.status} attempts={it.attempts} "
                          f"done_round={it.done_round} created_round={it.created_round} "
                          f"note={it.note!r}")
        print("\n[i] dry run,未写入任何改动")
        return 0
    removed = q.repair_duplicates()
    print(f"[+] removed {removed} duplicate record(s)")
    left = q.find_duplicates()
    print("[i] clean" if not left else f"[!] still duplicated: {left}")
    print("[i] 提醒:核对留下的状态是否与 devloop history 一致 —— "
          "重复副本本身可能是错的,启发式选不出真相")
    return 0 if not left else 1


def _cmd_unmark(args) -> int:
    """把误标的 done/dropped 改回 pending

    逻辑在 Queue.unmark 里,这里只做参数接线 —— 这样能在隔离目录里测,
    不用去动真实仓库的状态。
    """
    q = _queue()
    ok, detail = q.unmark(args.item_id, getattr(args, "reason", "") or "")
    if not ok:
        print(f"[!] {detail}", file=sys.stderr)
        return 2
    print(f"[+] {detail}"
          + (f"  (reason: {args.reason})" if getattr(args, "reason", "") else ""))
    return 0


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
    pr.add_argument("--item-id", help="本轮实际做完的是哪一条(不给则按优先级自动挑,并记为 auto)")

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

    pr2 = dsub.add_parser(
        "repair",
        help="修复队列里的重复 id(重复会让循环原地空转)",
    )
    pr2.add_argument("--force", action="store_true", help="没发现问题也走一遍修复流程")
    pr2.add_argument(
        "--dry-run", action="store_true",
        help="只看会怎么改,不落盘(两份副本都可能是错的,改之前先核对)",
    )

    pu = dsub.add_parser(
        "unmark",
        help="把误标的 done/dropped 改回 pending",
    )
    pu.add_argument("item_id")
    pu.add_argument("--reason", default="", help="为什么改回(记进 item.note)")

    pdr = dsub.add_parser(
        "drop",
        help="把一条假活标成 dropped(必须给理由)",
    )
    pdr.add_argument("item_id")
    pdr.add_argument(
        "--reason", required=True,
        help="为什么丢弃。丢弃原因记录的是'我们试过,它不成立',"
             "是这条记录里最值钱的部分",
    )

    # ── 多 agent 协作 ──
    # 三个命令合起来是"领活 → 干 → 交活"的闭环。少任何一个,
    # agent 就只能靠 load/save 直接改 queue.json —— 那在并发下会丢更新
    # (实测见 devloop/lock.py 的模块 docstring)。
    pcl = dsub.add_parser(
        "claim",
        help="原子认领一条待办(多 agent 并行的入口)",
    )
    pcl.add_argument(
        "--owner", default="",
        help="认领者标识(默认取 $ARL_AGENT#pid)。会写进 item.owner",
    )
    pcl.add_argument(
        "--item-id", default=None,
        help="指定要领哪一条;不给就按优先级自动挑",
    )

    prl = dsub.add_parser(
        "release",
        help="放弃认领(agent 挂掉/超时时用,否则那条被永远锁住)",
    )
    prl.add_argument("item_id")
    prl.add_argument("--reason", default="", help="为什么放弃(记进 item.note)")

    pfi = dsub.add_parser(
        "done-item",
        help="交活:标 done,或 --fail 退回 pending",
    )
    pfi.add_argument("item_id")
    pfi.add_argument(
        "--fail", action="store_true",
        help="这轮没做完,退回 pending 让别人(或下轮)接着干",
    )
    pfi.add_argument(
        "--owner", default="",
        help="校验用:与当前认领者不符时拒绝覆盖(留空=不校验)",
    )

    p.set_defaults(func=cmd_devloop)
