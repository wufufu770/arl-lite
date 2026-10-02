"""门禁必须验**实际效果**,不是验"它存在"

## 这个文件为什么存在

r24 抓到一道叫 `prompt_injection_guard` 的门禁:它只检查
"`_sanitize` 函数定义了没" + "`to_json` 调用它没",两条都过就绿 ——
而实跑真实载荷,三类注入**原样进了 prompt**,其中标签检测还是条死规则。

r25 继续审剩下的门禁,又抓出两处同族问题:

| 门禁 | 原来验什么 | 实际能漏什么 |
|---|---|---|
| `rules_have_advice` | 文本里有 `"advice:"` 子串 | 键被 `#` 注释掉 → 整条规则加载失败 |
| `no_thirdparty_import` | AST 里的静态 import | `importlib.import_module("requests")` |

两次的共同形状:**检查通过了,但被检查的东西没在起作用。**
和 r21 的无消费者、r22 的谎报文档、r23 的假覆盖率、r23 的
`add` 丢工作是同一族 —— **表面有,底下没发生**。

## 下面每条都注明了「实测真能溜进去过」

不是凭想象构造的失败。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from arl_lite.devloop.gates import NoThirdPartyImportGate, RulesHaveAdviceGate


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "pyproject.toml").is_file() and (cand / "arl_lite").is_dir():
            return cand
    raise RuntimeError("找不到项目根")


REPO = _find_repo_root()
RULES_DIR = REPO / "arl_lite" / "modules" / "analysis" / "rules"


class TestRulesGateCatchesCommentedOutFields(unittest.TestCase):
    """把规则键注释掉,门禁必须红

    ## r25 实测的失败

    旧门禁是 `missing = [k for k in KEYS if k not in content]` —— 纯子串。
    把 `redis_public.yml` 的四个顶层键全加 `#` 之后,文本里
    `"advice:"` 这些子串**一个都没少**,旧门禁报:

        OK  rules_have_advice: all 37 rules compliant

    而引擎实际输出:

        failed to load rule redis_public.yml: missing 'name'
        loaded 36 correlation rules

    一条 **risk 9** 的高危规则从引擎里静默消失,报告里不会有任何提示。
    """

    def setUp(self):
        self.target = RULES_DIR / "redis_public.yml"
        self.backup = self.target.read_text(encoding="utf-8")
        self.addCleanup(self._restore)

    def _restore(self):
        self.target.write_text(self.backup, encoding="utf-8")

    def _comment_out_required_keys(self):
        out = []
        for line in self.backup.splitlines():
            if line.startswith(("advice:", "name:", "risk:", "confidence:")):
                out.append("# " + line)      # YAML 注释:字段实际已失效
            else:
                out.append(line)
        self.target.write_text("\n".join(out), encoding="utf-8")

    def test_gate_fails_when_required_keys_are_commented_out(self):
        self._comment_out_required_keys()
        result = RulesHaveAdviceGate().run(REPO)
        self.assertFalse(
            result.passed,
            "把四条顶层键全注释掉,门禁仍然通过 —— "
            f"它验的是文本子串,不是规则是否真的能加载。detail={result.detail}")

    def test_the_rule_actually_stops_loading(self):
        """先证明"确实出事了",再验门禁抓到了 —— 顺序不能反

        不这么做的话,门禁"变红"可能只是因为别的原因
        (比如加载器炸了),而真正的缺陷其实没被覆盖。
        """
        from arl_lite.core.correlation_engine import load_all_rules
        before = len(load_all_rules(RULES_DIR))
        self._comment_out_required_keys()
        after = len(load_all_rules(RULES_DIR))
        self.assertEqual(after, before - 1,
                         f"前置条件不成立:注释掉键之后规则数没变({before}->{after}),"
                         "说明这个构造方式复现不了 r25 抓到的那个缺陷")
        self.assertFalse(RulesHaveAdviceGate().run(REPO).passed,
                         "规则确实少了一条,门禁却还是绿的")


class TestRulesGateCatchesEmptyValues(unittest.TestCase):
    """键在但值为空,也算缺 —— 旧的子串检查看不见这种"""

    def setUp(self):
        self.target = RULES_DIR / "redis_public.yml"
        self.backup = self.target.read_text(encoding="utf-8")
        self.addCleanup(self._restore)

    def _restore(self):
        self.target.write_text(self.backup, encoding="utf-8")

    def test_empty_advice_value_is_caught(self):
        # `advice: |` -> `advice: ""`:文本里 "advice:" 子串还在,旧门禁会放行
        self.target.write_text(
            self.backup.replace("advice: |", 'advice: ""', 1), encoding="utf-8")
        result = RulesHaveAdviceGate().run(REPO)
        self.assertFalse(result.passed,
                         f"advice 被清空但门禁通过 —— detail={result.detail}")
        self.assertIn("advice", result.detail)


class TestImportGateCatchesDynamicImports(unittest.TestCase):
    """动态 import 也是第三方依赖 —— 铁律不因写法而失效

    项目铁律是「零第三方 pip 依赖,纯 stdlib」。旧门禁只扫 AST 里的
    `import` / `from ... import`,于是 r25 实测这两种藏法**都能让门禁全绿**:

        importlib.import_module("requests")
        __import__("httpx")

    门禁当时的措辞是 "scanned 84 files, no third-party imports" ——
    这话**说过头了**:它只证明了「没有静态第三方 import」。
    """

    def setUp(self):
        self.probe = REPO / "arl_lite" / "_probe_dynamic_import.py"

    def tearDown(self):
        if self.probe.exists():
            self.probe.unlink()

    def _run_gate(self) -> bool:
        return NoThirdPartyImportGate().run(REPO).passed

    def test_importlib_import_module_is_caught(self):
        self.probe.write_text(
            'import importlib\n'
            'requests = importlib.import_module("requests")\n',
            encoding="utf-8")
        self.assertFalse(self._run_gate(),
                         "importlib.import_module('requests') 让门禁通过了 —— "
                         "零依赖铁律可以被一个函数调用绕过")

    def test_dunder_import_is_caught(self):
        self.probe.write_text('__import__("httpx")\n', encoding="utf-8")
        self.assertFalse(self._run_gate(),
                         "__import__('httpx') 让门禁通过了")

    def test_variable_argument_is_not_flagged(self):
        """变量传入的模块名静态不可判定 —— 放行,但不能因此误报

        门禁若对不可判定的写法也报警,总有一天会被人加白名单绕过去,
        那比漏一个真依赖更糟。
        """
        self.probe.write_text(
            'name = "yaml"\n'
            '__import__(name)\n',
            encoding="utf-8")
        self.assertTrue(self._run_gate(),
                        "变量传入的动态 import 被误报了 —— "
                        "静态不可判定的东西不该猜")

    def test_stdlib_and_internal_dynamic_imports_pass(self):
        self.probe.write_text(
            'import importlib\n'
            'importlib.import_module("json")\n'
            'importlib.import_module("arl_lite.core.risk_score")\n',
            encoding="utf-8")
        self.assertTrue(self._run_gate(),
                        "标准库和内部模块的动态 import 被误报了")

    def test_baseline_is_clean_without_probe(self):
        """前置条件:探针文件不在时门禁必须是绿的

        少了这条,上面四条可能因为"门禁本来就红"而假通过。
        """
        self.assertFalse(self.probe.exists())
        self.assertTrue(self._run_gate(), "没有探针文件时门禁就不绿了")


class TestTheseGatesAreNotTextMatching(unittest.TestCase):
    """守住「用 AST / 加载器,不用文本子串」这件事本身

    两次修复的共同教训:文本子串检查分不清
    「字段是活的」和「字段被注释掉了」。

    这条用 AST 确认门禁源码里**没有**退回到读全文找子串的写法。
    它是补充判据 —— 真正的判别力在上面那些行为测试上。
    """

    def test_rules_gate_no_longer_scans_raw_text(self):
        import ast
        from arl_lite.devloop import gates

        tree = ast.parse(Path(gates.__file__).read_text(encoding="utf-8"))
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "RulesHaveAdviceGate")
        # 找形如 `k not in content` 的子串比较 —— 那是退回文本检查的信号
        for node in ast.walk(cls):
            if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.NotIn):
                self.fail(
                    "RulesHaveAdviceGate 里出现了 `... not in ...` 比较 —— "
                    "很可能退回到文本子串检查了。那正是 r25 抓到的缺陷:"
                    "注释掉键,子串还在,门禁全绿,而规则已经加载不了。")

    def test_rules_gate_actually_calls_the_loader(self):
        import inspect
        from arl_lite.devloop import gates
        self.assertIn("load_all_rules", inspect.getsource(gates.RulesHaveAdviceGate),
                      "规则门禁不再调用引擎自己的加载器了 —— "
                      "它验的就不是引擎真正会用到的那份数据")


if __name__ == "__main__":
    unittest.main()
