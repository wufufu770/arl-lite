"""arl_lite.tui.app

简易 TUI(纯 stdlib,ANSI escape code)。

屏幕:
- 主菜单
- 资产浏览(分页)
- 任务查看
- 关联分析结果
- 监控列表
- 搜索

纪律:
- 失败不能崩(用 try/except 包每个 handler)
- 无 raw mode(用 readline 替代)
- 自动 fallback 到 CLI 子命令
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import unicodedata
from typing import Callable

try:  # termios/tty 仅 Unix;Windows 上 import arl_lite.tui 不再直接炸
    import termios
    import tty
except ImportError:  # pragma: no cover
    termios = None
    tty = None

from .. import __version__

log = logging.getLogger("arl_lite.tui.app")

# TUI 起不来时给用户的替代路径。**单一来源** —— 两处报错共用它,
# 手抄两份必然漂(r60-r64 修的正是这类漂移出来的死路)。
#
# 三条实测得来的约束,每条都对应一个真跑出来的坑:
#
# 1) 必须带 `arl-lite` 前缀。判据 tests/test_cli_advice_commandable.py
#    靠字面量前缀提取建议,写裸子命令(`query`)它一条都看不见 ——
#    那正是这处死路能一路躲过 r60-r64 四轮的原因。
#
# 2) 必须能真跑。`arl-lite query` 缺必填的 table,rc=2 报
#    "the following arguments are required: table",所以这里写具体表名。
#
# 3) 必须先有工作区。这条是全量测试时才逮到的:`arl-lite stats` 在
#    **全新 HOME** 下 rc=1 报 "workspace not found: 'default'" ——
#    而「装完还没跑过任何任务」正是新用户的默认处境。
#    修法不是删掉建议(那是拿删建议掩盖能力缺失,r63 的规矩),
#    是把出路一起给出来:`workspace list` 会自动建出 default。
_NON_TUI_ALTERNATIVES = (
    "改用这些子命令(均已实测可直接运行):\n"
    "  arl-lite workspace list              # 首次使用先跑这条,会自动建出 default 工作区\n"
    "  arl-lite stats                       # 资产概览\n"
    "  arl-lite query domains               # 域名列表\n"
    "  arl-lite query ports                 # 端口列表\n"
    "  arl-lite query sites                 # 站点列表\n"
    "  arl-lite query findings              # 指纹列表\n"
    "  arl-lite query correlations          # 关联分析结果\n"
    "  arl-lite query tasks                 # 任务历史\n"
    "  arl-lite export                      # 导出现状"
)


# =========================
# ANSI 控制
# =========================

def clear_screen() -> None:
    """清屏"""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def move_to(row: int, col: int) -> None:
    sys.stdout.write(f"\033[{row};{col}H")
    sys.stdout.flush()


def _term_width() -> int:
    """终端宽度(取不到时按 100 列)"""
    try:
        return shutil.get_terminal_size(fallback=(100, 24)).columns
    except Exception:
        return 100


def _dwidth(s: str) -> int:
    """显示宽度:CJK/全角字符算 2 列(纯 len() 会让中文表格错位)"""
    w = 0
    for ch in str(s):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _clip(s: str, width: int) -> str:
    """按显示宽度截断,超出加省略号"""
    s = str(s)
    if _dwidth(s) <= width:
        return s
    if width <= 4:
        return s[:width]
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 3:
            break
        out += ch
        w += cw
    return out + "..."


def _pad(s: str, width: int) -> str:
    """按显示宽度截断+补空格(表格列对齐)"""
    s = _clip(s, width)
    return s + " " * max(0, width - _dwidth(s))


def _fit_widths(cols: list[str], rows: list, total: int) -> list[int]:
    """把各列宽度分配进 total 内;超了按自然宽度比例收缩,每列保底 6"""
    natural = []
    for c in cols:
        w = _dwidth(c)
        for r in rows:
            v = r[c] if r[c] is not None else ""
            w = max(w, _dwidth(v))
        natural.append(min(w, 40))
    seps = 3 * (len(cols) - 1)
    avail = max(len(cols) * 6, total - seps)
    if sum(natural) <= avail:
        return natural
    scaled = [max(6, int(n * avail / sum(natural))) for n in natural]
    while sum(scaled) > avail:
        i = scaled.index(max(scaled))
        if scaled[i] <= 6:
            break
        scaled[i] -= 1
    return scaled


def colorize(text: str, color: str) -> str:
    """着色:color 是 ANSI 名: red/green/yellow/blue/cyan/magenta/white/gray/bold"""
    codes = {
        "red": "31", "green": "32", "yellow": "33", "blue": "34",
        "magenta": "35", "cyan": "36", "white": "37", "gray": "90",
        "bold": "1", "reset": "0",
    }
    code = codes.get(color, "0")
    return f"\033[{code}m{text}\033[0m"


def header(title: str, subtitle: str = "") -> None:
    """打印标题头(宽度自适应终端)"""
    w = _term_width()
    bar = "=" * max(40, min(w - 2, 66))
    print(colorize(bar, "cyan"))
    print(colorize(f"  {_clip(title, w - 4)}", "bold"))
    if subtitle:
        print(colorize(f"  {_clip(subtitle, w - 4)}", "gray"))
    print(colorize(bar, "cyan"))


def info(msg: str) -> None:
    print(colorize(f"  {_clip(msg, _term_width() - 4)}", "gray"))


def success(msg: str) -> None:
    print(colorize(f"  ✓ {msg}", "green"))


def warning(msg: str) -> None:
    print(colorize(f"  ! {msg}", "yellow"))


def error(msg: str) -> None:
    print(colorize(f"  ✗ {msg}", "red"))


# =========================
# 输入辅助
# =========================

def read_key() -> str:
    """读一个键(立即返回,无需回车)"""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def read_line(prompt: str = "") -> str:
    """读一行(等回车)"""
    if prompt:
        sys.stdout.write(colorize(prompt, "cyan"))
        sys.stdout.flush()
    return input()


def read_int(prompt: str, default: int | None = None,
             min_val: int | None = None, max_val: int | None = None) -> int:
    """读整数(带校验)"""
    while True:
        raw = read_line(prompt)
        if not raw and default is not None:
            return default
        try:
            v = int(raw.strip())
            if min_val is not None and v < min_val:
                warning(f"必须 ≥ {min_val}")
                continue
            if max_val is not None and v > max_val:
                warning(f"必须 ≤ {max_val}")
                continue
            return v
        except ValueError:
            warning(f"请输入整数,得到: {raw!r}")


# =========================
# 屏幕
# =========================

class Screen:
    """一个 TUI 屏幕"""
    name: str = "Screen"
    items: list[tuple[str, Callable]] = []  # (label, handler)

    def render(self) -> None:
        clear_screen()
        header(self.name)
        for i, (label, _) in enumerate(self.items, 1):
            print(colorize(f"  [{i}] {label}", "white"))
        print()
        print(colorize("  [q] 返回主菜单 / 退出", "gray"))
        print()

    def loop(self) -> None:
        self.render()
        while True:
            # 序号+回车 选择(单字符 read_key 只能表达 1-9,10 号以后的
            # 菜单项永远选不中);q 仍即时生效
            ch = read_key()
            if ch in ("q", "Q", "\x03", "\x1b"):  # q / Ctrl+C / Esc
                return
            if ch.isdigit():
                rest = input()  # 该行剩余部分(等回车)
                idx = int(ch + rest.strip()) - 1 if (ch + rest.strip()).isdigit() else int(ch) - 1
                if 0 <= idx < len(self.items):
                    try:
                        self.items[idx][1]()
                    except Exception as e:
                        error(f"操作失败: {type(e).__name__}: {e}")
                        read_line("按回车继续...")
                    self.render()


# =========================
# 主屏幕
# =========================

class MainScreen:
    name = "arl-lite 主菜单"
    items = []

    def __init__(self, storage):
        self.storage = storage
        self.items = [
            ("📊 资产概览", self.view_stats),
            ("🌐 域名列表", self.view_domains),
            ("🔌 端口列表", self.view_ports),
            ("🌍 站点列表", self.view_sites),
            ("🔍 指纹列表", self.view_findings),
            ("⚠️  关联分析(风险)", self.view_correlations),
            ("📈 任务历史", self.view_tasks),
            ("📡 监控列表", self.view_monitors),
            ("➕ 添加监控", self.add_monitor),
            ("🔄 跑关联分析", self.run_correlations),
            ("🛠  工具检查", self.check_tools),
            ("📦 导出现状(全部资产)", self.export_all),
        ]

    def render(self) -> None:
        clear_screen()
        header(f"arl-lite v{__version__}  资产侦察控制台")
        info(f"workspace: {colorize(self.storage.workspace, 'cyan')}")
        info(f"db: {self.storage.db_path}")
        print()
        for i, (label, _) in enumerate(self.items, 1):
            print(colorize(f"  [{i:2}] {label}", "white"))
        print()
        print(colorize("  [序号+回车] 选择操作", "gray"))
        print(colorize("  [q]   退出 TUI", "gray"))
        print()

    def loop(self) -> None:
        self.render()
        while True:
            # 序号+回车(与 Screen.loop 同款,保证两位数菜单项可达)
            ch = read_key()
            if ch in ("q", "Q", "\x03", "\x1b"):
                return
            if ch.isdigit():
                rest = input()
                digits = ch + rest.strip()
                idx = int(digits) - 1 if digits.isdigit() else int(ch) - 1
                if 0 <= idx < len(self.items):
                    try:
                        self.items[idx][1]()
                    except Exception as e:
                        error(f"操作失败: {type(e).__name__}: {e}")
                        read_line("按回车继续...")
                    self.render()

    # =========================
    # 操作实现
    # =========================

    def view_stats(self) -> None:
        clear_screen()
        header("资产概览", f"workspace: {self.storage.workspace}")
        stats = self.storage.get_stats()
        for k, v in stats.items():
            label = colorize(f"  {k:20}", "cyan")
            count = colorize(str(v), "bold" if v > 0 else "gray")
            print(f"{label}: {count}")
        print()
        workspaces = self.storage.list_workspaces()
        print(colorize("  workspaces:", "cyan"))
        for w in workspaces:
            print(f"    [{w['id']}] {w['name']} (tasks={w.get('task_count', 0)})")
        print()
        read_line("按回车返回...")

    def _paginated_table(self, title: str, table: str, cols: list[str],
                         page_size: int = 20) -> None:
        """通用分页表格查看器(列宽自适应终端,中文按 2 列计)"""
        offset = 0
        while True:
            clear_screen()
            header(title, f"table: {table}")
            with self.storage._conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE workspace_id = ? "
                    f"ORDER BY id DESC LIMIT ? OFFSET ?",
                    (self.storage.workspace_id, page_size, offset)
                ).fetchall()
            if not rows:
                info("(无数据)")
                read_line("按回车返回...")
                return

            widths = _fit_widths(cols, rows, _term_width() - 2)
            header_line = "  " + " | ".join(_pad(c, w) for c, w in zip(cols, widths))
            print(colorize(header_line, "bold"))
            print(colorize("  " + "-" * min(sum(widths) + 3 * (len(widths) - 1), _term_width() - 2), "gray"))
            for r in rows:
                line = "  " + " | ".join(
                    _pad("" if r[c] is None else str(r[c]), w)
                    for c, w in zip(cols, widths)
                )
                print(line)
            print()
            info(f"显示 {offset + 1} - {offset + len(rows)}  (每页 {page_size})")
            print()
            print(colorize("  [n] 下一页  [p] 上一页  [q] 返回", "gray"))
            ch = read_key()
            if ch in ("q", "Q", "\x1b"):
                return
            if ch == "n":
                offset += page_size
            elif ch == "p":
                offset = max(0, offset - page_size)

    def view_domains(self) -> None:
        self._paginated_table("域名列表", "domains", ["domain", "source", "confidence", "first_seen"])

    def view_ports(self) -> None:
        self._paginated_table("端口列表", "ports", ["ip", "port", "state", "service"])

    def view_sites(self) -> None:
        self._paginated_table("站点列表", "sites", ["url", "status_code", "title", "tech"])

    def view_findings(self) -> None:
        self._paginated_table("指纹/发现", "findings", ["target", "title", "severity", "finding_type"])

    def view_correlations(self) -> None:
        self._paginated_table("关联分析(风险)", "correlations",
                              ["rule_name", "target", "risk", "headline"])

    def view_tasks(self) -> None:
        self._paginated_table("任务历史", "tasks", ["name", "status", "sources_ok", "sources_failed", "started_at"])

    def view_monitors(self) -> None:
        self._paginated_table("监控列表", "monitors", ["target", "monitor_type", "interval_seconds", "enabled", "last_run_at"])

    def add_monitor(self) -> None:
        clear_screen()
        header("添加监控")
        target = read_line("目标域名/IP: ").strip()
        if not target:
            warning("目标不能为空")
            read_line("按回车返回...")
            return
        mtype = read_line("类型 [full/subdomain/port/site] (默认 full): ").strip() or "full"
        try:
            interval = read_int("间隔(秒,默认 86400=24h): ", default=86400, min_val=60)
        except KeyboardInterrupt:
            return

        from arl_lite.core.monitor import Monitor
        m = Monitor(self.storage)
        mid = m.add(target, mtype, interval)
        success(f"监控 #{mid} 已创建: {target} 每 {interval}s")
        read_line("按回车返回...")

    def run_correlations(self) -> None:
        clear_screen()
        header("跑关联分析")
        info("正在跑所有 37 条规则...")
        from arl_lite.core.correlation_engine import run_all_rules, save_correlations
        import time
        start = time.time()
        hits = run_all_rules(self.storage)
        n = save_correlations(self.storage, hits)
        elapsed = time.time() - start
        print()
        success(f"{len(hits)} 命中,save {n} 条 ({elapsed:.2f}s)")
        if hits:
            print()
            print(colorize("  Top 5 高风险:", "yellow"))
            for hit in sorted(hits, key=lambda h: -h.risk)[:5]:
                line = f"    [{hit.risk}] {hit.rule_name}: {hit.headline[:60]}"
                print(colorize(line, "red" if hit.risk >= 8 else "yellow"))
        print()
        read_line("按回车返回...")

    def check_tools(self) -> None:
        clear_screen()
        header("工具检查")
        # 调子命令的逻辑
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "arl_lite", "tools", "check"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))},
        )
        print(r.stdout)
        if r.stderr:
            print(colorize(r.stderr, "red"))
        read_line("按回车返回...")

    def export_all(self) -> None:
        clear_screen()
        header("导出全部资产(JSON)")
        import json
        data = {
            "workspace": self.storage.workspace,
            "stats": self.storage.get_stats(),
            "domains": self.storage.query("domains", limit=10000),
            "ports": self.storage.query("ports", limit=10000),
            "sites": self.storage.query("sites", limit=10000),
            "findings": self.storage.query("findings", limit=10000),
            "correlations": self.storage.query("correlations", limit=10000),
        }
        path = f"arl-lite-{self.storage.workspace}-export.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            success(f"已导出: {path}")
        except OSError as e:
            error(f"导出失败: {e}")
        read_line("按回车返回...")


# =========================
# 入口
# =========================

def run_tui(workspace: str = "default") -> None:
    """启动 TUI(需要交互终端)"""
    from ..db.storage import Storage

    if termios is None or tty is None:
        print("[!] TUI 仅支持 Unix/Linux(需要 termios)。" + _NON_TUI_ALTERNATIVES,
              file=sys.stderr)
        sys.exit(2)
    if not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
        print("[!] TUI 需要交互终端(cron/管道下不可用)。" + _NON_TUI_ALTERNATIVES,
              file=sys.stderr)
        sys.exit(2)

    storage = Storage(workspace=workspace)
    main = MainScreen(storage)
    try:
        main.loop()
    except KeyboardInterrupt:
        pass
    finally:
        clear_screen()
        print("bye.")


if __name__ == "__main__":
    ws = sys.argv[1] if len(sys.argv) > 1 else "default"
    run_tui(ws)
