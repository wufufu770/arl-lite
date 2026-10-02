"""置信度模型的**真值表** —— 硬编码期望值,不复算

## 为什么单独一个文件

`tests/test_confidence.py::test_disposition_matches_thresholds` 长这样:

```python
if a.score >= THRESHOLD_REPORT:      # ← 从被测模块 import 回来的常量
    self.assertEqual(a.status, STATUS_REPORT)
```

它**重新实现了一遍 assess() 的判定逻辑**,而且用的是被测模块自己
导出的阈值。于是把 `THRESHOLD_OBSERVE` 从 0.45 改成 0.40 ——
或者更现实一点,把 `base_prior` 里的 0.45 改成 0.4「顺便取个整」——
这条测试**照样绿**。

这不是它名字暗示的「验证阈值正确」,它验证的是
「assess() 自己的输出符合 assess() 自己的规则」。恒真。

代价是真实付过的:`docs/CONFIDENCE_VS_RISK.md` 初稿把 observe 下界
写成 0.4、把 `exclusion_bonus` 写反了,而当时**没有任何测试会红** ——
因为没有任何测试钉住这些数。文档于是顺理成章地写上了
「实测,不是照 docstring 转述」,而实际上那张表是手抄的。

这个文件把那 9 组数**硬编码成字面量**,并在最后把
`docs/CONFIDENCE_VS_RISK.md` 里那张表读回来逐格比对。
要改任何一个因子,必须同时改这里的期望值 —— 这是刻意的阻力。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from arl_lite.core.confidence import (
    assess,
    CONF_HIGH, CONF_MEDIUM, CONF_LOW,
    STATUS_REPORT, STATUS_OBSERVE, STATUS_DISCARD,
)


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "pyproject.toml").is_file() and (cand / "arl_lite").is_dir():
            return cand
    raise RuntimeError("找不到项目根")


REPO = _find_repo_root()
DOC = REPO / "docs" / "CONFIDENCE_VS_RISK.md"


# ── 阈值:字面量,故意不从 confidence 模块 import ──────────────────────
#
# 从模块 import 就等于让被测对象自己出题。import 的话改常量测试不红。
THRESHOLD_REPORT_EXPECTED = 0.70
THRESHOLD_OBSERVE_EXPECTED = 0.45

_BASE_PRIOR_EXPECTED = {
    CONF_HIGH: 0.90,
    CONF_MEDIUM: 0.70,
    CONF_LOW: 0.45,
}
# 1 张 / 2 张 / >=3 张,再往下不降(封顶)
_TABLE_COMPLEXITY_EXPECTED = {1: 1.0, 2: 0.9, 3: 0.8, 5: 0.8}
# 带 has_exclusion 才加分,不带 = 1.0。初稿文档在这里写反了。
_EXCLUSION_EXPECTED = {False: 1.0, True: 1.05}
_CROSS_EVIDENCE_EXPECTED = {
    (False, False): 1.0,   # 没声明 cross_ref
    (True, False): 0.6,    # 声明了但没满足
    (True, True): 1.15,    # 声明了且满足
}


# ── 9 组真值:(base, has_cross_ref, satisfied) -> (score, status) ──────
#
# 数字来自 r22 实跑 assess(),未经四舍五入。
# score 精确到 3 位:assess() 里就是 round(score, 3)。
TRUTH_TABLE = [
    # (base,        xref,  sat,   score, status)
    (CONF_HIGH,   False, False, 0.900, STATUS_REPORT),
    (CONF_HIGH,   True,  False, 0.540, STATUS_OBSERVE),
    (CONF_HIGH,   True,  True,  1.000, STATUS_REPORT),
    (CONF_MEDIUM, False, False, 0.700, STATUS_REPORT),
    (CONF_MEDIUM, True,  False, 0.420, STATUS_DISCARD),
    (CONF_MEDIUM, True,  True,  0.805, STATUS_REPORT),
    (CONF_LOW,    False, False, 0.450, STATUS_OBSERVE),
    (CONF_LOW,    True,  False, 0.270, STATUS_DISCARD),
    (CONF_LOW,    True,  True,  0.517, STATUS_OBSERVE),
]


class TestThresholdsArePinned(unittest.TestCase):

    def test_threshold_literals_match_module(self):
        from arl_lite.core import confidence as C
        self.assertEqual(C.THRESHOLD_REPORT, THRESHOLD_REPORT_EXPECTED)
        self.assertEqual(C.THRESHOLD_OBSERVE, THRESHOLD_OBSERVE_EXPECTED)

    def test_base_prior_literals_match_module(self):
        from arl_lite.core import confidence as C
        self.assertEqual(C._BASE_PRIOR, _BASE_PRIOR_EXPECTED)

    def test_report_threshold_above_observe(self):
        """两个阈值写反的话整张表全废 —— 单独钉一下它们的大小关系"""
        self.assertGreater(THRESHOLD_REPORT_EXPECTED, THRESHOLD_OBSERVE_EXPECTED)


class TestTruthTable(unittest.TestCase):

    def test_all_nine_combinations(self):
        """9 组组合的分数与处置,逐个对字面量"""
        for base, xref, sat, want_score, want_status in TRUTH_TABLE:
            with self.subTest(base=base, has_cross_ref=xref, cross_ref_satisfied=sat):
                a = assess("rule", base, has_cross_ref=xref, cross_ref_satisfied=sat)
                self.assertAlmostEqual(a.score, want_score, places=3,
                                       msg=f"{base}/xref={xref}/sat={sat} 分数变了")
                self.assertEqual(a.status, want_status,
                                 msg=f"{base}/xref={xref}/sat={sat} 处置变了")

    def test_truth_table_has_no_duplicates(self):
        """9 行里若有重复行,上面那条测试的覆盖是虚的"""
        keys = [(b, x, s) for b, x, s, _, _ in TRUTH_TABLE]
        self.assertEqual(len(keys), 9)
        self.assertEqual(len(set(keys)), 9, "真值表有重复组合,覆盖不到")

    def test_boundary_rows_are_exactly_on_threshold(self):
        """两行压线是已知脆弱点(文档里有记)—— 它们必须仍然压线。

        如果哪天 base_prior 被"顺便取整",这两行会静默翻档,
        而主测试仍会因为期望值被同步改过而绿。这条专门盯住它:
        分数变了但仍恰好等于阈值,是危险方向。
        """
        on_line = [(b, s, want) for b, x, s, want, _ in TRUTH_TABLE
                   if want in (THRESHOLD_REPORT_EXPECTED, THRESHOLD_OBSERVE_EXPECTED)]
        self.assertTrue(on_line, "预期的压线组合不见了,先搞清楚发生了什么")
        for base, _sat, want in on_line:
            a = assess("rule", base, has_cross_ref=False)
            self.assertAlmostEqual(a.score, want, places=3)
            self.assertGreaterEqual(a.score, want,
                                    "压线行掉到阈值下方了 —— 处置已经翻转")


class TestFactorValues(unittest.TestCase):

    def test_cross_evidence(self):
        for (xref, sat), want in _CROSS_EVIDENCE_EXPECTED.items():
            with self.subTest(has_cross_ref=xref, cross_ref_satisfied=sat):
                a = assess("r", CONF_MEDIUM, has_cross_ref=xref, cross_ref_satisfied=sat)
                self.assertAlmostEqual(a.factors["cross_evidence"], want)

    def test_table_complexity(self):
        for n, want in _TABLE_COMPLEXITY_EXPECTED.items():
            with self.subTest(matched_table_count=n):
                a = assess("r", CONF_MEDIUM, matched_table_count=n)
                self.assertAlmostEqual(a.factors["table_complexity"], want)

    def test_exclusion_bonus_direction(self):
        """带排除项**才加分**。初稿文档写成「带 1.0 / 不带递减」,反了。"""
        for has_excl, want in _EXCLUSION_EXPECTED.items():
            with self.subTest(has_exclusion=has_excl):
                a = assess("r", CONF_MEDIUM, has_exclusion=has_excl)
                self.assertAlmostEqual(a.factors["exclusion_bonus"], want)
        self.assertGreater(_EXCLUSION_EXPECTED[True], _EXCLUSION_EXPECTED[False])

    def test_exclusion_is_what_saves_the_high_tier_three_table_case(self):
        """exclusion_bonus 不是装饰 —— 但它**极窄**

        27 组成对比较里只有 **1 组**处置会因 has_exclusion 翻转。
        初测时我挑了 medium/无 cross_ref 想验证,结果两组都是 report,
        差点把这个测试写成假的。真实翻转点是 high 档 + cross_ref
        声明了但没满足 + 跨 3 张表:0.432(discard) → 0.454(observe)。

        窄不代表可以删。分数变了 → 落库的 `confidence` 整数变了
        → 报告按置信度排序时位置变了。真要删因子,得先承认排序会变。
        """
        without = assess("r", CONF_HIGH, has_cross_ref=True, cross_ref_satisfied=False,
                         matched_table_count=3)
        with_excl = assess("r", CONF_HIGH, has_cross_ref=True, cross_ref_satisfied=False,
                           has_exclusion=True, matched_table_count=3)
        self.assertEqual(without.status, STATUS_DISCARD)
        self.assertEqual(with_excl.status, STATUS_OBSERVE,
                         "high/cross_ref未满足/3表 这一组是 1.05 唯一能翻转处置的组合,变了就说明因子动了")
        self.assertAlmostEqual(without.score, 0.432, places=3)
        self.assertAlmostEqual(with_excl.score, 0.454, places=3)

    def test_exclusion_flips_exactly_one_combination(self):
        """把"只翻一组"钉住。因子一改,这条会告诉你影响面变大了多少"""
        flips = []
        for base in (CONF_HIGH, CONF_MEDIUM, CONF_LOW):
            for xref, sat in ((False, False), (True, False), (True, True)):
                for tc in (1, 2, 3):
                    wo = assess("r", base, has_cross_ref=xref, cross_ref_satisfied=sat,
                                has_exclusion=False, matched_table_count=tc)
                    we = assess("r", base, has_cross_ref=xref, cross_ref_satisfied=sat,
                                has_exclusion=True, matched_table_count=tc)
                    if wo.status != we.status:
                        flips.append((base, xref, sat, tc))
        self.assertEqual(flips, [(CONF_HIGH, True, False, 3)],
                         f"exclusion 的影响面变了,现在是 {flips}")


class TestStoredIntAgreesWithStatus(unittest.TestCase):
    """坑四:落库同时写 `confidence=round(score*100)` 和 `confidence_status`

    两者独立算出。r22 枚举 54 种组合实测零错位(因子离散,落不进夹缝),
    但那是巧合不是保证 —— 因子一被微调,缝隙就开。这条守住现状。
    """

    def test_no_disagreement_across_all_reachable_combinations(self):
        mismatches = []
        for base in (CONF_HIGH, CONF_MEDIUM, CONF_LOW):
            for xref, sat in ((False, False), (True, False), (True, True)):
                for tc in (1, 2, 3):
                    for excl in (False, True):
                        a = assess("r", base, has_cross_ref=xref, cross_ref_satisfied=sat,
                                   has_exclusion=excl, matched_table_count=tc)
                        stored = round(a.score * 100)  # correlation_engine:468
                        if stored >= 70:
                            implied = STATUS_REPORT
                        elif stored >= 45:
                            implied = STATUS_OBSERVE
                        else:
                            implied = STATUS_DISCARD
                        if implied != a.status:
                            mismatches.append(
                                (base, xref, sat, tc, excl, a.score, stored, a.status, implied))
        self.assertEqual(mismatches, [],
                         "confidence 与 confidence_status 对不上,用户会按数字误判处置")


class TestDocTableMatchesCode(unittest.TestCase):
    """`docs/CONFIDENCE_VS_RISK.md` 那张表必须和 `assess()` 一致

    这条是本轮真正的产出。初稿文档宣称「实测」但数字是手抄的,
    9 行错 6 行,还和自己在坑一的结论互相矛盾(阈值 0.4 vs 0.42=discard)。
    没有这条测试,下一次手抄还会错,而且照样能写上「实测」。

    文档表格被重排/改列数 -> 这里**故意**失败:
    表格一旦不能被逐格核对,文档里的数字就退回了"没人验证过"的状态。
    """

    _XREF = {"无": False, "有": True}
    _SAT = {"—": False, "-": False, "否": False, "是": True}

    @staticmethod
    def _plain(cell: str) -> str:
        """剥掉 markdown 强调/反引号 —— 表格里 `**否**` 是个真值,
        不是两个星号加一个'否'。粗体只表强调,不该影响语义。"""
        return cell.replace("`", "").replace("*", "").replace(" ", "").strip()

    def _parse_doc_table(self) -> list[tuple]:
        if not DOC.is_file():
            self.fail(f"文档不存在:{DOC}")
        text = DOC.read_text(encoding="utf-8")

        m = re.search(r"^##\s*实测.*?$", text, re.M)
        if not m:
            self.fail("找不到 '## 实测' 标题 —— 表格锚点变了,请同步改本测试")

        rows = []
        for line in text[m.end():].splitlines():
            line = line.strip()
            if not line.startswith("|"):
                if rows:
                    break          # 表格结束
                continue
            cells = [self._plain(c) for c in line.strip("|").split("|")]
            if len(cells) < 5:
                continue
            if set("".join(cells)) <= set("-: "):   # 分隔行
                continue
            if cells[0] in ("base", "---"):
                continue
            rows.append(cells)
        return rows

    def test_doc_table_row_count(self):
        rows = self._parse_doc_table()
        self.assertEqual(len(rows), 9,
                         f"文档表格应有 9 行,实得 {len(rows)}: "
                         f"{[r[0] for r in rows]}")

    def test_doc_scores_and_statuses_match_assess(self):
        rows = self._parse_doc_table()
        self.assertEqual(len(rows), 9, "行数不对,先修表格结构")
        for cells in rows:
            base_raw, xref_raw, sat_raw, score_raw, status_raw = cells[:5]
            base = {"high": CONF_HIGH, "medium": CONF_MEDIUM, "low": CONF_LOW}.get(base_raw)
            self.assertIsNotNone(base, f"未知 base 档位:{base_raw!r}")
            self.assertIn(xref_raw, self._XREF, f"未知 cross_ref 取值:{xref_raw!r}")
            self.assertIn(sat_raw, self._SAT, f"未知「满足」取值:{sat_raw!r}")

            status = status_raw.replace("`", "").replace("*", "")
            a = assess("rule", base,
                       has_cross_ref=self._XREF[xref_raw],
                       cross_ref_satisfied=self._SAT[sat_raw])

            with self.subTest(base=base_raw, xref=xref_raw, sat=sat_raw):
                self.assertAlmostEqual(
                    float(score_raw), a.score, places=3,
                    msg=f"文档写的分数 {score_raw} 与实测 {a.score} 不符")
                self.assertEqual(
                    status, a.status,
                    msg=f"文档写的处置 {status} 与实测 {a.status} 不符")

    def test_doc_states_the_real_observe_threshold(self):
        """初稿把 observe 下界写成 0.4(实际 0.45)—— 那个 0.05 正好
        决定 0.42 是 discard 还是 observe,写错会翻转最关键那行的结论。"""
        text = DOC.read_text(encoding="utf-8")
        m = re.search(r"^##\s*实测.*?$", text, re.M)
        section = text[m.end():] if m else ""
        # 处置阈值小节在实测表之前
        head = text[:m.start()] if m else text
        self.assertIn(f"{THRESHOLD_OBSERVE_EXPECTED:.2f}", head,
                      "文档的 observe 阈值小节没有写出真实阈值 0.45")
        self.assertNotIn(
            re.search(r"0\.4\s*[–-]\s*0\.7", head).group(0) if
            re.search(r"0\.4\s*[–-]\s*0\.7", head) else "\0",
            head, "文档仍写着 0.4–0.7 的 observe 区间(实际下界是 0.45)")


class TestDocFactorTable(unittest.TestCase):
    """文档「因子表」那四行也必须和代码一致

    ## 这段是被变异测试逼出来的

    加上本文件前 7 个变异里,有一个**存活**了:把文档因子表里的
    `exclusion_bonus` 改回初稿的错误写法(「带 1.0 / 不带递减」),
    15 条测试全绿。原因是 `TestDocTableMatchesCode` 只核对
    「9 组合实测表」,因子表不在它的解析范围内 —— 而**初稿就是
    在因子表那行写反的**。

    一个只覆盖了一半文档的检查,比没有检查更容易骗人:
    它让人以为「文档已经被守住了」。

    ## 本测试的局限(别夸大)

    它核对的是**每个因子行里出现了哪些数值**,不解析中文语义。
    所以「1.05 出现在 exclusion_bonus 那一行」能守住,
    但「带/不带的方向写对了」严格说没有完全守住 ——
    只靠「反方向的写法会漏掉 1.05」这个巧合拦着。
    真要做到语义级,得让文档里那张表由代码生成,而不是手写。
    """

    _FACTOR_ROWS = {
        "base_prior": ["0.9", "0.7", "0.45"],
        "cross_evidence": ["1.15", "0.6", "1.0"],
        "table_complexity": ["1.0", "0.9", "0.8"],
        "exclusion_bonus": ["1.05", "1.0"],
    }

    def _factor_row(self, name: str) -> str:
        text = DOC.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("|") and f"`{name}`" in line:
                return line
        self.fail(f"文档因子表里找不到 `{name}` 那一行")

    def test_every_factor_row_lists_its_real_values(self):
        for name, expected in self._FACTOR_ROWS.items():
            row = self._factor_row(name)
            for value in expected:
                with self.subTest(factor=name, value=value):
                    self.assertIn(value, row,
                                  f"文档 `{name}` 行缺少真实取值 {value}:{row.strip()}")

    def test_exclusion_row_not_reversed(self):
        """专盯存活的那个变异 —— 初稿的原始错误"""
        row = self._factor_row("exclusion_bonus")
        self.assertIn("1.05", row,
                      "`exclusion_bonus` 漏掉了 1.05,多半被写成了"
                      "「带 1.0 / 不带递减」这种反向描述(初稿的错误)")


class TestModuleDocstringNamesRealFactors(unittest.TestCase):
    """`arl_lite/core/confidence.py` 的模块 docstring 自己写过一套假因子

    r22 修文档时才发现:模块 docstring 写的是
    `base_prior × signal_factor × cross_evidence × temporal` ——
    **`signal_factor` 和 `temporal` 在代码里从不存在**,
    真实的 `table_complexity` / `exclusion_bonus` 一个没提。
    同一段还声称"任一因子为 0 则总分 0", 而实际四个因子都 >= 0.6。

    这很可能就是文档跟着写歪的来源: 写文档时读的是 docstring,
    而 docstring 在讲一个没实现过的模型。**描述会污染下游。**
    """

    _REAL_FACTORS = {"base_prior", "cross_evidence",
                     "table_complexity", "exclusion_bonus"}
    _PHANTOM_FACTORS = ("signal_factor", "temporal")

    def setUp(self):
        from arl_lite.core import confidence as C
        self.doc = C.__doc__ or ""

    def test_phantom_factors_are_gone(self):
        for name in self._PHANTOM_FACTORS:
            with self.subTest(phantom=name):
                # 允许出现在"这里原先写的是…从不存在"这类说明里,
                # 但不允许出现在那行公式本身。取公式行来判。
                formula = [ln for ln in self.doc.splitlines()
                           if "×" in ln and "=" in ln]
                self.assertTrue(formula, "模块 docstring 里找不到公式行")
                self.assertFalse(
                    any(name in ln for ln in formula),
                    f"公式行里仍出现不存在的因子 {name}:{formula}")

    def test_formula_line_names_all_real_factors(self):
        formula = [ln for ln in self.doc.splitlines() if "×" in ln and "=" in ln]
        self.assertTrue(formula, "模块 docstring 里找不到公式行")
        joined = " ".join(formula)
        for name in self._REAL_FACTORS:
            with self.subTest(factor=name):
                self.assertIn(name, joined,
                              f"公式行没有提到真实因子 {name}:{joined}")

    def test_factors_dict_keys_are_exactly_the_named_four(self):
        """assess() 实际吐出的因子名必须就是公式里那四个 —— 不多不少"""
        a = assess("r", CONF_MEDIUM, has_cross_ref=True, cross_ref_satisfied=True,
                   has_exclusion=True, matched_table_count=2)
        self.assertEqual(set(a.factors), self._REAL_FACTORS)

    def test_no_factor_is_zero(self):
        """docstring 曾称"任一因子为 0 则总分 0" —— 实测没有任何因子取到 0"""
        for base in (CONF_HIGH, CONF_MEDIUM, CONF_LOW):
            for xref, sat in ((False, False), (True, False), (True, True)):
                for tc in (1, 2, 3):
                    for excl in (False, True):
                        a = assess("r", base, has_cross_ref=xref, cross_ref_satisfied=sat,
                                   has_exclusion=excl, matched_table_count=tc)
                        for name, v in a.factors.items():
                            self.assertGreater(v, 0.0,
                                               f"{name} 取到了 0({base}/{xref}/{sat}/{tc}/{excl})")
                            self.assertLess(v, 1.3, f"{name} 异常大:{v}")


class TestStatusFallbackSemantics(unittest.TestCase):
    """`status_of` 的兜底口径 —— 脏数据/缺字段不许被静默藏起来

    这条是被变异测试逼出来的:把 `status_of` 里的 `.strip().lower()`
    去掉,15+ 条测试**全绿**。也就是说"脏数据按 observe 兜底"这个
    承诺,当时一行测试都没守着。

    而它不是小事 —— 库里确实存过大小写/空白不规整的值,
    一旦不归一,`"  Discard  "` 就既不是 report 也不是 discard,
    分流键 `_STATUS_RANK[...]` 直接 KeyError,整份报告生成失败。
    """

    # (原始值, 期望档位)
    CASES = [
        ("report", STATUS_REPORT),
        ("observe", STATUS_OBSERVE),
        ("discard", STATUS_DISCARD),
        # 归一:大小写与首尾空白
        ("  Discard  ", STATUS_DISCARD),
        ("DISCARD", STATUS_DISCARD),
        ("Report", STATUS_REPORT),
        # 缺失 / 空 -> observe
        (None, STATUS_OBSERVE),
        ("", STATUS_OBSERVE),
        ("   ", STATUS_OBSERVE),
        # 脏数据 -> observe(不藏起来,也不误判成 discard)
        ("maybe", STATUS_OBSERVE),
        ("0", STATUS_OBSERVE),
    ]

    def test_status_of_normalizes_and_falls_back(self):
        from arl_lite.core.confidence import status_of
        for raw, want in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(status_of({"confidence_status": raw}), want)

    def test_missing_key_behaves_like_none(self):
        from arl_lite.core.confidence import status_of
        self.assertEqual(status_of({}), STATUS_OBSERVE)

    def test_is_discarded_agrees_with_status_of(self):
        """两个函数不许对同一行给出不同答案 —— 它们分工不同但不该矛盾"""
        from arl_lite.core.confidence import is_discarded, status_of, STATUS_DISCARD
        for raw, want in self.CASES:
            with self.subTest(raw=raw):
                row = {"confidence_status": raw}
                self.assertEqual(is_discarded(row), status_of(row) == STATUS_DISCARD)


class TestOnlyConfidenceModuleReadsTheStatusField(unittest.TestCase):
    """架构约束:`confidence_status` 只许由 `core/confidence.py` 读

    ## 为什么这条要存在

    r22 把「discard 要不要排除」收敛成一个 `is_discarded()`,
    因为三处各写一遍判断时,漏改一处**不会报错**,只会安静地
    给出和另外两处对不上的数。

    但"收敛"本身是约定,不是强制。变异 15 把报告层改回内联判断
    (`(c.get("confidence_status") or "").strip().lower() != "discard"`),
    **48 条测试全绿** —— 行为等价,没测试会红。

    行为等价所以**当场无害**,可下一次有人改内联那一版时,
    两处就会分叉,而没人知道它们曾经共用过一份判断。

    ## 判据

    走 AST,不看文本 —— 文本匹配分不清「真的在读这个字段」
    和「只是在注释里解释这个字段」,而那正是本项目的铁律。
    约束一句话:**除 `core/confidence.py` 外,任何模块都不得
    直接读 `confidence_status`**;要判断请调 `status_of` / `is_discarded`。
    """

    _ALLOWED = "confidence.py"

    def _modules_reading_the_field(self) -> list[tuple[str, int]]:
        import ast
        hits = []
        for path in sorted((REPO / "arl_lite").rglob("*.py")):
            if path.name == self._ALLOWED:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # 形如 x.get("confidence_status")
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "confidence_status"):
                    hits.append((str(path.relative_to(REPO)), node.lineno))
                # 形如 row["confidence_status"]
                elif (isinstance(node, ast.Subscript)
                      and isinstance(node.slice, ast.Constant)
                      and node.slice.value == "confidence_status"):
                    hits.append((str(path.relative_to(REPO)), node.lineno))
        return hits

    def test_no_module_bypasses_the_shared_predicate(self):
        hits = self._modules_reading_the_field()
        self.assertEqual(
            hits, [],
            "这些模块直接读了 confidence_status,绕过了 core.confidence 的统一判断:"
            f"{hits}。要判断请调 status_of() / is_discarded() —— "
            "漏改一处不会报错,只会安静地给出对不上的数")


if __name__ == "__main__":
    unittest.main()
