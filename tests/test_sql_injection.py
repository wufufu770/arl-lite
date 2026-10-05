"""check_filter_sql 的注入防护测试

这个防护防的是**已实测确认的真实漏洞**, 不是假想:

    filter = "1=1) UNION SELECT title,1,99 FROM findings --"
    → 拼进 SELECT * FROM domains WHERE workspace_id=? AND (<filter>) ORDER BY id
    → 括号闭合 + 注释吃掉尾部 → findings 表内容被 UNION 进结果集
    → 实测能读到别表的 title

写操作关键字(DROP/DELETE/...)拦不住它, 因为 UNION 是读操作。
所以修了两处:
  1. 关键字表加 UNION
  2. 新增注释剥离 —— `un/**/ion` 会被 \\b 词边界当成两个词放行
"""
from __future__ import annotations

import unittest

from arl_lite.db.storage import check_filter_sql, strip_sql_comments, strip_sql_literals


class TestCommentStripping(unittest.TestCase):

    def test_line_comment_removed(self):
        """行注释吃到换行为止, 换行本身保留"""
        out = strip_sql_comments("a -- b\nc")
        self.assertNotIn("b", out, "注释内容未被剥离")
        self.assertIn("a", out)
        self.assertIn("c", out)

    def test_block_comment_removed(self):
        self.assertNotIn("union", strip_sql_comments("un/**/ion").lower())

    def test_offsets_preserved(self):
        """替换成空格而非空串——保持长度, 报错列号才对得上原文"""
        src = "a/*xx*/b"
        self.assertEqual(len(strip_sql_comments(src)), len(src))

    def test_comment_chars_inside_literal_preserved(self):
        """引号里的 -- 不是注释

        顺序反了会把 domain = '--x' 截断成 domain = '     ',
        然后字面量保护失效, 里面的内容会暴露给关键字扫描。
        """
        out = strip_sql_literals("domain = '--not a comment'")
        self.assertIn("?", out)
        self.assertNotIn("not a comment", out)


class TestInjectionBlocked(unittest.TestCase):
    """已实测可泄漏的 payload —— 每一条都必须拦住"""

    ATTACKS = [
        # 真实泄漏 payload(UNION 读别表)
        "1=1) UNION SELECT title,1,99 FROM findings --",
        "1=1)UNION SELECT 1",
        "1=1 UNION SELECT 1",
        "domain = 'a' UNION SELECT 1",
        # 注释分隔关键字绕过
        "1=1) un/**/ion SELECT 1",
        "1=1) un\nion select 1",
        "x=1) unio/**/n sel/**/ect 1 --",
        # 注释用来吃掉尾部
        "1=1) /*c*/ DROP TABLE domains --",
        "1=1) DR/**/OP TABLE domains",
    ]

    def test_all_attacks_blocked(self):
        missed = []
        for payload in self.ATTACKS:
            try:
                check_filter_sql(payload)
                missed.append(payload)
            except ValueError:
                pass
        self.assertEqual(missed, [], f"漏过: {missed}")

    def test_union_specifically_named_in_error(self):
        """报错要指名关键字, 否则使用者不知道怎么改"""
        with self.assertRaises(ValueError) as ctx:
            check_filter_sql("1=1) UNION SELECT 1")
        self.assertIn("UNION", str(ctx.exception))


class TestLegitimateFiltersPass(unittest.TestCase):
    """防护不能变成"什么都拦"——那等于没防护

    退化路径:为了堵住 UNION, 有人把 \b 边界去掉, 结果
    `domain LIKE '%union%'` 这种正常查询被误杀。
    """

    LEGIT = [
        "domain LIKE '%union%'",        # 字面量里含关键字
        "domain LIKE '%UNIONED%'",      # 前缀
        "source = 'unionization'",     # 词内包含
        "domain LIKE '%drop%'",
        "domain LIKE '%update%'",
        "severity IN ('critical','high')",
        "source='crtsh'",
        "title != ''",
        "risk >= 7",
        "detected_at > '2026-01-01'",
        "domain NOT LIKE '%.internal.example.com'",
    ]

    def test_all_legit_pass(self):
        rejected = []
        for f in self.LEGIT:
            try:
                check_filter_sql(f)
            except ValueError as e:
                rejected.append(f"{f!r} -> {e}")
        self.assertEqual(rejected, [], f"误伤正常查询: {rejected}")


class TestExistingGuardsStillWork(unittest.TestCase):
    """加固不能削弱原有防护"""

    def test_semicolon_still_blocked(self):
        with self.assertRaises(ValueError):
            check_filter_sql("domain='a'; DROP TABLE domains")

    def test_literal_does_not_trigger_keyword_scan(self):
        """字面量内容不参与关键字扫描, 否则 LIKE '%drop%' 会被误杀"""
        check_filter_sql("domain LIKE '%drop%'")

    def test_real_write_keywords_still_blocked(self):
        for kw in ("DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "PRAGMA", "ATTACH"):
            with self.assertRaises(ValueError, msg=f"{kw} 未被拦截"):
                check_filter_sql(f"1=1; {kw} TABLE domains")


if __name__ == "__main__":
    unittest.main()
