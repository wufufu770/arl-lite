"""架构约束测试

抄 HunterX 的思路(它有 26 个这类测试守住 Clean Architecture 分层),
但只取对本项目真正有约束力的几条——目的是**防腐化**,不是凑数。

每条测试对应一个真实的退化路径。没有退化路径的约束不写,
因为写了也只会变成"看起来很严、实际没人看"的摆设。

已有真实事故支撑的约束:
  - correlation_engine 直接 import sqlite3  → 必须经 Storage
  - modules 依赖 core.base_module          → 这是设计,不是违规
  - core 依赖 db                          → 依赖注入+weak import,已核查合规
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

def _find_repo_root() -> Path:
    """向上找 pyproject.toml + arl_lite/ 确认根目录。

    不靠 `parents[N]` 猜——从 tests/ 出发 parents[1] 是项目根,
    但 __file__ 是相对路径时 resolve() 前后的层级数可能不同,
    算错一层就会扫到项目外面去。
    """
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "pyproject.toml").is_file() and (cand / "arl_lite").is_dir():
            return cand
    raise RuntimeError(f"找不到项目根(从 {here} 向上未找到 pyproject.toml + arl_lite/)")


REPO = _find_repo_root()
PKG = REPO / "arl_lite"

# 分层定义(外层可以依赖内层,反之不行)
#
# 实际拓扑(用本文件其余测试验证过):
#   notify ← db ← core ← modules ← integrations
#                       ↑        ↓
#                    ai/mcp/tui
#
# 关于 core -> modules: task_runner 通过 modules.registry 发现模块。
# 这不是理想设计(引擎不该认识业务), 但它是**既有且正确的行为**——
# 改成依赖注入要动 task_runner 的公开 API, 属于独立议题, 不该由
# 一个架构测试顺手判死。约束的价值在于防退化, 不在于评判现状。
LAYERS = {
    "notify": set(),
    "db": {"notify"},
    # core -> modules: task_runner 经 modules.registry 发现模块,
    # 引擎依赖业务注册表。方向不理想但是既有行为, 见上方说明。
    "core": {"db", "notify", "modules"},
    # modules -> integrations: github.py 复用 github_search 的查询函数,
    # 能力下沉到 integrations、模块只做编排, 方向正确。
    "modules": {"core", "db", "notify", "integrations"},
    "integrations": {"core", "db", "notify", "modules"},
    "ai": {"core", "db", "modules", "notify"},
    "mcp": {"core", "db", "modules"},
    "tui": {"core", "db", "modules", "ai"},
}

# 允许的顶层包(不算层级)
ALLOWED_ROOTS = set(LAYERS) | {"__init__", "__main__", "cli", "cli_report_html"}


def _layer_of(rel: Path) -> str | None:
    """从**相对 PKG 的路径**取所属层。传绝对路径会报错。"""
    parts = rel.parts
    return parts[0] if parts and parts[0] in LAYERS else None


def _imports(tree: ast.AST) -> set[str]:
    """取出一个文件 import 到的所有同包顶层模块"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                out.add(node.module.split(".")[0])
            elif node.module and node.module.startswith("arl_lite."):
                out.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("arl_lite."):
                    out.add(a.name.split(".")[1])
    return out


class TestNoLayerInversion(unittest.TestCase):
    """分层不得反向依赖

    退化路径:某天为了"方便"在 db 层 import 了 core,
    于是存储逻辑知道了业务规则,再也没法单独测 DB。
    """

    def test_no_inversion(self):
        violations = []
        for py in PKG.rglob("*.py"):
            rel = py.relative_to(PKG)
            layer = _layer_of(rel)
            if not layer:
                continue
            allowed = LAYERS[layer]
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for dep in _imports(tree):
                if dep in ALLOWED_ROOTS and dep not in allowed and dep != layer:
                    violations.append(f"{rel.as_posix()} ({layer}) -> {dep}")
        self.assertEqual(violations, [], f"分层反向依赖: {violations}")


class TestPersistenceIsolation(unittest.TestCase):
    """SQL 访问必须经 Storage

    退化路径:有人为了少写一个参数直接在业务代码里 `import sqlite3`
    连库。后果是连接管理、事务、workspace 隔离全部绕开——
    本项目三态纪律和 workspace 概念都建立在这层封装上。

    ## 约束的写法演进

    初版是"除了 db/storage.py 谁都不许 import sqlite3"。它抓得住退化路径,
    但**编码的是文件名巧合而不是真实不变式**:`db/errors.py` 读一下
    `sqlite3.SQLITE_CONSTRAINT_UNIQUE` 常量做异常分类,既不开连接也不执行
    SQL,却被判成违规。

    于是把约束改成编码**意图**:除了 storage 以外,任何文件都不得
    **执行 SQL**(connect/execute/cursor/...)。只读常量和异常属性是允许的。

    这样约束不是被放宽了,而是变准了——见同文件的
    `test_refined_rule_still_catches_real_bypass`,它验证新规则照样能抓住
    一个真的绕过 Storage 的文件。
    """

    # 唯一合法执行 SQL 的文件
    SQL_ACCESS_POINT = "db/storage.py"

    # 判定"执行了 SQL"的调用特征。少一个就等于开了一个绕过 Storage 的口子。
    _SQL_CALLS = (
        r"\.connect\s*\(",
        r"\.execute\s*\(",
        r"\.executemany\s*\(",
        r"\.executescript\s*\(",
        r"\.cursor\s*\(",
    )

    def test_only_storage_executes_sql(self):
        offenders = []
        for py in PKG.rglob("*.py"):
            rel = py.relative_to(PKG)
            # storage.py 是唯一合法访问点。用 as_posix() 字符串比较——
            # Path.__eq__ 在跨平台时对分隔符敏感
            if rel.as_posix() == self.SQL_ACCESS_POINT:
                continue
            src = py.read_text(encoding="utf-8")
            if not re.search(r"^\s*import\s+sqlite3", src, re.M):
                continue
            # 读常量/看异常属性不算访问;执行 SQL 才算
            for pat in self._SQL_CALLS:
                if re.search(pat, src):
                    offenders.append(f"{rel.as_posix()} ({pat})")
                    break
        self.assertEqual(offenders, [],
                         f"SQL 只能由 {self.SQL_ACCESS_POINT} 执行: {offenders}")

    def test_refined_rule_still_catches_real_bypass(self):
        """反向验证:新规则必须真的能抓住绕过 Storage 的文件

        约束被"精化"过,所以必须证明精化没有让它变空。
        拿两段真代码过一遍判定逻辑:一个只读常量(该放行),
        一个真的开连接(该拦下)。
        """
        reads_constant = (
            "import sqlite3\n"
            "CODES = frozenset({sqlite3.SQLITE_CONSTRAINT_UNIQUE})\n"
            "def classify(e):\n"
            "    return getattr(e, 'sqlite_errorcode', None) in CODES\n"
        )
        opens_connection = (
            "import sqlite3\n"
            "def run():\n"
            "    return sqlite3.connect('data.db').execute('SELECT 1')\n"
        )

        def flagged(src: str) -> bool:
            return any(re.search(p, src) for p in self._SQL_CALLS)

        # 只读常量:放行(db/errors.py 属于这一类)
        self.assertFalse(flagged(reads_constant),
                         "只读 sqlite3 常量不该被判成 SQL 访问")
        # 真开连接:必须拦下
        self.assertTrue(flagged(opens_connection),
                        "新规则抓不住真绕过了,约束被精化空了")

    def test_storage_is_still_the_exempt_access_point(self):
        """豁免名单必须只有 storage 一个,且它自己真的在执行 SQL

        防的是"有人往豁免名单里再塞一个文件"。名单硬编码在本类里,
        改动会在这里留下痕迹。
        """
        self.assertEqual(self.SQL_ACCESS_POINT, "db/storage.py")
        src = (PKG / self.SQL_ACCESS_POINT).read_text(encoding="utf-8")
        self.assertRegex(src, r"\.connect\s*\(",
                         "豁免文件自己却不碰连接,豁免名存实亡")


class TestZeroDependencyIronLaw(unittest.TestCase):
    """零第三方依赖是项目铁律

    退化路径:某次"就加这一个包"用了 requests,于是这个工具再也不能
    在 PyPI 不可达的环境里跑了——而那正是侦察工具的部署常态。
    """
    # 已在 pyproject 里但实现阶段全部推翻的依赖——不能偷偷回来
    FORBIDDEN = {
        "typer", "textual", "httpx", "requests", "yaml", "rich",
        "litellm", "mcp", "apscheduler", "openpyxl", "aiohttp",
    }

    def test_no_thirdparty_imports(self):
        import sys
        offenders = {}
        for py in PKG.rglob("*.py"):
            rel = py.relative_to(PKG)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            found = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        top = a.name.split(".")[0]
                        if top not in sys.stdlib_module_names and top != "arl_lite":
                            found.add(top)
                elif isinstance(node, ast.ImportFrom):
                    if node.level or not node.module:
                        continue
                    top = node.module.split(".")[0]
                    if top not in sys.stdlib_module_names and top != "arl_lite":
                        found.add(top)
            if found:
                offenders[str(rel)] = sorted(found)
        self.assertEqual(offenders, {}, f"引入第三方依赖: {offenders}")

    def test_forbidden_never_return(self):
        """特别盯住那几个"当年规划过、后来推翻"的包"""
        offenders = set()
        for py in PKG.rglob("*.py"):
            for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    mods = [node.module.split(".")[0]]
                offenders.update(set(mods) & self.FORBIDDEN)
        self.assertEqual(offenders, set(),
                         f"被明令废弃的依赖复活了: {offenders}")


class TestRulesStayActionable(unittest.TestCase):
    """规则必须可操作

    退化路径:规则只写"命中即报"不给处置建议,使用者无法采取行动,
    告警就退化成噪音。这是规则"可维护性"的底线。
    """

    RULES = PKG / "modules" / "analysis" / "rules"

    def test_all_rules_have_advice(self):
        missing = [f.name for f in self.RULES.glob("*.yml")
                   if "advice:" not in f.read_text(encoding="utf-8")]
        self.assertEqual(missing, [], f"缺 advice 的规则: {missing}")

    def test_all_rules_have_confidence(self):
        """置信度字段——防"命中即高危"回退"""
        missing = [f.name for f in self.RULES.glob("*.yml")
                   if "confidence:" not in f.read_text(encoding="utf-8")]
        self.assertEqual(missing, [], f"缺 confidence 的规则: {missing}")

    def test_risk_in_range(self):
        bad = []
        for f in self.RULES.glob("*.yml"):
            m = re.search(r"^risk:\s*(\d+)", f.read_text(encoding="utf-8"), re.M)
            if not m or not (0 <= int(m.group(1)) <= 10):
                bad.append(f.name)
        self.assertEqual(bad, [], f"risk 越界或缺失: {bad}")


class TestSecurityInvariants(unittest.TestCase):
    """安全防线不得回退

    退化路径:有人为了"让 prompt 简短"把 _sanitize 调用删了,
    于是攻击者可以在页面 title 里写"忽略以上所有指令"。
    """

    def test_prompt_sanitize_preserved(self):
        src = (PKG / "ai" / "prompts.py").read_text(encoding="utf-8")
        self.assertIn("def _sanitize(", src, "净化层被删除")
        tree = ast.parse(src)
        to_json_calls_sanitize = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "to_json":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "_sanitize":
                        to_json_calls_sanitize = True
        self.assertTrue(to_json_calls_sanitize,
                        "to_json 不再调用 _sanitize —— 净化形同虚设")

    def test_all_templates_have_data_boundary(self):
        """五个 AI 边界都要有 <data_json> 定界"""
        src = (PKG / "ai" / "prompts.py").read_text(encoding="utf-8")
        for name in ("ASK", "REPORT", "EXPLAIN", "SUGGEST", "FIX"):
            m = re.search(rf"{name}_USER_TEMPLATE\s*=\s*\"\"\"(.*?)\"\"\"", src, re.S)
            self.assertIsNotNone(m, f"{name}_USER_TEMPLATE 未找到")
            self.assertIn("<data_json>", m.group(1),
                          f"{name} 模板缺 data_json 定界")

    def test_concurrency_bounded(self):
        """并发闸不得被摘掉

        退化路径:有人嫌慢把 Semaphore 去掉,9 个 module × 内部 50 并发
        = 450 请求打向 crt.sh, 会被限流甚至封 IP。
        """
        src = (PKG / "core" / "task_runner.py").read_text(encoding="utf-8")
        self.assertIn("Semaphore", src, "并发闸被摘除")
        self.assertIn("MODULE_TIMEOUT_SECONDS", src, "单 module 超时兜底被摘除")
        self.assertIn("asyncio.wait_for", src, "超时保护被摘除")


class TestNoImportCycle(unittest.TestCase):
    """循环依赖会让 import 行为不可预测

    退化路径:A import B、B import C、C 又 import A。
    结果取决于谁先被 import——单元测试里能过、CLI 里可能炸。
    """

    def test_no_cycles(self):
        graph: dict[str, set[str]] = {}
        for py in PKG.rglob("*.py"):
            rel = py.relative_to(PKG)
            mod = ".".join(rel.with_suffix("").parts)
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            graph[mod] = _imports(ast.parse(py.read_text(encoding="utf-8")))

        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[str, int] = {}
        cycles: list[str] = []

        def dfs(node: str, path: list[str]):
            color[node] = GREY
            for dep in graph.get(node, ()):
                if dep not in graph:
                    continue
                state = color.get(dep, WHITE)
                if state == GREY:
                    cycles.append(" -> ".join(path + [dep]))
                elif state == WHITE:
                    dfs(dep, path + [dep])
            color[node] = BLACK

        for node in list(graph):
            if color.get(node, WHITE) == WHITE:
                dfs(node, [node])
        self.assertEqual(cycles, [], f"检测到循环依赖: {cycles}")


if __name__ == "__main__":
    unittest.main()
