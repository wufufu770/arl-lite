"""arl_lite CLI

Phase 1 命令(纯 argparse,零外部依赖):
    arl-lite run -t example.com --modules subfinder,crtsh
    arl-lite query domains
    arl-lite query sites --filter "title like '%admin%'"
    arl-lite search sites "admin"
    arl-lite export json --workspace example.com
    arl-lite workspace list
    arl-lite workspace create foo
    arl-lite stats
    arl-lite tools check
    arl-lite diff --since 7d
    arl-lite version
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import string
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 让 from arl_lite import ... 能用
from . import __version__
from .db.storage import Storage, get_default_workspace_root
from .core.task_runner import TaskRunner
from .core.signal_handler import GracefulShutdown
# 词表只此一份:CLI 的 choices 从这里派生,不手写(见 build_parser 里
# `monitor changes` 那处的说明)。手写的那份已经过期过一次。
from .core.monitor import CHANGE_TYPES, Monitor as _Monitor

_ASSET_TYPES = tuple(_Monitor._ASSET_TABLES)
_ASSET_TYPES_TO_TABLE = dict(_Monitor._ASSET_TABLES)
from .modules.registry import discover_modules

log = logging.getLogger("arl_lite.cli")


# ============================================
# 输出辅助
# ============================================

def _use_color() -> bool:
    """只在交互终端上用 ANSI 颜色,重定向/管道不污染输出"""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _ensure_workspace_exists(name: str) -> int:
    """只读类命令的 workspace 存在性检查,不自动创建。

    Storage() 会静默自动创建 workspace——拼错 -w 的结果是"全 0 统计"
    加一个垃圾目录,用户会误判数据丢失。

    判定顺序(轻→重,避免检查本身产生副作用):
    1. 目录下有 data.db(覆盖 `run -w` 直建的库,零副作用)
    2. default 库注册表(覆盖 `workspace create` 只写表不建目录;
       仅在 default 库已存在时查——否则这个"检查"会自己创建 default)
    """
    ws_root = get_default_workspace_root()
    if (ws_root / name / "data.db").exists():
        return 0
    ref = None
    if (ws_root / "default" / "data.db").exists():
        try:
            ref = Storage(workspace="default")
            if any(w.get("name") == name for w in ref.list_workspaces()):
                return 0
        except Exception as e:
            log.debug(f"workspace existence check degraded: {e}")
            return 0  # 检查本身失败时不阻塞(降级为旧行为)
    available = set()
    if ref is not None:
        try:
            available |= {w.get("name", "") for w in ref.list_workspaces()}
        except Exception:
            pass
    if ws_root.exists():
        available |= {d.name for d in ws_root.iterdir() if d.is_dir()}
    available.discard("")
    print(f"[!] workspace not found: {name!r}", file=sys.stderr)
    if available:
        print(f"    available: {sorted(available)}", file=sys.stderr)
    return 1


def _print_table(rows: list[dict], cols: list[str] | None = None) -> None:
    """简单表格输出(无 rich 降级)"""
    if not rows:
        print("(empty)")
        return
    if cols is None:
        cols = list(rows[0].keys())
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    sep = "  "
    print(sep.join(c.ljust(widths[c]) for c in cols))
    print(sep.join("-" * widths[c] for c in cols))
    for r in rows:
        print(sep.join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def _print_json(obj: Any, file=None) -> None:
    out = file or sys.stdout
    out.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    if file is None:
        out.write("\n")


def _print_csv(rows: list[dict], file=None) -> None:
    """CSV 输出(所有表所有列)"""
    import csv as csv_mod
    if not rows:
        return
    cols = list(rows[0].keys())
    writer = csv_mod.writer(file or sys.stdout)
    writer.writerow(cols)
    for r in rows:
        writer.writerow(["" if r.get(c) is None else str(r.get(c)) for c in cols])


def _print_tsv(rows: list[dict], file=None) -> None:
    """TSV 输出"""
    if not rows:
        return
    cols = list(rows[0].keys())
    out = file or sys.stdout
    out.write("\t".join(cols) + "\n")
    for r in rows:
        out.write("\t".join(
            ("" if r.get(c) is None else str(r.get(c))).replace("\t", " ").replace("\n", " ")
            for c in cols
        ) + "\n")


def _print_markdown(data: dict, file=None) -> None:
    """Markdown 输出(每个表一个 section)"""
    out = file or sys.stdout
    for table, rows in data.items():
        out.write(f"\n## {table} ({len(rows)} rows)\n\n")
        if not rows:
            out.write("_(empty)_\n")
            continue
        cols = list(rows[0].keys())
        out.write("| " + " | ".join(cols) + " |\n")
        out.write("|" + "|".join(["---"] * len(cols)) + "|\n")
        for r in rows:
            out.write("| " + " | ".join(_md_cell(r.get(c, "")) for c in cols) + " |\n")


def _md_cell(v) -> str:
    """单元格值:None→空串,竖线/链接语法转义。

    数据(站点 title 等)来自外部,markdown 导出若保留 `[x](url)`
    语法,贴进任何 markdown 渲染器就会触发外联请求/钓鱼链接。
    """
    s = str(v if v is not None else "")
    s = s.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return s.replace("|", "\\|").replace("\n", " ")


# ============================================
# 子命令
# ============================================

def cmd_run(args) -> int:
    """跑一个扫描任务"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # 防御:target 边界
    if not args.target or not args.target.strip():
        print("[!] target must not be empty", file=sys.stderr)
        return 2
    if len(args.target) > 1000:
        print(f"[!] target too long: {len(args.target)} > 1000", file=sys.stderr)
        return 2
    if "\x00" in args.target:
        print("[!] target contains null byte", file=sys.stderr)
        return 2

    storage = Storage(workspace=args.workspace)
    runner = TaskRunner(storage=storage, workspace_id=storage.workspace_id)

    # SIGTERM 优雅停止
    shutdown = GracefulShutdown(runner)
    shutdown.install()

    # 解析 modules
    if args.modules is not None:
        modules = [m.strip() for m in args.modules.split(",") if m.strip()]
        # 空串/全逗号和拼错的模块名都提前拒绝:旧逻辑空串会静默跑默认模块
        # (意外发动 3 分钟扫描),拼错的名字只在执行期报错还可能 exit 0
        if not modules:
            print("[!] --modules parsed to empty list (check for stray commas)", file=sys.stderr)
            return 2
        available = discover_modules()
        unknown = [m for m in modules if m not in available]
        if unknown:
            print(f"[!] unknown module(s): {unknown}", file=sys.stderr)
            print(f"    available: {sorted(available.keys())}", file=sys.stderr)
            return 2
    else:
        modules = ["subfinder", "crtsh"]  # 默认

    print(f"[+] workspace: {args.workspace}")
    print(f"[+] target: {args.target}")
    print(f"[+] modules: {modules}")
    print()

    async def _go():
        return await runner.run(
            target=args.target,
            modules=modules,
            preset=args.preset,
        )

    result = asyncio.run(_go())

    print()
    print(f"[+] done in {result.duration_seconds:.1f}s")
    print(f"[+] found: {result.found}")
    if result.errors:
        print(f"[!] errors: {len(result.errors)}")
        for e in result.errors[:5]:
            print(f"    - {e}")
    # 显示每源状态(零假数据纪律)— source_status 由 TaskRunner 自动入库
    if result.sources:
        print()
        print("[i] source status:")
        for src in result.sources:
            mark = "✓" if src.ok else "✗"
            extra = f" ({src.error})" if src.error else ""
            print(f"    {mark} {src.source}: {len(src.data)} found in {src.duration:.1f}s{extra}")

    # 退出码:任一源成功即 0(部分降级 ≠ 全盘失败,cron/脚本才好判断);
    # 全部失败(或没跑成任何源)才非零
    ok_sources = sum(1 for s in result.sources if s.ok)
    return 0 if ok_sources > 0 else 1


def cmd_query(args) -> int:
    if _ensure_workspace_exists(args.workspace):
        return 1
    storage = Storage(workspace=args.workspace)
    rows = storage.query(args.table, filter_sql=args.filter, limit=args.limit)
    if args.format == "json":
        _print_json(rows)
    else:
        _print_table(rows)
    return 0


def cmd_search(args) -> int:
    if _ensure_workspace_exists(args.workspace):
        return 1
    storage = Storage(workspace=args.workspace)
    rows = storage.search(args.table, args.keyword, limit=args.limit)
    if args.format == "json":
        _print_json(rows)
    else:
        _print_table(rows)
    return 0


def cmd_export(args) -> int:
    if _ensure_workspace_exists(args.workspace):
        return 1
    storage = Storage(workspace=args.workspace)
    data: dict = {}
    for table in ["domains", "hosts", "ports", "sites", "findings", "tasks", "source_status", "correlations"]:
        data[table] = storage.query(table, limit=10000)
    fmt = getattr(args, "format", "json")
    output = getattr(args, "output", None)
    if output:
        # 用户直觉里 ~/x.html 是家目录;不展开会在 CWD 建出字面量 "~" 目录
        output = str(Path(output).expanduser())

    if fmt == "html":
        from .cli_report_html import write_html_report
        target = output or f"{args.workspace}_report.html"
        try:
            out_path = write_html_report(storage, args.workspace, target)
        except (IOError, OSError) as e:
            print(f"[!] HTML export failed: {e}", file=sys.stderr)
            return 1
        print(f"[+] HTML report written: {out_path}")
        print(f"    size: {out_path.stat().st_size} bytes")
        return 0

    # 其他格式:支持 -o 写文件(没 -o 输出到 stdout)
    if output:
        try:
            file = open(output, "w", encoding="utf-8")
        except (IOError, OSError) as e:
            print(f"[!] cannot open output file {output}: {e}", file=sys.stderr)
            return 1
    else:
        file = None  # 用 stdout

    try:
        if fmt == "json":
            _print_json(data, file=file)
        elif fmt == "csv":
            for table, rows in data.items():
                if file is None:
                    # 分隔行走 stderr:stdout 是机读数据流,混入垃圾行会污染表头
                    print(f"=== {table} ({len(rows)} rows) ===", file=sys.stderr)
                _print_csv(rows, file=file)
        elif fmt == "tsv":
            for table, rows in data.items():
                if file is None:
                    print(f"=== {table} ({len(rows)} rows) ===", file=sys.stderr)
                _print_tsv(rows, file=file)
        elif fmt == "markdown":
            _print_markdown(data, file=file)
        elif fmt == "table":
            for table, rows in data.items():
                print(f"\n=== {table} ({len(rows)} rows) ===")
                _print_table(rows)
        else:
            print(f"[!] unknown format: {fmt}", file=sys.stderr)
            return 2
    finally:
        if file is not None:
            file.close()

    if output:
        print(f"[+] {fmt} exported to {output}")
    return 0


def cmd_workspace_list(args) -> int:
    # workspace list 不依赖某个具体 workspace,显示所有
    # 但仍需要一个 Storage 实例来读 workspaces 表,用 "default" 即可
    ws = getattr(args, "workspace", None) or "default"
    storage = Storage(workspace=ws)
    rows = storage.list_workspaces()
    _print_table(rows, cols=["id", "name", "description", "ticket", "task_count", "last_active_at"])
    return 0


def cmd_workspace_delete(args) -> int:
    if not args.name or not args.name.strip():
        print("[!] workspace name must not be empty", file=sys.stderr)
        return 2
    if args.name in ("default",):
        print("[!] cannot delete built-in 'default' workspace", file=sys.stderr)
        return 2
    # 与 create/Storage 同款校验:名字会参与拼 workspace 目录路径
    if "/" in args.name or "\\" in args.name or args.name in (".", ".."):
        print(f"[!] invalid workspace name: {args.name!r}", file=sys.stderr)
        return 2
    # 用合并后的列表确认存在(list_workspaces 会并集目录扫描,
    # run 直接建的 workspace 也能被看到)
    temp_storage = Storage(workspace="default")
    exists = any(w.get("name") == args.name for w in temp_storage.list_workspaces())
    if not exists:
        print(f"[!] workspace {args.name!r} not found", file=sys.stderr)
        return 1
    if not args.yes:
        print(f"[!] deleting workspace {args.name!r} will remove its data. use -y to confirm", file=sys.stderr)
        return 2
    # 注册表每库独立:default 库(`workspace create` 写的)和该库自己
    # (`run -w` 写的)两处登记都要清,按名字删,别用别的库的 id
    try:
        temp_storage.delete_workspace_by_name(args.name)
    except Exception as e:
        print(f"[!] delete failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    try:
        Storage(workspace=args.name).delete_workspace_by_name(args.name)
    except Exception as e:
        log.debug(f"own-registry cleanup skipped: {e}")
    # 删目录
    import shutil
    ws_dir = temp_storage.workspace_root / args.name
    if ws_dir.exists():
        try:
            shutil.rmtree(ws_dir)
        except OSError as e:
            print(f"[!] warning: data dir not removed: {e}", file=sys.stderr)
    print(f"[+] workspace {args.name!r} deleted")
    return 0


def cmd_workspace_create(args) -> int:
    if not args.name or not args.name.strip():
        print("[!] workspace name must not be empty", file=sys.stderr)
        return 2
    if "/" in args.name or "\\" in args.name:
        print(f"[!] workspace name must not contain path separator: {args.name!r}", file=sys.stderr)
        return 2
    if args.name in (".", ".."):
        print(f"[!] workspace name must not be . or ..: {args.name!r}", file=sys.stderr)
        return 2
    # 用 default 临时 storage 来检查
    storage = Storage(workspace="default")
    for w in storage.list_workspaces():
        if w.get("name") == args.name:
            print(f"[!] workspace {args.name!r} already exists (id={w.get('id')})", file=sys.stderr)
            return 1
    try:
        wid = storage.create_workspace(
            name=args.name,
            description=args.description or "",
            ticket=args.ticket,
            valid_from=args.valid_from,
            valid_until=args.valid_until,
        )
    except Exception as e:
        print(f"[!] failed to create workspace: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"[+] workspace '{args.name}' created (id={wid})")
    return 0


def cmd_stats(args) -> int:
    ws = getattr(args, "workspace", None) or "default"
    if _ensure_workspace_exists(ws):
        return 1
    storage = Storage(workspace=ws)
    stats = storage.get_stats()
    _print_json(stats)
    return 0


def _parse_since(value: str) -> str:
    """`7d` / `24h` / ISO 时间 → ISO 起点串

    ## 为什么要抽成函数(r51)

    它原先内联在 `cmd_diff` 里。r51 要给 `monitor changes --since` 和
    `monitor prune --older-than` 用同一个语义,而**复制第二份**就是
    r45 那条教训的重演(同一张表两个来源迟早漂)。所以一份就够,三处共用。

    ## 「能 parse 就当时间」那种写法为什么不要

    原实现的 ISO 分支把原串原样传下去,不自己校验。实测那样**也是**安全的
    —— 下游 `diff_new_since` 会拒(`--since garbage` 返回 exit 2)。但那是
    碰巧下游记得校验:一个自己不校验的解析器,安全完全挂在「每个下游都记得」
    上。所以这里自己先过一道 `parse_ts`,拒绝不了就地报错,给的是这一条
    命令自己的话。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-empty string")
    from .core.monitor import parse_ts
    if value.endswith("d") or value.endswith("h"):
        unit = "days" if value.endswith("d") else "hours"
        n = int(value[:-1])
        if n < 0:
            raise ValueError(f"{unit} must be >= 0, got {n}")
        delta = timedelta(days=n) if unit == "days" else timedelta(hours=n)
        return (datetime.utcnow() - delta).isoformat()
    dt = parse_ts(value)
    if dt is None:
        raise ValueError(f"not a recognised window or timestamp: {value!r}")
    return dt.isoformat()


def cmd_diff(args) -> int:
    if _ensure_workspace_exists(args.workspace):
        return 1
    storage = Storage(workspace=args.workspace)
    # 解析 since:7d / 24h / ISO 时间
    try:
        since = _parse_since(args.since)
    except ValueError as e:
        print(f"[!] invalid --since '{args.since}': {e}", file=sys.stderr)
        print(f"    valid: '7d' / '24h' / '2026-09-01'", file=sys.stderr)
        return 2

    table = args.table or "domains"
    rows = storage.diff_new_since(since, table=table)
    print(f"[+] new {table} since {since}: {len(rows)}")
    _print_table(rows)
    return 0


def cmd_correlate(args) -> int:
    """跑关联分析"""
    # 边界校验(Phase 3 审计发现)
    if args.min_risk is not None and not (0 <= args.min_risk <= 10):
        print(f"[!] --min-risk must be 0..10 (got {args.min_risk})", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit <= 0:
        print(f"[!] --limit must be > 0 (got {args.limit})", file=sys.stderr)
        return 2
    from .core.correlation_engine import run_all_rules, save_correlations, load_all_rules
    from pathlib import Path
    if _ensure_workspace_exists(args.workspace):
        return 1
    storage = Storage(workspace=args.workspace)
    rules_dir = Path(__file__).parent / "modules" / "analysis" / "rules"
    rules = load_all_rules(rules_dir)
    print(f"[i] {len(rules)} rules loaded from {rules_dir.name}/")
    import time as _time
    start = _time.time()
    hits = run_all_rules(storage, rules_dir)
    elapsed = _time.time() - start
    n = save_correlations(storage, hits)
    print(f"[+] {len(hits)} hits, saved/refreshed {n} in {elapsed:.2f}s")

    # 按 risk 降序
    hits.sort(key=lambda h: -h.risk)
    # 过滤 min_risk
    hits = [h for h in hits if h.risk >= args.min_risk]
    hits = hits[:args.limit]

    if not hits:
        print("[i] no correlations found (target too clean? run 'arl-lite run' first)")
        return 0

    print()
    use_color = _use_color()
    for h in hits:
        line = f"  [{h.risk}] {h.rule_name}: {h.headline[:80]}"
        if use_color:
            color = "\033[31m" if h.risk >= 8 else ("\033[33m" if h.risk >= 5 else "\033[36m")
            print(f"{color}{line}\033[0m")
        else:
            print(line)

    # 通知。notify_correlation 此前是死代码 —— 有定义、有导出、只有测试在调,
    # 关联命中永远不外发。规则自己的 tags(rce/unauth/data_leak...)也一直被
    # 丢掉,通知里只剩一个风险等级。
    notified = _notify_correlations(args, hits)
    if notified is not None:
        print()
        print(notified)
    return 0


def _notify_correlations(args, hits) -> str | None:
    """把关联命中推给 webhook;返回给人看的汇总行,没配置则返回 None

    配置沿用项目既有约定:**由调用方注入**(Watcher 也是
    `Watcher(storage, webhook_config=...)`),不在这里凭空造一个。
    """
    if not getattr(args, "notify", False):
        return None
    cfg = getattr(args, "webhook", None)
    if cfg is None:
        # CLI 入口:从 --webhook-url 现场构造。Watcher 那边仍然是注入的,
        # 这里只是 CLI 参数的落点,不该让它去读全局配置文件。
        url = getattr(args, "webhook_url", "") or ""
        if not url:
            return "[!] --notify given but no --webhook-url provided"
        try:
            from .notify import WebhookConfig
            cfg = WebhookConfig(
                url=url,
                provider=getattr(args, "webhook_provider", "ntfy") or "ntfy",
            )
        except Exception as e:
            return f"[!] invalid webhook config: {e}"
    if not getattr(cfg, "url", ""):
        return "[!] --notify given but no webhook url"

    from .notify import notify_correlation

    ok = 0
    failed = 0
    for h in hits:
        payload = h.to_dict() if hasattr(h, "to_dict") else dict(h.__dict__)
        try:
            if notify_correlation(cfg, payload):
                ok += 1
            else:
                failed += 1
        except Exception as e:  # 单条失败不该中断整批
            log.warning("notify_correlation(%s) failed: %s",
                        getattr(h, "rule_name", "?"), e)
            failed += 1
    line = f"[i] notified {ok}/{len(hits)} correlation(s)"
    if failed:
        line += f" ({failed} failed)"
    return line


def cmd_monitor_add(args) -> int:
    from .core.monitor import Monitor
    # 边界校验
    if not args.target or not args.target.strip():
        print("[!] target must not be empty", file=sys.stderr)
        return 2
    if args.interval is not None and args.interval <= 0:
        print(f"[!] --interval must be > 0 (got {args.interval})", file=sys.stderr)
        return 2
    storage = Storage(workspace=args.workspace)
    m = Monitor(storage)
    try:
        mid = m.add(args.target, args.type, args.interval)
    except ValueError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1
    print(f"[+] monitor #{mid} added: {args.target} (type={args.type}, interval={args.interval}s)")
    return 0


def cmd_monitor_list(args) -> int:
    from .core.monitor import Monitor
    storage = Storage(workspace=args.workspace)
    monitors = Monitor(storage).list()
    if not monitors:
        print("[i] no monitors. use 'arl-lite monitor add <target>' to add one")
        return 0
    print(f"[i] {len(monitors)} monitor(s):")
    for m in monitors:
        status = "✓" if m.get("enabled") else "✗"
        # `last_change_count` 一起显示:r54 之前它被写进去了却**零消费点**
        # (全项目只在 schema 和那条 UPDATE 里出现过),所以既没人能确认它
        # 写对了,也没人能发现它写错了。r50 的教训是「只进日志不够,要落在
        # 用户下次还会看的地方」—— 这一列本来就存在,只是没人看。
        #
        # 没跑过的显示 `never` 而不是 0:那一列的 schema DEFAULT 是 0,
        # 而「从没跑过」和「跑了、这轮没变更」是两回事,都印 0 就把前者
        # 伪装成了后者 —— 用户会以为这个 target 已经监控过了。
        if m.get("last_run_at") is None:
            changes_txt = "never"
        else:
            changes_txt = str(m.get("last_change_count") or 0)
        print(f"  [{status}] #{m['id']} {m['target']:30} type={m['monitor_type']:10} "
              f"interval={m['interval_seconds']}s last_run={m.get('last_run_at') or 'never'} "
              f"changes={changes_txt}")
    return 0


def cmd_monitor_remove(args) -> int:
    from .core.monitor import Monitor
    storage = Storage(workspace=args.workspace)
    m = Monitor(storage)
    arg = str(args.id_or_target).strip()
    # 数字 → 按 id 删
    if arg.isdigit():
        mid = int(arg)
        if m.remove(mid):
            print(f"[+] monitor #{mid} removed")
            return 0
        print(f"[!] monitor #{mid} not found", file=sys.stderr)
        return 1
    # 字符串 → 按 target 删(全部匹配)
    n = m.remove_by_target(arg)
    if n > 0:
        print(f"[+] {n} monitor(s) for {arg!r} removed")
        return 0
    print(f"[!] no monitor for target {arg!r}", file=sys.stderr)
    return 1


def cmd_monitor_enable(args) -> int:
    from .core.monitor import Monitor
    storage = Storage(workspace=args.workspace)
    enabled = not args.disable
    ok = Monitor(storage).enable(args.id, enabled)
    if ok:
        print(f"[+] monitor #{args.id} {'enabled' if enabled else 'disabled'}")
        return 0
    print(f"[!] monitor #{args.id} not found", file=sys.stderr)
    return 2


# 给人看的资产标识:hash 前缀对人没有意义,而身份字段本来就在快照里。
# 认不出来就回退到 hash —— 宁可难看,也不能什么都不显示。
_ASSET_LABEL_FIELDS = {
    "domain": ("domain",),
    "host": ("host", "ip"),
    "port": ("ip", "port", "protocol"),
    "site": ("url",),
    "finding": ("cve", "title", "description"),
}

# `diff` 为 NULL 的**正常**情形:这两种变更本来就只有单边快照,没有可比的
# 另一边。写明白是为了让「本来就没有」和「该有却没存下来」在输出上分得开。
_SINGLE_SIDED = {
    "NEW_ASSET": "首次入库,只有 after 快照 —— 本来就没有 before 可比",
    "DISAPPEARED": "资产消失,只有 before 快照 —— 本来就没有 after 可比",
}


def _row_json(row: dict, key: str) -> dict:
    """把一行里的 JSON 文本列读成 dict;读不出来就返回空 dict

    读不出来**不抛** —— 展示层因为一条坏数据就整个崩掉,那是拿报表换进程。
    """
    raw = row.get(key)
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _change_label(storage, row: dict, cache: dict | None = None) -> str:
    """这一行是人看的资产标识

    ## 为什么不直接用 hash 前缀

    `68cd3922cdc45da2` 对人没有意义,而身份字段本来就在库里。

    ## 为什么**查表优先**,快照只当兜底

    字段级变更的 `before_value`/`after_value` **只装变动的那一个字段**
    (`{"ip": "2.2.2.2"}`),里面没有资产身份。只看快照的话,host 的
    标识会退化成 `ip` —— 而 ip 正是会变的那个字段,于是「标识」每次
    都跟着变,比给个稳定 hash 还误导人。所以顺序是:表 → 快照 → hash。

    代价:表里是**当前**值,不是变更当时的值。身份字段(host/domain/url)
    本身是稳定的,所以这不构成问题;真变了的话,变的是被报告的那个字段。

    查不到就往下退,三级都不抛 —— 展示层因为一条坏数据崩掉不值当。
    但「退」不等于「闷着」:行不存在是正常的(资产可能已删),静默;
    **SQL 报错**(标识列名写错 / 表没了)要 `log.warning` —— 实测过这条
    退化路径,旧代码的 `except: pass` 让它零警告地退化成 hash。
    """
    h = row.get("asset_hash", "")
    at = row.get("asset_type", "")
    key = (at, h)
    fields = _ASSET_LABEL_FIELDS.get(at, ())
    if cache is not None and key in cache:
        return cache[key]

    def _found(v) -> str:
        if cache is not None:
            cache[key] = str(v)
        return str(v)

    table = _ASSET_TYPES_TO_TABLE.get(at)
    if table and fields:
        try:
            with storage._conn() as conn:
                got = conn.execute(
                    f"SELECT {', '.join(fields)} FROM {table} WHERE hash = ?",
                    (h,)).fetchone()
        except Exception as e:      # noqa: BLE001
            # 静默降级比降级本身更坏。实测:标识列名写错 → SQL 抛
            # OperationalError → 旧的 `except: pass` 全吞 → 输出退化成
            # hash 前缀,全程零警告,用户只会以为「标识本来就长这样」。
            # 注意这和「行不存在」(`got is None`)是两回事:后者是正常
            # 的(资产可能已删),走下面的快照兜底,不算异常。
            log.warning(
                "monitor changes: 读 %s 的标识列 %s 失败(%s: %s),"
                "标识回退到快照/hash —— 列名写错或表结构变了?",
                table, ", ".join(fields), type(e).__name__, e)
            got = None
        if got:                      # None = 行不存在,正常,静默往下退
            for f in fields:
                v = got[f]
                if v not in (None, ""):
                    return _found(v)
    for src in ("after_value", "before_value"):
        payload = _row_json(row, src)
        for f in fields:
            v = payload.get(f)
            if v not in (None, ""):
                return _found(v)
    return str(h)[:16]


def _change_lines(row: dict) -> list[str]:
    """一行变更的详情,可能有多行(多字段各一行)

    ## 为什么 NULL 的 diff 要写明白,而不是显示成空

    `NEW_ASSET` / `DISAPPEARED` 只有一个快照,`diff` 是 NULL 是**正常的**。
    如果只是什么都不显示,那「本来就没有可比的」和「本该存却没存下来」
    看起来一模一样 —— 后者是 bug,前者是设计。分不开就等于看不见。
    """
    raw = row.get("diff")
    if not raw:
        ct = row.get("change_type", "")
        return ["(无字段级 diff:" + _SINGLE_SIDED.get(
            ct, "这一条本该有 diff 却没存下来 —— 这是异常") + ")"]
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return ["(diff 解析不出来,原样无法展示)"]
    if not isinstance(d, dict) or not d:
        return ["(diff 是空的)"]
    out = []
    for field, ch in d.items():
        if not isinstance(ch, dict) or "before" not in ch or "after" not in ch:
            out.append(f"{field}: (结构异常,渲染不了)")
            continue
        out.append(f"{field}: {_fmt(ch['before'])} → {_fmt(ch['after'])}")
    return out


def _fmt(v) -> str:
    """一个值怎么印给人看。None 印成 ∅ 而不是 None —— 后者像 bug"""
    if v is None:
        return "∅"
    if v == "":
        return "(空串)"
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def _split_identity(asset_type: str, value: str) -> tuple:
    """`--asset` 给的名字 → `_ASSET_IDENTITY` 要的分段身份

    ## 为什么不「看着像 hash 就当 hash」

    那是个启发式,而启发式猜错时**不报错**:一个恰好是 16 位十六进制的域名
    会被当成 hash,于是查出来是空的,用户以为「它没变过」。所以规则写成
    无歧义的两条:给了 `--type` 就一定按名字解释;没给就只接受 16 位 hash,
    不是就报错并告诉人加 `--type`。

    ## port 为什么要 rsplit

    用户的写法是 `1.1.1.1:443` 或 `web.example.com:443`,而 IPv6 是
    `2001:db8::1:443` —— 从左边切第一个冒号会把地址切烂。从**右边**切
    最后一段才是端口,这也是唯一对三种写法都成立的位置。

    ## findings 为什么直接拒绝

    它的身份是 target、finding_type、title 三段用竖线拼的,而用户心里的
    「这个 finding」是 `target`。要用户背内部格式才能过滤一个功能,那不叫
    功能 —— 宁可明说「这条不支持按名字,给裸 hash」。
    """
    if asset_type in ("domain", "host", "site"):
        return (value,)
    if asset_type == "port":
        host, sep, port = value.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError(
                f"port 的 --asset 写法是 host:port(比如 1.1.1.1:443),"
                f"给的是 {value!r}")
        return (host, port)
    raise ValueError(
        f"--type {asset_type} 不支持按名字过滤(身份是多段复合的),"
        f"请直接给 16 位资产 hash")


def _resolve_asset_hash(storage, value: str, asset_type: str | None) -> str:
    """`--asset` 的取值 → 资产 hash

    算 hash 的形状归 `db.storage._ASSET_IDENTITY` 一家,这里只负责把
    **用户写的一个字符串**拆成它要的那几段 —— 拆错了报出来,不猜。
    """
    if not asset_type:
        v = value.strip().lower()
        if len(v) == 16 and all(c in string.hexdigits for c in v):
            return v
        raise ValueError(
            f"--asset {value!r} 不带 --type 时只能是 16 位资产 hash;"
            f"要按名字过滤请加 --type(如 --type host)")
    from .db.storage import compute_asset_hash
    table = _ASSET_TYPES_TO_TABLE[asset_type]
    parts = _split_identity(asset_type, value)
    return compute_asset_hash(storage.workspace_id, table, *parts)


def cmd_monitor_changes(args) -> int:
    from .core.monitor import count_changes, list_changes
    if args.limit is not None and args.limit <= 0:
        print(f"[!] --limit must be > 0 (got {args.limit})", file=sys.stderr)
        return 2
    storage = Storage(workspace=args.workspace)
    asset_hash = None
    if getattr(args, "asset", None):
        try:
            asset_hash = _resolve_asset_hash(storage, args.asset, args.type)
        except ValueError as e:
            print(f"[!] {e}", file=sys.stderr)
            return 2
    since = None
    # `is not None` 而不是真值判断:`--since ''` 看着像「用户要求了个时间窗」,
    # 而真值判断会把它当成「没给」,于是**静默返回全部** —— 用户以为筛过了。
    # 空串该被 _parse_since 当成非法值拒掉,而不是悄悄放宽。
    if getattr(args, "since", None) is not None:
        try:
            since = _parse_since(args.since)
        except ValueError as e:
            print(f"[!] invalid --since '{args.since}': {e}", file=sys.stderr)
            print("    valid: '7d' / '24h' / '2026-10-01'", file=sys.stderr)
            return 2
    rows = list_changes(storage, asset_type=args.type,
                        change_type=args.change_type, limit=args.limit,
                        asset_hash=asset_hash, since=since)
    if getattr(args, "json", False):
        # 机器消费:payload 原样带出去,不经过给人看的那些渲染/截断。
        # 想要 diff 就自己 parse,想要标签就自己取。
        cache: dict = {}   # (asset_type, hash) -> 标识
        print(json.dumps(
            [{"change_type": r["change_type"], "asset_type": r["asset_type"],
              "asset_hash": r["asset_hash"],
              "label": _change_label(storage, r, cache),
              "detected_at": r.get("detected_at", ""),
              "before_value": _row_json(r, "before_value"),
              "after_value": _row_json(r, "after_value"),
              "diff": _row_json(r, "diff"), "detail": _change_lines(r)}
             for r in rows], ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("[i] no changes")
        return 0
    # `len(rows)` 只是**显示了多少**,不是一共有多少。r55 之前首行直接印
    # `len(rows)`,于是库里 200 条时它说「50 change(s)」—— 一个字都没提
    # 还有 150 条没显示。用户拿它当总数,尤其是配 `--since` 时,会得出
    # 「这周只有 50 个变更」的错误结论,而 50 只是 `--limit` 的默认值。
    #
    # 只有**真的可能**被截断时才多查一次 COUNT:行数没顶到 limit 时
    # 显然没截断,没必要付这次查询。`--json` 那条路在上面就 return 了 ——
    # 机器消费要 payload 原样,加一句话反而破坏可解析性。
    shown = len(rows)
    if shown < (args.limit or shown):
        print(f"[i] {shown} change(s):")
    else:
        total = count_changes(storage, asset_type=args.type,
                              change_type=args.change_type,
                              asset_hash=asset_hash, since=since)
        if total > shown:
            print(f"[i] {shown} of {total} change(s) "
                  f"(只显示了最新 {shown} 条;--limit {args.limit}。"
                  f"要全看就调大 --limit,机器消费用 --json)")
        else:
            print(f"[i] {shown} change(s):")
    cache: dict = {}
    for r in rows:
        print(f"  [{r['change_type']:18}] {r['asset_type']:10} "
              f"{_change_label(storage, r, cache):32} {r.get('detected_at', '')}")
        for line in _change_lines(r):
            print(f"      {line}")
    return 0


def cmd_monitor_prune(args) -> int:
    """删掉过期���变更记录

    ## 为什么默认不删

    删除的安全默认值是「什么都不做」。给删除命令加一个 `--dry-run` 标志,
    等于**默认就删** —— 少打一个字母就没了。所以这里是反过来的:默认只数,
    真删必须显式加 `--yes`。

    ## 为什么必须把「基线判据会变」说出来

    `is_baseline_noise` 读的就是这张表,而且读**全部历史**。删掉旧行等于
    把它的输入截短:很久以前抖过几次的资产,清理之后就不再被当成抖动。
    这大概率是好事(那本来就是记着的局限),但它意味着**同一批数据在清理
    前后会得到不同的答案** —— 「误报率实测」这类测量因此在某天悄悄失去
    可比性。所以这里明说,而不是让用户自己发现。
    """
    from .core.monitor import prune_changes
    storage = Storage(workspace=args.workspace)
    try:
        cutoff = _parse_since(args.older_than)
    except ValueError as e:
        print(f"[!] invalid --older-than '{args.older_than}': {e}",
              file=sys.stderr)
        print("    valid: '90d' / '720h' / '2026-07-01'", file=sys.stderr)
        return 2
    dry = not args.yes
    try:
        r = prune_changes(storage, cutoff, dry_run=dry)
    except ValueError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2
    verb = "会删掉" if dry else "已删掉"
    print(f"[i] {verb} {r['matched']} 条早于 {r['cutoff']} 的变更记录"
          f"(workspace={args.workspace})")
    if dry:
        print(f"[i] 这是试算,什么都没删。要真删请加 --yes")
    if r["matched"]:
        print(f"[!] 注意:这会**改变基线判据的输入**。`is_baseline_noise` 读的是"
              f"这张表的全部历史,清理掉 {r['deleted'] if not dry else r['matched']} "
              f"条之后,很久以前抖过几次的资产可能不再被当成抖动 ——"
              f"「误报率实测」这类测量在清理前后不可直接比较。")
    return 0


def cmd_tui(args) -> int:
    from .tui.app import run_tui
    run_tui(workspace=args.workspace)
    return 0


def cmd_risk_summary(args) -> int:
    from .core.risk_score import risk_summary
    storage = Storage(workspace=args.workspace)
    s = risk_summary(storage)
    print(f"[+] workspace '{args.workspace}' 风险概览")
    print(f"  total correlations: {s['total_correlations']}")
    print(f"  unique targets:     {s['unique_targets']}")
    print(f"  max risk:           {s['max_risk']}")
    print()
    print("  by level:")
    use_color = _use_color()
    for level, n in s["by_level"].items():
        if n > 0:
            line = f"    {level:10}{n}"
            if use_color:
                color = {
                    "critical": "\033[31m", "high": "\033[33m",
                    "medium": "\033[36m", "low": "\033[37m"
                }.get(level, "")
                line = f"    {color}{level:10}{n}\033[0m"
            print(line)
    return 0


def cmd_risk_top(args) -> int:
    from .core.risk_score import compute_asset_risks
    limit = args.limit_short if args.limit_short is not None else args.limit
    if limit is not None and limit <= 0:
        print(f"[!] -n/--limit must be > 0 (got {limit})", file=sys.stderr)
        return 2
    if _ensure_workspace_exists(args.workspace):
        return 1
    storage = Storage(workspace=args.workspace)
    risks = compute_asset_risks(storage)[:limit]
    if not risks:
        print("[i] no risks (run 'arl-lite run' + 'arl-lite correlate' first)")
        return 0
    print(f"[+] top {len(risks)} high-risk assets:")
    use_color = _use_color()
    for r in risks:
        line = f"  [{r.risk_score:2}/{r.risk_level:8}] {r.target:30} (rules={r.rule_count}, type={r.target_type})"
        if use_color:
            color = {
                "critical": "\033[31m", "high": "\033[33m",
                "medium": "\033[36m", "low": "\033[37m"
            }.get(r.risk_level, "")
            print(f"{color}{line}\033[0m")
        else:
            print(line)
    return 0


def cmd_tools_list(args) -> int:
    """列出所有已注册的 recon module"""
    from .modules.registry import discover_modules
    registry = discover_modules()
    by_category: dict[str, list] = {}
    for name, cls in registry.items():
        cat = getattr(cls, "category", "uncategorized")
        by_category.setdefault(cat, []).append((name, cls))
    print(f"[i] {len(registry)} modules available:")
    for cat in sorted(by_category):
        print(f"  [{cat}]")
        for name, cls in sorted(by_category[cat]):
            tools = ",".join(getattr(cls, "required_tools", []) or []) or "-"
            ver = getattr(cls, "version", "?")
            desc = (getattr(cls, "description", "") or "")[:50]
            print(f"    - {name:12} v{ver}  tools=[{tools}]  {desc}")
    return 0


def cmd_perf_bench(args) -> int:
    """跑核心流水线性能基线,产出 docs/PERF_BASELINE.md

    全程离线且不碰网络:负载是按真实 schema 形状合成的数据,
    测的是存储写入 / 规则引擎 / 置信度 / 风险打分这四段。
    测不到的部分(子域名枚举、HTTP 探测)写在报告的「局限」里。

    默认跑 3 次取中位数 —— 单次测量会被 GC 和磁盘缓存干扰,
    那种抖动画进趋势线只会误导下一个人。
    """
    from pathlib import Path as _P
    from .core import perf_bench as pb

    scale = getattr(args, "scale", "medium") or "medium"
    repeat = int(getattr(args, "repeat", 3) or 3)
    rep = pb.run_repeated(scale=scale, repeat=repeat)

    out = _P(args.out) if getattr(args, "out", "") else _P("docs/PERF_BASELINE.md")
    pb.write_report(out, rep)

    print(f"[i] scale={rep.scale}  repeat={repeat}  "
          f"{rep.total_rows} 行  {rep.total_seconds:.2f}s  "
          f"峰值 {rep.peak_kb / 1024:.1f} MB")
    print(f"[i] {rep.rules} 条规则 / {rep.hits} 个命中")
    for p in sorted(rep.phases, key=lambda x: -x.seconds)[:5]:
        rps = f"{p.rows_per_sec:,.0f} 行/s" if p.rows_per_sec else "-"
        print(f"   {p.name:36s} {p.seconds:7.3f}s  {rps}")
    print(f"[+] 报告已写入 {out}")
    return 0


def cmd_fp_bench(args) -> int:
    """离线跑规则集误报率基准,产出 docs/FP_RATE.md

    全程离线:样本是人工构造的受控数据,不需要真实目标、不需要网络。
    """
    from pathlib import Path as _P
    from .core import fp_bench as fb

    results = fb.run_bench()
    rep = fb.analyze(results)

    out = _P(args.out) if getattr(args, "out", "") else _P("docs/FP_RATE.md")
    fb.write_report(out, rep)

    print(f"[i] {len(results)} case(s) run, rules={len(rep.per_rule)}")
    print(f"[i] 误报率 {rep.fp_rate:.1%} ({rep.total_fp}/{rep.total_opportunities})"
          f"  召回 {rep.recall:.1%} ({rep.total_tp}/{rep.total_tp + rep.total_fn})")
    for r in results:
        if r.false_positives:
            print(f"  FP {r.case.name}: {', '.join(sorted(r.false_positives))}")
        if r.false_negatives:
            print(f"  FN {r.case.name}: {', '.join(sorted(r.false_negatives))}")
        if r.error:
            print(f"  !! {r.case.name}: {r.error}")
    print(f"[+] report written to {out}")
    # 崩掉的样本必须让命令失败 —— 少跑几个样本却报 0% 误报率,
    # 比误报本身更危险
    return 1 if any(r.error for r in results) else 0


def cmd_notify_test(args) -> int:
    """发测试通知"""
    from .notify import WebhookConfig, notify

    provider = args.provider
    if provider == "local" or (not args.url and provider != "local"):
        # 默认 local,如果没 url
        if not args.url and provider != "local":
            provider = "local"

    try:
        config = WebhookConfig(
            url=args.url or "",
            provider=provider,
            min_severity=args.min_severity,
            timeout=args.timeout,
        )
    except ValueError as e:
        print(f"[!] config error: {e}", file=sys.stderr)
        return 2

    print(f"[i] provider={config.provider} min_severity={config.min_severity}")
    if config.provider != "local":
        print(f"    url: {config.url}")

    ok = notify(
        config,
        title="[ARL] Test notification",
        message="This is a test message from arl-lite.",
        severity="high",
        tags=["test_tube"],
    )
    if ok:
        print("[+] sent OK")
        return 0
    print("[!] send failed (network / provider error)")
    return 1


def cmd_watch_add(args) -> int:
    """添加 watch target"""

    # 空目标不校验的话,坏 entry 会让之后的 watch start 永久瘫痪
    if not args.target or not args.target.strip():
        print("[!] watch target must not be empty", file=sys.stderr)
        return 2
    if len(args.target) > 1000:
        print(f"[!] watch target too long: {len(args.target)} > 1000", file=sys.stderr)
        return 2

    storage = Storage(workspace=args.workspace if hasattr(args, "workspace") else "default")
    # 单进程内:复用全局 watcher 状态(简化)
    state_dir = Path.home() / ".arl-lite" / "watch"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "watch.json"

    targets = []
    if state_file.exists():
        try:
            targets = json.loads(state_file.read_text())
        except Exception:
            targets = []

    modules = None
    if args.modules:
        modules = [m.strip() for m in args.modules.split(",") if m.strip()]

    entry = {
        "target": args.target,
        "modules": modules or ["dns", "whois", "subfinder", "crtsh"],
        "interval_seconds": args.interval,
        # 运行态也落盘:之前只存 3 个字段,进程重启后 watcher 不知道
        # 上次什么时候跑的,会立刻重复扫一遍,周期越短越容易打爆数据源
        "last_run": None,
        "next_run": None,
        "last_status": None,
    }
    targets = [t for t in targets if t.get("target") != args.target]
    targets.append(entry)
    state_file.write_text(
        json.dumps(targets, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[+] watch: added {args.target} (interval={args.interval}s)")
    print(f"    state: {state_file}")
    return 0


def cmd_watch_remove(args) -> int:
    """移除 watch target"""
    state_dir = Path.home() / ".arl-lite" / "watch"
    state_file = state_dir / "watch.json"
    if not state_file.exists():
        print("[!] no watch state")
        return 1
    targets = json.loads(state_file.read_text(encoding="utf-8"))
    before = len(targets)
    targets = [t for t in targets if t.get("target") != args.target]
    state_file.write_text(
        json.dumps(targets, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if len(targets) < before:
        print(f"[+] removed {args.target}")
        return 0
    print(f"[!] {args.target} not in watch list")
    return 1


def cmd_watch_list(args) -> int:
    """列出 watch targets"""
    state_dir = Path.home() / ".arl-lite" / "watch"
    state_file = state_dir / "watch.json"
    if not state_file.exists():
        print("[i] no watch targets (use `arl-lite watch add` first)")
        return 0
    targets = json.loads(state_file.read_text(encoding="utf-8"))
    if not targets:
        print("[i] watch list empty")
        return 0
    print(f"[i] {len(targets)} watch targets:")
    for t in targets:
        modules = ",".join(t.get("modules", []) or []) or "(default)"
        interval = t.get("interval_seconds", 86400)
        print(f"  - {t['target']:30} modules={modules} interval={interval}s")
        last_run = t.get("last_run")
        if last_run:
            print(f"    last_run={last_run} status={t.get('last_status') or '-'}")
        # 截断留痕要出现在用户**还会再看的地方**,不只在日志里。
        # 累计丢弃数不为 0 就说明有变更永远没进 asset_changes(r50:丢掉是
        # 永久的,因为下一轮的检测窗口起点已经越过它们的 first_seen)。
        dropped = t.get("dropped_change_count") or 0
        if dropped:
            last_dropped = t.get("last_dropped_change_count") or 0
            print(f"    [!] 累计有 {dropped} 条变更因写入上限没进 asset_changes"
                  f"(最近一轮 {last_dropped} 条,且不会补上)"
                  f" —— `monitor changes` 看到的记录是不完整的")
    return 0


def cmd_watch_start(args) -> int:
    """启动 watch 调度器(同步跑,Ctrl+C 退出)"""
    from .core.watcher import Watcher

    state_dir = Path.home() / ".arl-lite" / "watch"
    state_file = state_dir / "watch.json"
    if not state_file.exists():
        print("[!] no watch state")
        return 1
    targets_data = json.loads(state_file.read_text())
    if not targets_data:
        print("[!] no watch targets")
        return 1

    storage = Storage(workspace=getattr(args, "workspace", "default"))
    w = Watcher(storage, state_path=state_file)
    skipped = 0
    for t in targets_data:
        # 历史坏 entry(如空 target)跳过并告警,不中断整个 watch
        target = t.get("target") if isinstance(t, dict) else None
        if not target or not str(target).strip():
            log.warning(f"watch: skipping invalid state entry: {t!r}")
            skipped += 1
            continue
        try:
            wt = w.add(
                target,
                modules=t.get("modules"),
                interval_seconds=t.get("interval_seconds", 86400),
            )
            # 恢复持久化的运行态:不恢复的话 next_run 为空 → watcher 认为
            # "从没跑过" → 重启后立刻重复扫一遍,把数据源打爆
            if isinstance(t, dict):
                for key in ("last_run", "next_run", "last_count", "run_count", "new_count"):
                    val = t.get(key)
                    if val is not None:
                        setattr(wt, key, val)
        except (ValueError, TypeError) as e:
            log.warning(f"watch: skipping invalid entry {target!r}: {e}")
            skipped += 1
    if skipped:
        print(f"[!] skipped {skipped} invalid watch entry(ies) — fix with 'arl-lite watch remove <target>'", file=sys.stderr)
    if not w.list():
        print("[!] no valid watch targets", file=sys.stderr)
        return 1

    print(f"[+] watch starting with {len(w.list())} targets (Ctrl+C to stop)")
    w.start()
    try:
        # 主线程等 stop
        import threading
        stop_event = threading.Event()
        try:
            from .core.signal_handler import GracefulShutdown
            gs = GracefulShutdown(w)
            gs.install()
        except Exception:
            pass
        while w.is_running():
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        print("\n[!] Ctrl+C, stopping")
    finally:
        w.stop(timeout=5.0)
        print("[+] watch stopped")
    return 0


def cmd_watch_stop(args) -> int:
    """watch stop(同 start 一样立刻返回,因为是同步)"""
    print("[i] watch 是同步模式,直接 Ctrl+C 退出 start 即可")
    return 0


def cmd_tools_check(args) -> int:
    """检查外部工具是否安装"""
    from .integrations.subfinder import is_available, get_version
    from .integrations.crtsh import CRTSH_URL

    print("[i] external tools:")
    print(f"    subfinder: {'✓' if is_available() else '✗'} ({get_version()})")
    print(f"    crt.sh: ✓ (HTTP, no install needed, endpoint={CRTSH_URL})")

    mods = discover_modules()
    print()
    print(f"[i] discovered modules ({len(mods)}):")
    for name, cls in mods.items():
        required = ",".join(cls.required_tools or []) or "(none)"
        print(f"    - {name} [{cls.category}] requires: {required}")
    return 0


def cmd_version(args) -> int:
    print(f"arl-lite {__version__}")
    print(f"Python {sys.version.split()[0]}")
    return 0


def cmd_mcp(args) -> int:
    """启动 MCP server(stdio JSON-RPC 2.0)"""
    from .mcp.server import MCPServer
    server = MCPServer(workspace=args.workspace)
    return server.serve_forever()


# ============================================
# 入口
# ============================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arl-lite",
        description="灯塔 ARL 降级增强版:2G 内存友好的资产侦察工具",
    )
    p.add_argument("--log-level", default="INFO", help="日志级别(默认: INFO)")
    # 注:--workspace 不放全局,放每个子命令里,避免 argparse 冲突
    # 习惯上:`arl-lite run -t xxx -w foo` 这种写法要支持

    sub = p.add_subparsers(dest="command", required=True)

    # devloop — 自持迭代协议
    from .devloop.cli import add_devloop_parser
    add_devloop_parser(sub)

    # run
    pr = sub.add_parser("run", help="跑一个扫描任务")
    pr.add_argument("-t", "--target", required=True, help="目标(域名/IP/URL)")
    pr.add_argument("-m", "--modules", help="模块列表(逗号分隔,默认: subfinder,crtsh)")
    pr.add_argument("-p", "--preset", help="preset 名(Phase 2 实现)")
    pr.add_argument("-w", "--workspace", help="工作空间名(可放在 run 后面,等同于全局 --workspace)", default="default")
    pr.set_defaults(func=cmd_run)

    # query
    pq = sub.add_parser("query", help="查询资产")
    pq.add_argument("table", choices=["domains", "hosts", "ports", "sites", "findings",
                                        "tasks", "source_status", "correlations", "asset_changes"])
    pq.add_argument("-f", "--filter", help="SQL WHERE 子句(不含 WHERE 关键字)")
    pq.add_argument("-l", "--limit", type=int, default=50)
    pq.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pq.add_argument("--format", choices=["table", "json"], default="table")
    pq.set_defaults(func=cmd_query)

    # search
    ps = sub.add_parser("search", help="FTS5 全文搜索")
    ps.add_argument("table", choices=["sites", "domains", "findings"])
    ps.add_argument("keyword", help="搜索关键词")
    ps.add_argument("-l", "--limit", type=int, default=50)
    ps.add_argument("-w", "--workspace", help="工作空间名", default="default")
    ps.add_argument("--format", choices=["table", "json"], default="table")
    ps.set_defaults(func=cmd_search)

    # export
    pe = sub.add_parser("export", help="导出所有数据(json/csv/tsv/markdown/table/html)")
    pe.add_argument("--format", choices=["json", "csv", "tsv", "markdown", "table", "html"], default="json")
    pe.add_argument("-o", "--output", help="输出文件(不指定时 html 写入 CWD,其余打到 stdout)")
    pe.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pe.set_defaults(func=cmd_export)

    # workspace
    pw = sub.add_parser("workspace", help="工作空间管理")
    pw_sub = pw.add_subparsers(dest="workspace_cmd", required=True)
    pwl = pw_sub.add_parser("list", help="列出所有 workspace")
    pwl.add_argument("-w", "--workspace", help="(忽略)当前 workspace 名", default="default")
    pwl.set_defaults(func=cmd_workspace_list)

    pwc = pw_sub.add_parser("create", help="创建 workspace")
    pwc.add_argument("name", help="workspace 名")
    pwc.add_argument("-d", "--description", default="")
    pwc.add_argument("--ticket", help="授权凭证号")
    pwc.add_argument("--valid-from", help="授权开始时间(ISO)")
    pwc.add_argument("--valid-until", help="授权结束时间(ISO)")
    pwc.set_defaults(func=cmd_workspace_create)

    pwd = pw_sub.add_parser("delete", help="删除 workspace")
    pwd.add_argument("name", help="workspace 名")
    pwd.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    pwd.set_defaults(func=cmd_workspace_delete)

    # stats
    pst = sub.add_parser("stats", help="资产统计")
    pst.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pst.set_defaults(func=cmd_stats)

    # diff
    pd = sub.add_parser("diff", help="看新增资产(真 diff,基于 first_seen)")
    pd.add_argument("--since", default="7d", help="时间窗口,如 7d / 24h / 2026-09-01")
    pd.add_argument("-t", "--table", choices=["domains", "hosts", "ports", "sites", "findings"])
    pd.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pd.set_defaults(func=cmd_diff)

    # correlate(关联分析)
    pc = sub.add_parser("correlate", help="跑关联分析(37 条规则)")
    pc.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pc.add_argument("-l", "--limit", type=int, default=50, help="最多显示多少条(>0)")
    pc.add_argument("--min-risk", type=int, default=0, help="最小风险等级(0-10)")
    pc.add_argument(
        "--notify", action="store_true",
        help="把命中推给 webhook(需要 --webhook-url)",
    )
    pc.add_argument(
        "--webhook-url", default="", help="webhook 地址(配合 --notify 使用)",
    )
    pc.add_argument(
        "--webhook-provider", default="ntfy",
        help="ntfy / slack / generic / local(默认 ntfy)",
    )
    pc.set_defaults(func=cmd_correlate)

    # fp-bench(离线误报率基准)
    pfb = sub.add_parser(
        "fp-bench",
        help="离线跑规则集误报率基准,产出 docs/FP_RATE.md",
    )
    pfb.add_argument("--out", default="", help="报告输出路径(默认 docs/FP_RATE.md)")
    pfb.set_defaults(func=cmd_fp_bench)

    # perf-bench(核心流水线性能基线)
    ppb = sub.add_parser(
        "perf-bench",
        help="跑核心流水线性能基线,产出 docs/PERF_BASELINE.md",
    )
    ppb.add_argument("--out", default="", help="报告输出路径(默认 docs/PERF_BASELINE.md)")
    ppb.add_argument(
        "--scale", default="medium", choices=["small", "medium", "large"],
        help="数据规模档(默认 medium)",
    )
    ppb.add_argument(
        "--repeat", type=int, default=3,
        help="跑几次取中位数(默认 3)。单次测量会被 GC 和磁盘缓存干扰",
    )
    ppb.set_defaults(func=cmd_perf_bench)

    # monitor(资产监控)
    pm = sub.add_parser("monitor", help="资产监控管理")
    pm_sub = pm.add_subparsers(dest="monitor_cmd", required=True)

    pma = pm_sub.add_parser("add", help="添加监控")
    pma.add_argument("target", help="目标域名/IP")
    pma.add_argument("-t", "--type", default="full", choices=["full", "subdomain", "port", "site"])
    pma.add_argument("-i", "--interval", type=int, default=86400, help="间隔秒数(默认 24h)")
    pma.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pma.set_defaults(func=cmd_monitor_add)

    pml = pm_sub.add_parser("list", help="列出监控")
    pml.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pml.set_defaults(func=cmd_monitor_list)

    pmr = pm_sub.add_parser("remove", help="删除监控")
    pmr.add_argument("id_or_target", help="监控 ID(数字)或 target 域名")
    pmr.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pmr.set_defaults(func=cmd_monitor_remove)

    pme = pm_sub.add_parser("enable", help="启用/禁用监控")
    pme.add_argument("id", type=int, help="监控 ID")
    pme.add_argument("--disable", action="store_true", help="禁用(默认启用)")
    pme.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pme.set_defaults(func=cmd_monitor_enable)

    pmc = pm_sub.add_parser("changes", help="看变更事件")
    # 这两个 choices **从契约表派生**,不手写。
    #
    # 手写过,后果实测过:`--change-type ADDRESS_CHANGED` 被 argparse
    # 直接拒绝(exit 2),而错误信息只列那 6 个旧值,看起来像是这个类型
    # 根本不存在。同一张契约表当时有三份来源 —— `core/monitor.py`(真的)、
    # `schema.sql` 的注释(已被测试钉住)、这里(没人管)。
    # 同一张表有两个来源,迟早会漂;三个只是漂得更晚一点。
    # 列表**不在** help 文本里再写一遍:argparse 的 usage 行本来就从
    # `choices` 渲染出完整取值,再抄一份等于给自己造第二处会漂移的地方
    # (r45 的第一版就正好栽在这:choices 派生了,help 却写死旧列表)。
    pmc.add_argument("-t", "--type", choices=sorted(_ASSET_TYPES),
                    help="资产类型")
    pmc.add_argument("-c", "--change-type", choices=list(CHANGE_TYPES),
                    help="变更类型")
    pmc.add_argument("-l", "--limit", type=int, default=50, help="最多显示多少条")
    pmc.add_argument("--asset", help="只看这一个资产的变更。给名字要配 --type"
                                    "(如 --type host --asset web.example.com);"
                                    "不给 --type 时只接受 16 位 hash")
    pmc.add_argument("--since", help="只看这个时间点之后的变更,如 7d / 24h / 2026-10-01")
    pmc.add_argument("--json", action="store_true",
                    help="输出 JSON(payload 原样,供机器消费)")
    pmc.add_argument("-w", "--workspace", help="工作空间名", default="default")

    # 清理:默认只数不删(见 cmd_monitor_prune 的文档)
    pmp = pm_sub.add_parser(
        "prune", help="删掉过期的变更记录(默认只试算,加 --yes 才真删)")
    pmp.add_argument("--older-than", required=True,
                     help="删掉早于这个时间点的记录,如 90d / 720h / 2026-07-01")
    pmp.add_argument("--yes", action="store_true",
                     help="真的要删(不加就是试算)")
    pmp.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pmp.set_defaults(func=cmd_monitor_prune)
    pmc.set_defaults(func=cmd_monitor_changes)

    # tui(交互式终端)
    pt = sub.add_parser("tui", help="启动交互式 TUI 终端")
    pt.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pt.set_defaults(func=cmd_tui)

    # risk(风险画像)
    prk = sub.add_parser("risk", help="风险画像(基于关联分析)")
    prk_sub = prk.add_subparsers(dest="risk_cmd", required=True)

    prks = prk_sub.add_parser("summary", help="workspace 风险概览")
    prks.add_argument("-w", "--workspace", help="工作空间名", default="default")
    prks.set_defaults(func=cmd_risk_summary)

    prkt = prk_sub.add_parser("top", help="top N 高风险资产")
    prkt.add_argument("-n", "--limit", type=int, default=10, help="最多显示 N 条")
    prkt.add_argument("-l", "--limit-short", type=int, default=None, help=argparse.SUPPRESS)  # 别名兼容
    prkt.add_argument("-w", "--workspace", help="工作空间名", default="default")
    prkt.set_defaults(func=cmd_risk_top)

    # ai(AI 集成:5 边界点 + 配置)
    from .ai.commands import (
        cmd_ai_ask, cmd_ai_config_get, cmd_ai_config_list, cmd_ai_config_reset,
        cmd_ai_config_set, cmd_ai_config_test, cmd_ai_explain, cmd_ai_fix,
        cmd_ai_report, cmd_ai_suggest,
    )
    pa = sub.add_parser("ai", help="AI 集成(自然语言/报告/解释/建议/修复)")
    pa_sub = pa.add_subparsers(dest="ai_cmd", required=True)

    # ai ask
    paa = pa_sub.add_parser("ask", help="自然语言查询资产/风险")
    paa.add_argument("question", help="要问的问题(中文)")
    paa.add_argument("-w", "--workspace", help="工作空间名", default="default")
    paa.add_argument("--provider", help="强制 provider (openai/anthropic/google/ollama)")
    paa.set_defaults(func=cmd_ai_ask)

    # ai report
    par = pa_sub.add_parser("report", help="生成 workspace 报告")
    par.add_argument("-w", "--workspace", help="工作空间名", default="default")
    par.add_argument("--provider", help="强制 provider")
    par.set_defaults(func=cmd_ai_report)

    # ai explain
    pae = pa_sub.add_parser("explain", help="解释一条关联分析")
    pae.add_argument("corr_id", help="关联 ID(数字)")
    pae.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pae.add_argument("--provider", help="强制 provider")
    pae.set_defaults(func=cmd_ai_explain)

    # ai suggest
    pasg = pa_sub.add_parser("suggest", help="基于当前数据,建议下一步扫描")
    pasg.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pasg.add_argument("--provider", help="强制 provider")
    pasg.set_defaults(func=cmd_ai_suggest)

    # ai fix
    paf = pa_sub.add_parser("fix", help="给一条 finding 修复建议")
    paf.add_argument("finding_id", help="finding ID(数字)")
    paf.add_argument("-w", "--workspace", help="工作空间名", default="default")
    paf.add_argument("--provider", help="强制 provider")
    paf.set_defaults(func=cmd_ai_fix)

    # ai config
    pac = pa_sub.add_parser("config", help="AI 配置管理")
    pac_sub = pac.add_subparsers(dest="ai_config_cmd", required=True)

    pacs = pac_sub.add_parser("set", help="设置 provider 配置")
    pacs.add_argument("provider", help="provider 名 (openai/anthropic/google/ollama)")
    pacs.add_argument("--api-key", help="API key")
    pacs.add_argument("--base-url", help="API base URL")
    pacs.add_argument("--model", help="模型名")
    pacs.add_argument("--temperature", type=float, help="temperature (0-2)")
    pacs.add_argument("--max-tokens", type=int, help="max tokens")
    pacs.set_defaults(func=cmd_ai_config_set)

    pacg = pac_sub.add_parser("get", help="查看 provider 配置(api key 掩码)")
    pacg.add_argument("provider", nargs="?", help="provider 名(留空看全部)")
    pacg.set_defaults(func=cmd_ai_config_get)

    pacl = pac_sub.add_parser("list", help="列出所有 provider + 当前活动")
    pacl.set_defaults(func=cmd_ai_config_list)

    pacr = pac_sub.add_parser("reset", help="重置 provider 配置")
    pacr.add_argument("provider", nargs="?", help="provider 名(留空重置全部)")
    pacr.set_defaults(func=cmd_ai_config_reset)

    pact = pac_sub.add_parser("test", help="测试当前 provider 是否可用")
    pact.add_argument("--provider", help="强制 provider")
    pact.set_defaults(func=cmd_ai_config_test)

    # tools
    ptk = sub.add_parser("tools", help="工具管理")
    ptk_sub = ptk.add_subparsers(dest="tools_cmd", required=True)
    ptkc = ptk_sub.add_parser("check", help="检查工具可用性")
    ptkc.set_defaults(func=cmd_tools_check)
    ptkl = ptk_sub.add_parser("list", help="列出所有可用 module")
    ptkl.set_defaults(func=cmd_tools_list)

    # notify (v0.7)
    pnf = sub.add_parser("notify", help="Webhook 通知测试 / 配置")
    pnf_sub = pnf.add_subparsers(dest="notify_cmd", required=True)
    pnft = pnf_sub.add_parser("test", help="发测试通知")
    pnft.add_argument("--url", help="webhook URL")
    pnft.add_argument("--provider", default="generic", choices=["generic", "ntfy", "slack", "local"])
    pnft.add_argument("--min-severity", default="info", choices=["info", "low", "medium", "high", "critical"])
    pnft.add_argument("--timeout", type=int, default=10)
    pnft.set_defaults(func=cmd_notify_test)

    # watch (v0.7)
    pwa = sub.add_parser("watch", help="持续监控 / Watch 模式")
    pwa_sub = pwa.add_subparsers(dest="watch_cmd", required=True)
    pwaa = pwa_sub.add_parser("add", help="添加 watch target")
    pwaa.add_argument("target", help="目标域名/IP")
    pwaa.add_argument("-m", "--modules", help="逗号分隔 module 列表")
    pwaa.add_argument("--interval", type=int, default=86400, help="重跑间隔秒(默认 86400=24h)")
    pwaa.set_defaults(func=cmd_watch_add)
    pwar = pwa_sub.add_parser("remove", help="移除 watch target")
    pwar.add_argument("target", help="目标")
    pwar.set_defaults(func=cmd_watch_remove)
    pwal = pwa_sub.add_parser("list", help="列出 watch targets")
    pwal.set_defaults(func=cmd_watch_list)
    pwas = pwa_sub.add_parser("start", help="启动 watch 调度器(前台运行,Ctrl+C 停止)")
    pwas.set_defaults(func=cmd_watch_start)
    pwap = pwa_sub.add_parser("stop", help="停止 watch 调度器")
    pwap.set_defaults(func=cmd_watch_stop)

    # version
    pv = sub.add_parser("version", help="版本")
    pv.set_defaults(func=cmd_version)

    # mcp(启动 MCP server)
    pm = sub.add_parser("mcp", help="启动 MCP server(stdio JSON-RPC 2.0)")
    pm.add_argument("-w", "--workspace", help="工作空间名", default="default")
    pm.set_defaults(func=cmd_mcp)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 日志级别
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[!] interrupted", file=sys.stderr)
        return 130  # 标准的 SIGINT exit code
    except ValueError as e:
        # 用户输入错误(白名单/SQL 注入等)— 干净报错
        print(f"[!] error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        # 未预期错误 — log + 非零退出
        log = logging.getLogger("arl_lite.cli")
        log.exception(f"unexpected error: {e}")
        print(f"[!] unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
