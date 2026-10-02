"""arl_lite.devloop.gates — 自持迭代协议的质量闸门

7 道闸门覆盖 arl-lite 项目最重要的退化防线:

1. **no_thirdparty_import** — 零 pip 依赖铁律的强制执行
2. **test_baseline** — 测试失败数不许悄悄上涨
3. **no_import_cycle** — 内部模块不许形成循环依赖
4. **loc_budget** — 代码不许无限膨胀,协议自身也不能
5. **rules_have_advice** — 安全规则的 advice/risk/name 字段不许漏
6. **prompt_injection_guard** — AI prompt 的净化防线不许回退
7. **doc_freshness** — 文档不许引用实际未使用的依赖

设计原则:
- 零第三方依赖,纯 stdlib(项目铁律)
- GateResult frozen=True,易于哈希/比较
- baselines.json 用 tmp + os.replace 原子写
- 每个 gate 独立可测、独立可跑

调用约定(protocol.py 会这样用):
    gates = [get_gate(n) for n in all_gate_names()]
    results = run_all(repo_path)
    for r in results:
        if not r.passed and r.blocking:
            ...降级,而不是掩盖...

CLI 入口(便于人肉检查):
    python3 -m arl_lite.devloop.gates                  # 全跑
    python3 -m arl_lite.devloop.gates no_thirdparty_import  # 跑单个
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


# =====================================================================
# 契约:protocol.py 会引用这两个符号,签名不能改
# =====================================================================


@dataclass(frozen=True)
class GateResult:
    """单个门禁的判定结果

    Attributes:
        name: 门禁名(如 "no_thirdparty_import")
        passed: True=通过, False=失败
        measured: 实测数字/事实(如违规数、失败测试数、行数)
        baseline: 本轮对应的门限值(从 baselines.json 读)
        blocking: True=失败会拦下本轮迭代, False=仅警告
        detail: 给人看的结果摘要(可含文件路径/行号)
    """

    name: str
    passed: bool
    detail: str
    measured: Any = None
    baseline: Any = None
    blocking: bool = True


class Gate(Protocol):
    """所有 gate 必须实现的协议"""

    name: str
    blocking: bool

    # 是否允许用 `devloop accept` 提升它的 baseline。
    # 默认 False——"能不能被显式放宽"必须由 gate 自己逐个点头,
    # 不能默认全开。恒为 0 的红线(no_thirdparty_import / no_import_cycle)
    # 永远不该被提升:把"当前有 5 处三方 import"写成新基准,
    # 等于把 bug 追认为正常状态。
    promotable: bool = False
    # 允许被提升的字段名。dict 型 baseline 用;标量 gate 留空即可。
    promotable_fields: tuple = ()

    def run(self, repo: Path) -> GateResult:
        """执行检查,返回 GateResult

        Args:
            repo: 项目根目录(包含 arl_lite/、tests/、docs/ 等)
        """
        ...


# =====================================================================
# Baseline 持久化
# =====================================================================
#
# baselines.json 形如:
# {
#   "test_baseline": {"failed": 9, "passed": 45},
#   "loc_budget":    {"total_loc": 13455, "devloop_loc": 1505}
# }
#
# 首次运行若文件不存在,load_baseline 返回 {}。
# save_baseline 用 os.replace 保证原子性(写 tmp → rename)。

_DEFAULT_LOC_TOLERANCE = 300  # 单轮允许的代码增长(行)
# 协议自身膨胀红线。devloop 是给项目用的工具,不是项目本身——
# 它一旦比被它守护的代码涨得还快,就本末倒置了。
# 当前 devloop 约 2700 行(引擎+门禁+队列+状态),所以 3200 是个
# "还能再加一点,但别失控"的量程。
_DEFAULT_DEVELOOP_LOC_LIMIT = 3200  # 协议自身膨胀红线


def baseline_path(repo: Path) -> Path:
    """baselines.json 的固定位置

    放在 devloop/ 下,跟代码一起进版本控制——baseline 必须可追溯,
    否则"防回归"就变成了"谁跑谁改"。

    刻意不放 arl_lite/devloop/(包目录):包目录是源码,会跟着安装走,
    而 baseline 是**每个仓库一份的运行时状态**,不是库的一部分。
    """
    return Path(repo) / "devloop" / "baselines.json"


def load_baseline(repo: Path) -> dict:
    """读取 baseline;文件不存在则返回 {}"""
    p = baseline_path(repo)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # baseline 损坏不应让门禁崩——当成空基线
        return {}


def save_baseline(repo: Path, data: dict) -> None:
    """原子写 baseline:tmp + os.replace

    os.replace 在 POSIX 是原子的,Windows 上也是;
    崩在写 tmp 阶段不会污染原文件。
    """
    p = baseline_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, p)


def update_baseline(repo: Path, gate: str, measured: Any) -> None:
    """更新某个 gate 的 baseline

    measured 直接覆盖——例如 test_baseline 关心 failed 数,
    caller 自己构造 {"failed": 9, "passed": 45} 再传进来。
    """
    data = load_baseline(repo)
    data[gate] = measured
    save_baseline(repo, data)


# =====================================================================
# 内部工具
# =====================================================================


def _py_files(root: Path) -> list[Path]:
    """列举 root 下所有 .py,自动排除 __pycache__ 和 tests/"""
    out = []
    for p in root.rglob("*.py"):
        parts = set(p.parts)
        if "__pycache__" in parts:
            continue
        if "tests" in parts:  # 防御性:tests/ 可能被复制进 arl_lite/
            continue
        out.append(p)
    return out


def _count_lines(files: list[Path]) -> int:
    """总行数(物理行,含空行/注释;LOC 预算关注规模而非有效行)"""
    total = 0
    for f in files:
        try:
            total += sum(1 for _ in f.open(encoding="utf-8"))
        except OSError:
            continue
    return total


def _has_pytest_timeout() -> bool:
    """探测 pytest-timeout 是否可用——PEP 668 环境不能 pip install

    用独立子进程跑 import 探测,失败不影响主流程。
    """
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import pytest_timeout"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# =====================================================================
# Gate 1: no_thirdparty_import
# =====================================================================
#
# 防的退化:有人手贱 `import httpx` / `import yaml`,直接破坏零依赖承诺。
# stdlib 集合用 sys.stdlib_module_names(Python 3.10+);arl_lite 内部白名单。
#
# 防御深度:
# - AST 而非正则:避免被 """import foo""" 字符串字面量误判
# - 顶层模块名:`from foo.bar import baz` 只看 foo 是否在白名单外
# - 相对导入 (level > 0) 永远放行——那一定指向 arl_lite 内部


class NoThirdPartyImportGate:
    """防的退化:任何 pip 依赖偷偷溜进核心代码。

    用 AST 扫描 arl_lite/**/*.py,所有 import 顶层模块名不在
    sys.stdlib_module_names 且不在白名单 {arl_lite} 的,一律算违规。
    """

    name = "no_thirdparty_import"
    blocking = True
    _stdlib: frozenset[str] = frozenset(sys.stdlib_module_names)
    _allowed: frozenset[str] = frozenset({"arl_lite"})

    def run(self, repo: Path) -> GateResult:
        root = repo / "arl_lite"
        if not root.exists():
            return GateResult(
                name=self.name,
                passed=False,
                detail=f"arl_lite/ not found at {root}",
                measured=0,
                blocking=self.blocking,
            )

        violations: list[tuple[str, int, str]] = []
        for py in _py_files(root):
            try:
                src = py.read_text(encoding="utf-8")
                tree = ast.parse(src, filename=str(py))
            except (SyntaxError, OSError):
                # 解析失败不算 lint 违规——但要让人知道
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top not in self._stdlib and top not in self._allowed:
                            violations.append((str(py), node.lineno, top))
                elif isinstance(node, ast.ImportFrom):
                    # 相对导入(以 . 开头)——一定指向 arl_lite 内部,放行
                    if node.level and node.level > 0:
                        continue
                    if node.module is None:
                        continue
                    top = node.module.split(".")[0]
                    if top not in self._stdlib and top not in self._allowed:
                        violations.append((str(py), node.lineno, top))

        if not violations:
            return GateResult(
                name=self.name,
                passed=True,
                detail=f"scanned {len(_py_files(root))} files, no third-party imports",
                measured=0,
                baseline=0,
                blocking=self.blocking,
            )

        # 失败:逐条列出,文件:行号 模块名
        lines = [f"{len(violations)} violation(s):"]
        for path, lineno, mod in violations[:50]:
            lines.append(f"  {path}:{lineno} {mod}")
        if len(violations) > 50:
            lines.append(f"  ... and {len(violations) - 50} more")
        return GateResult(
            name=self.name,
            passed=False,
            detail="\n".join(lines),
            measured=len(violations),
            baseline=0,
            blocking=self.blocking,
        )


# =====================================================================
# Gate 2: test_baseline
# =====================================================================
#
# 防的退化:某次改动悄悄把已有测试搞挂,CI 通过但实际更烂。
# 跑 pytest 解析末尾的 "N failed, M passed",failed 数 > baseline 即失败。
#
# 边界:
# - 当前环境缺 pytest-asyncio,9 个 async 测试必然失败——这是 baseline,不算回归
# - 第一次跑会主动把当前 failed 数落进 baseline,让后续轮次有参考点


_PYTEST_SUMMARY_RE = re.compile(
    r"(?P<failed>\d+)\s+failed"  # "N failed"
    r"(?:[^,\n]*?,\s*(?P<passed>\d+)\s+passed)?"  # 可选 ", M passed"
)


class TestBaselineGate:
    """防的退化:已有测试悄悄退步,CI 没拦住。

    跑 pytest,对比 failed 数;只许降不许升(降了是改进,可 update_baseline)。
    """

    name = "test_baseline"
    blocking = True

    def run(self, repo: Path) -> GateResult:
        baseline = load_baseline(repo)
        prev_failed = baseline.get(self.name, {}).get("failed", 0)

        cmd = [sys.executable, "-m", "pytest", "tests/", "-q"]
        if _has_pytest_timeout():
            cmd.append("--timeout=300")

        try:
            proc = subprocess.run(
                cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=600,  # 兜底,防止 pytest 卡死
            )
        except subprocess.TimeoutExpired:
            return GateResult(
                name=self.name,
                passed=False,
                detail="pytest timed out after 600s",
                measured=-1,
                baseline=prev_failed,
                blocking=self.blocking,
            )
        except FileNotFoundError as e:
            return GateResult(
                name=self.name,
                passed=False,
                detail=f"pytest not available: {e}",
                measured=-1,
                baseline=prev_failed,
                blocking=self.blocking,
            )

        output = proc.stdout + proc.stderr
        m = _PYTEST_SUMMARY_RE.search(output)
        if not m:
            # 没匹配到 summary 行——可能 pytest 启动失败
            tail = "\n".join(output.splitlines()[-10:])
            return GateResult(
                name=self.name,
                passed=False,
                detail=f"could not parse pytest summary\n---tail---\n{tail}",
                measured=-1,
                baseline=prev_failed,
                blocking=self.blocking,
            )

        failed = int(m.group("failed"))
        passed = int(m.group("passed") or 0)

        # failed 比 baseline 多就是回归
        if failed > prev_failed:
            return GateResult(
                name=self.name,
                passed=False,
                detail=(
                    f"regression! baseline failed={prev_failed}, "
                    f"now failed={failed} (+{failed - prev_failed}); "
                    f"passed={passed}"
                ),
                measured={"failed": failed, "passed": passed},
                baseline=prev_failed,
                blocking=self.blocking,
            )

        # 通过(持平或改善)
        detail = (
            f"failed={failed} (baseline={prev_failed}), passed={passed}"
            if prev_failed > 0
            else f"first run: failed={failed}, passed={passed} (baseline established)"
        )
        return GateResult(
            name=self.name,
            passed=True,
            detail=detail,
            measured={"failed": failed, "passed": passed},
            baseline=prev_failed,
            blocking=self.blocking,
        )


# =====================================================================
# Gate 3: no_import_cycle
# =====================================================================
#
# 防的退化:模块之间互相 import 形成环,导致部分初始化失败、
# import 时随机 AttributeError、单元测试互相污染。
# 用 DFS 染白灰黑三色,灰色回边即环。


class NoImportCycleGate:
    """防的退化:arl_lite 内部模块互相 import 形成循环依赖。

    AST 解析每个 .py,提取所有 from .x import y / from arl_lite.x import y,
    构建模块依赖图,DFS 三色检测环。环路径以 "a -> b -> c -> a" 形式打印。
    """

    name = "no_import_cycle"
    blocking = True

    def run(self, repo: Path) -> GateResult:
        root = repo / "arl_lite"
        if not root.exists():
            return GateResult(
                name=self.name,
                passed=False,
                detail="arl_lite/ not found",
                measured=0,
                blocking=self.blocking,
            )

        files: dict[str, Path] = {}
        for p in _py_files(root):
            rel = p.relative_to(root).with_suffix("").as_posix()
            files[rel] = p

        graph: dict[str, set[str]] = defaultdict(set)
        for rel, path in files.items():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                # 相对导入:from .x import y / from ..x.y import z
                if node.level and node.level > 0:
                    parts = rel.split("/")
                    # depth = 包嵌套深度(普通 .py 与 __init__.py 都是 len(parts)-1)
                    depth = len(parts) - 1
                    # level=1 → target 起算于"当前包根";level=2 → 包根上一级,以此类推
                    eff_depth = depth - node.level + 1
                    if eff_depth < 0:
                        # 解析到 arl_lite/ 之外,运行时本就不合法,跳过
                        continue
                    base_parts = parts[:eff_depth]
                    mod_parts = node.module.split(".") if node.module else []
                    target_parts = base_parts + mod_parts
                    target = "/".join(target_parts)
                    if target in files and target != rel:
                        # 跳过自环边(A->A 不是真环,只是写法怪)
                        graph[rel].add(target)
                    continue
                # 绝对 import:只关心 arl_lite.* 内部模块
                if node.module and node.module.startswith("arl_lite"):
                    mod = node.module
                    if mod in files and mod != rel:
                        graph[rel].add(mod)

        # DFS 找环
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in files}
        path: list[str] = []
        cycles: list[list[str]] = []

        def dfs(u: str) -> None:
            color[u] = GRAY
            path.append(u)
            for v in graph[u]:
                if color[v] == GRAY:
                    idx = path.index(v)
                    cycles.append(path[idx:] + [v])
                elif color[v] == WHITE:
                    dfs(v)
            path.pop()
            color[u] = BLACK

        for n in files:
            if color[n] == WHITE:
                dfs(n)

        if not cycles:
            return GateResult(
                name=self.name,
                passed=True,
                detail=(
                    f"scanned {len(files)} modules, "
                    f"{sum(len(v) for v in graph.values())} internal imports, no cycles"
                ),
                measured=0,
                baseline=0,
                blocking=self.blocking,
            )

        # 去重(同一环从不同起点可能发现多次)
        uniq: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for c in cycles:
            key = tuple(c)
            if key not in seen:
                seen.add(key)
                uniq.append(c)

        lines = [f"{len(uniq)} cycle(s):"]
        for c in uniq[:10]:
            lines.append("  " + " -> ".join(c))
        if len(uniq) > 10:
            lines.append(f"  ... and {len(uniq) - 10} more")
        return GateResult(
            name=self.name,
            passed=False,
            detail="\n".join(lines),
            measured=len(uniq),
            baseline=0,
            blocking=self.blocking,
        )


# =====================================================================
# Gate 4: loc_budget
# =====================================================================
#
# 防的退化:无意识扩张——单轮加 1000 行没人管,半年后 10w 行没人敢改。
# 同时盯 devloop/ 自身:协议不能自己膨胀,否则 devloop 变难迭代。


class LocBudgetGate:
    """防的退化:代码无边界膨胀。

    - arl_lite/ 总行数(排除 tests/、__pycache__)不许比 baseline 涨超 300 行
    - arl_lite/devloop/ 总行数不许超 2000(协议自身的膨胀红线)
    """

    name = "loc_budget"
    blocking = True
    # 代码量预算本身就是"随功能增长而显式上移"的闸门,
    # 提升它是协议设计内的动作(比如一轮做完了 TLS 采集这种实打实的新功能)。
    # 只放开 total_loc;devloop_loc 是协议自身的红线,不接受提升。
    promotable = True
    promotable_fields = ("total_loc",)

    def run(self, repo: Path) -> GateResult:
        arl_root = repo / "arl_lite"
        if not arl_root.exists():
            return GateResult(
                name=self.name,
                passed=False,
                detail="arl_lite/ not found",
                measured=0,
                blocking=self.blocking,
            )

        # 1) arl_lite 全量(排除 devloop 和 tests,以免双重计算)
        all_py = [
            p for p in _py_files(arl_root)
            if "devloop" not in p.parts
        ]
        total_loc = _count_lines(all_py)

        # 2) devloop 单独统计
        devloop_py = list((arl_root / "devloop").rglob("*.py")) if (arl_root / "devloop").exists() else []
        devloop_py = [p for p in devloop_py if "__pycache__" not in p.parts]
        devloop_loc = _count_lines(devloop_py)

        baseline = load_baseline(repo)
        prev = baseline.get(self.name, {})

        problems: list[str] = []

        # devloop 红线(硬)
        if devloop_loc > _DEFAULT_DEVELOOP_LOC_LIMIT:
            problems.append(
                f"devloop/ has {devloop_loc} lines (limit {_DEFAULT_DEVELOOP_LOC_LIMIT})"
            )

        # arl_lite 增长(相对 baseline + 容差)
        prev_total = prev.get("total_loc")
        if prev_total is not None:
            limit = prev_total + _DEFAULT_LOC_TOLERANCE
            if total_loc > limit:
                problems.append(
                    f"arl_lite/ has {total_loc} lines "
                    f"(baseline {prev_total} + tolerance {_DEFAULT_LOC_TOLERANCE})"
                )

        if problems:
            return GateResult(
                name=self.name,
                passed=False,
                detail="; ".join(problems),
                measured={"total_loc": total_loc, "devloop_loc": devloop_loc},
                baseline=prev_total,
                blocking=self.blocking,
            )

        if prev_total is None:
            detail = (
                f"first run: arl_lite={total_loc}, devloop={devloop_loc} (baselines established)"
            )
        else:
            detail = (
                f"arl_lite={total_loc} (baseline {prev_total} +{total_loc - prev_total}), "
                f"devloop={devloop_loc}"
            )

        return GateResult(
            name=self.name,
            passed=True,
            detail=detail,
            measured={"total_loc": total_loc, "devloop_loc": devloop_loc},
            baseline=prev_total,
            blocking=self.blocking,
        )


# =====================================================================
# Gate 5: rules_have_advice
# =====================================================================
#
# 防的退化:规则文件被简化时把 advice/risk/name 删掉,
# 导致报告里没有修复指引、风险评级丢失、规则无法被引用。
# 零依赖:用文本扫描而不是 PyYAML 解析(项目铁律)。


_RULES_REQUIRED_KEYS = ("advice:", "name:", "risk:")


class RulesHaveAdviceGate:
    """防的退化:分析规则的 advice / name / risk 字段被删。

    扫描 arl_lite/modules/analysis/rules/*.yml,用文本子串检查,
    避免引入 PyYAML 解析(项目零依赖铁律)。
    """

    name = "rules_have_advice"
    blocking = True

    def run(self, repo: Path) -> GateResult:
        rules_dir = repo / "arl_lite" / "modules" / "analysis" / "rules"
        if not rules_dir.exists():
            return GateResult(
                name=self.name,
                passed=False,
                detail=f"rules dir not found: {rules_dir}",
                measured=0,
                blocking=self.blocking,
            )

        bad: list[str] = []
        total = 0
        for p in sorted(rules_dir.glob("*.yml")):
            total += 1
            try:
                content = p.read_text(encoding="utf-8")
            except OSError:
                bad.append(f"{p.name}:read_error")
                continue
            missing = [k for k in _RULES_REQUIRED_KEYS if k not in content]
            if missing:
                bad.append(f"{p.name}:missing={','.join(missing)}")

        compliant = total - len(bad)
        if bad:
            return GateResult(
                name=self.name,
                passed=False,
                detail=f"{compliant}/{total} compliant\n  " + "\n  ".join(bad),
                measured=compliant,
                baseline=total,
                blocking=self.blocking,
            )
        return GateResult(
            name=self.name,
            passed=True,
            detail=f"all {total} rules compliant",
            measured=compliant,
            baseline=total,
            blocking=self.blocking,
        )


# =====================================================================
# Gate 6: prompt_injection_guard
# =====================================================================
#
# 防的退化:prompts.py 里的净化函数被回退或绕过,导致 prompt injection
# 直接打到 LLM。这是项目安全防线之一,必须硬性守住。


class PromptInjectionGuardGate:
    """防的退化:prompts.py 的净化防线被回退。

    静态检查:
    1. prompts.py 必须定义 _sanitize 函数
    2. to_json 函数体必须调用 _sanitize(——所有 AI 边界点的数据出口
    任一缺失 = 失败。
    """

    name = "prompt_injection_guard"
    blocking = True

    def run(self, repo: Path) -> GateResult:
        p = repo / "arl_lite" / "ai" / "prompts.py"
        if not p.exists():
            return GateResult(
                name=self.name,
                passed=False,
                detail=f"{p} not found",
                measured=0,
                blocking=self.blocking,
            )

        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            return GateResult(
                name=self.name,
                passed=False,
                detail=f"syntax error in prompts.py: {e}",
                measured=0,
                blocking=self.blocking,
            )

        funcs: dict[str, ast.FunctionDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                funcs[node.name] = node

        problems: list[str] = []
        if "_sanitize" not in funcs:
            problems.append("_sanitize function not defined")
        if "to_json" not in funcs:
            problems.append("to_json function not defined")
        else:
            # to_json 体里必须实际调用 _sanitize(  —— 防被人注释掉/替换
            to_json = funcs["to_json"]
            calls_sanitize = False
            for sub in ast.walk(to_json):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    # _sanitize(...) / self._sanitize(...) / mod._sanitize(...)
                    name = None
                    if isinstance(f, ast.Name):
                        name = f.id
                    elif isinstance(f, ast.Attribute):
                        name = f.attr
                    if name == "_sanitize":
                        calls_sanitize = True
                        break
            if not calls_sanitize:
                problems.append("to_json does not call _sanitize(")

        if problems:
            return GateResult(
                name=self.name,
                passed=False,
                detail="; ".join(problems),
                measured=0,
                baseline=1,
                blocking=self.blocking,
            )
        return GateResult(
            name=self.name,
            passed=True,
            detail="_sanitize defined and to_json calls it",
            measured=1,
            baseline=1,
            blocking=self.blocking,
        )


# =====================================================================
# Gate 7: doc_freshness
# =====================================================================
#
# 防的退化:文档(PROJECT_PLAN.md)还在引用实际未用的依赖,误导新读者。
# 引用过的、过期的依赖列表维护在此 gate 内——文档已迁移/重写可清空此表。


_DOC_STALE_DEPS = (
    "typer",
    "textual",
    "litellm",
    "pyyaml",
)

# 文档豁免标记: 包裹"有意保留的历史记录"段落
_DOC_IGNORE_MARKER = "<!-- devloop:ignore-doc-stale -->"
_DOC_IGNORE_END = "<!-- /devloop:ignore-doc-stale -->"


class DocFreshnessGate:
    """防的退化:PROJECT_PLAN.md 还在吹嘘已下架的依赖。

    当前规则只警告不拦——文档迁移是阶段性任务,不该反复挡迭代。
    但命中 > 0 仍要 detail 给出具体引用位置,便于后续清理。
    """

    name = "doc_freshness"
    blocking = False  # 仅警告

    def run(self, repo: Path) -> GateResult:
        p = repo / "docs" / "PROJECT_PLAN.md"
        if not p.exists():
            return GateResult(
                name=self.name,
                passed=True,
                detail=f"{p} not found (skipped)",
                measured=0,
                blocking=self.blocking,
            )

        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return GateResult(
                name=self.name,
                passed=False,
                detail=f"read error: {e}",
                measured=-1,
                blocking=self.blocking,
            )

        hits: list[tuple[str, int, str]] = []
        # 豁免机制:文档用 `<!-- devloop:ignore-doc-stale -->` 开启一段
        # "有意保留的历史记录", 直到出现 `<!-- /devloop:ignore-doc-stale -->`
        # 或文件结束。
        #
        # 没有豁免, 门禁只能靠猜: 对照表里提 typer 是合理记录, 但没法和
        # "声称项目依赖 typer"区分开——只能全报, 逼人去删本该留的信息。
        # 豁免把判断权还给文档作者, 而作者是唯一知道这段话是吹嘘还是
        # 记录的人。
        #
        # 开闭标记在同一行出现时, 该行自身也豁免。
        ignoring = False
        for line_no, line in enumerate(text.splitlines(), 1):
            if _DOC_IGNORE_MARKER in line:
                ignoring = not line.rstrip().endswith(_DOC_IGNORE_END)
                continue
            if ignoring:
                if _DOC_IGNORE_END in line:
                    ignoring = False
                continue
            for dep in _DOC_STALE_DEPS:
                # 单词边界匹配,避免误命中子串(如 `typer` 不应命中 `typer-like`)
                if re.search(rf"\b{re.escape(dep)}\b", line, re.IGNORECASE):
                    hits.append((dep, line_no, line.strip()[:120]))

        if not hits:
            return GateResult(
                name=self.name,
                passed=True,
                detail="no stale dependency references",
                measured=0,
                baseline=0,
                blocking=self.blocking,
            )

        # 命中:警告而非失败(有问题但不拦下迭代——文档迁移是阶段性任务)
        lines = [f"{len(hits)} stale reference(s):"]
        for dep, ln, snippet in hits[:20]:
            lines.append(f"  L{ln} {dep}: {snippet}")
        if len(hits) > 20:
            lines.append(f"  ... and {len(hits) - 20} more")
        return GateResult(
            name=self.name,
            passed=False,  # 文档确实过期——passed=False,但 blocking=False 不拦
            detail="\n".join(lines),
            measured=len(hits),
            baseline=0,
            blocking=self.blocking,
        )


# =====================================================================
# Registry & 顶层 API
# =====================================================================


_REGISTRY: dict[str, Gate] = {
    g.name: g
    for g in (
        NoThirdPartyImportGate(),
        TestBaselineGate(),
        NoImportCycleGate(),
        LocBudgetGate(),
        RulesHaveAdviceGate(),
        PromptInjectionGuardGate(),
        DocFreshnessGate(),
    )
}


def get_gate(name: str) -> Gate:
    """按名字取 gate;未知名字抛 KeyError(契约要求)"""
    if name not in _REGISTRY:
        raise KeyError(f"unknown gate: {name!r}; known: {all_gate_names()}")
    return _REGISTRY[name]


def all_gate_names() -> list[str]:
    """全部可用 gate 名(稳定的全量列表,便于 protocol.py 全跑)"""
    return list(_REGISTRY.keys())


def run_all(
    repo: Path,
    only: list[str] | None = None,
) -> list[GateResult]:
    """跑所有(或 only 指定)gate,返回结果列表

    Args:
        repo: repo 根目录
        only: 仅跑这些名字的 gate;None = 全部
    """
    names = only if only is not None else all_gate_names()
    # 名字未知立即失败,不静默跳过
    for n in names:
        if n not in _REGISTRY:
            raise KeyError(f"unknown gate: {n!r}")
    results: list[GateResult] = []
    for n in names:
        g = _REGISTRY[n]
        t0 = time.monotonic()
        try:
            r = g.run(repo)
        except Exception as e:  # noqa: BLE001 — gate 自身崩也要被记录
            r = GateResult(
                name=n,
                passed=False,
                detail=f"gate raised {type(e).__name__}: {e}",
                blocking=g.blocking,
            )
        dt = time.monotonic() - t0
        # detail 里塞耗时,便于定位慢 gate
        r_dt = GateResult(
            name=r.name,
            passed=r.passed,
            detail=f"[{dt:.2f}s] {r.detail}",
            measured=r.measured,
            baseline=r.baseline,
            blocking=r.blocking,
        )
        results.append(r_dt)
    return results


# =====================================================================
# CLI:python3 -m arl_lite.devloop.gates [gate_name]
# =====================================================================


def _format_result(r: GateResult) -> str:
    flag = "PASS" if r.passed else "FAIL"
    blocking = "[block]" if r.blocking else "[warn] "
    return f"  [{flag}] {blocking} {r.name}: {r.detail}"


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:无参数跑全部,带参数只跑指定 gate"""
    parser = argparse.ArgumentParser(
        prog="python3 -m arl_lite.devloop.gates",
        description="arl-lite 自持迭代协议的质量闸门",
    )
    parser.add_argument(
        "gate",
        nargs="*",
        default=None,
        help="只跑指定的 gate(可多个,省略则跑全部)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="跑完把每个 gate 的 measured 值写进 baselines.json",
    )
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parents[2]  # .../arl-lite
    only = list(args.gate) if args.gate else None
    print(f"== arl-lite gates (repo={repo}) ==")
    if only:
        print(f"  filter: {', '.join(only)}")

    results = run_all(repo, only=only)

    for r in results:
        print(_format_result(r))

    # 总结
    failed = [r for r in results if not r.passed and r.blocking]
    warned = [r for r in results if not r.passed and not r.blocking]
    blocking_failed = [r for r in results if not r.passed and r.blocking]

    print("---")
    print(f"  total: {len(results)}, failed(blocking): {len(blocking_failed)}, warned: {len(warned)}")

    # baseline 落盘(可选)——每个 gate 的 measured 已经是完整 dict
    if args.update_baseline:
        data = load_baseline(repo)
        for r in results:
            if r.measured is not None:
                data[r.name] = r.measured
        save_baseline(repo, data)
        print(f"  baseline updated → {baseline_path(repo)}")

    # 退出码:任何 blocking 失败 → 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())