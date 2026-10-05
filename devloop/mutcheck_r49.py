"""r49 变异测试:第二份去重键推导不许存在,也不许偷偷回来

同 r41~r48:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死 ——
变异把源文件写坏时 pytest 同样会红,但那和判据无关(r47 踩过)。

## 本轮特有的两个坑

一,**判据要比它守的事窄**。r49 第一版 AST 判据写成「`bulk_insert` 里不许
出现 `table == <资产表>` 的比较」,结果误报了 `COLUMN_ALIAS` 那处 ——
那是列名别名,和身份拼接是两回事。判据收窄成「分支体里有没有给
`row["hash"]` 赋值」,这才是它要守的东西。

二,**默认值是契约的一部分**。`add_finding` 的 `title` 形参默认 `""`,
所以 `bulk_insert` 省略 title 必须落成同一个 hash。变异专门盯这一条 ——
它是本轮**自己引入又修掉**的回归。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

STORAGE = "arl_lite/db/storage.py"
TEST = "tests/test_bulk_insert_identity.py"
FILTER_TEST = "tests/test_monitor_changes_filter.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

MUTANTS = [
    ("M1", "bulk_insert 绕开契约表自己拼 —— 第二份推导又回来了",
     [(STORAGE, """                        row["hash"] = asset_identity_of_row(
                            self.workspace_id, table, row)""",
        """                        row["hash"] = compute_hash(
                            str(self.workspace_id),
                            f"{row.get('host') or row.get('domain') or row.get('url') or ''}")""")]),

    ("M2", "findings 的默认值 `""` 去掉 —— 省略 title 从此报错",
     [(STORAGE, '    "findings": (("target", "finding_type", "title"), "|",\n'
                '                 (_REQUIRED, _REQUIRED, "")),',
        '    "findings": (("target", "finding_type", "title"), "|",\n'
        '                 (_REQUIRED, _REQUIRED, _REQUIRED)),')]),

    ("M3", "findings 的默认值改成 `\"未命名\"` —— 两条路落不到同一个 hash",
     [(STORAGE, '                 (_REQUIRED, _REQUIRED, "")),',
        '                 (_REQUIRED, _REQUIRED, "untitled")),')]),

    ("M4", "ports 的列名改回形参名 `host` —— bulk_insert 取不到值了",
     [(STORAGE, '    "ports": (("ip", "port"), ":", (_REQUIRED, _REQUIRED)),',
        '    "ports": (("host", "port"), ":", (_REQUIRED, _REQUIRED)),')]),

    ("M5", "缺必需身份列时用 `''` 顶上 —— 又回到「凑一个 hash」",
     [(STORAGE, """        elif default is _REQUIRED:
            raise ValueError(
                f"{table} 的行缺身份列 {name!r} —— 这一段没有默认值,"
                f"补上它,或者调用方是漏传了。")""",
        """        elif default is _REQUIRED:
            parts.append("")""")]),

    ("M6", "未知表不报错,退回按第一段算 —— 兜底回来了",
     [(STORAGE, """    spec = _ASSET_IDENTITY.get(table)
    if spec is None:
        raise ValueError(
            f"{table!r} 不在 _ASSET_IDENTITY 里,没法按行算身份。"
            f"要么它不是资产表(那就别给它算 hash),要么该往表里补一份定义。")""",
        """    spec = _ASSET_IDENTITY.get(table)
    if spec is None:
        return compute_hash(str(workspace_id), row.get("host", ""))""")]),

    ("M7", "段是 `None` 不再报错 —— `add_*` 少给一段会静默存成别的身份",
     [(STORAGE, """    for name, v in zip(names, identity):
        if v is None:""",
        """    for name, v in zip(names, identity):
        if False:""")]),

    ("M8", "按列名取时用 `row.get(n, \"\")` —— 缺列不再报错",
     [(STORAGE, """        if name in row:
            parts.append(row[name])
        elif default is _REQUIRED:""",
        """        if name in row:
            parts.append(row[name])
        elif False:""")]),

    ("M9", "分隔符换成竖线 —— ports 落成另一个资产",
     [(STORAGE, '    "ports": (("ip", "port"), ":", (_REQUIRED, _REQUIRED)),',
        '    "ports": (("ip", "port"), "|", (_REQUIRED, _REQUIRED)),')]),
]

# 反向:契约表不动,改**调用方**。这份漂移是 r49 的正题。
M_BACKWARD = [
    ("B1", "add_port 传错段 —— 入库那份和批量那份分家",
     [(STORAGE, "            identity=(host, port),", "            identity=(port, host),")]),

    ("B2", "add_finding 的 title 形参默认值改了,契约表没跟 —— 两条路不一致",
     [(STORAGE, "        title: str = \"\",", "        title: str = \"untitled\",")]),

    ("B3", "add_site 把 url 小写化 —— 和批量那份分家",
     [(STORAGE, "            identity=(url,),", "            identity=(url.lower(),),")]),
]

COVERAGE_MUTANTS = [
    # 期望**被杀**:去掉「两条路一致」那条,再改坏一边。真正兜住的是
    # 去重行为本身(第二条插不进去)以及逐类型的直接对账。
    ("C-two-paths", "去掉「add 与 bulk 必须去重」那条,同时改坏 add_site 的身份",
     [(TEST, "def test_add_and_bulk_insert_agree_on_the_hash(",
             "@pytest.mark.skip\ndef test_add_and_bulk_insert_agree_on_the_hash("),
      (STORAGE, "            identity=(url,),", "            identity=(url.lower(),),")], False),

    # 期望**被杀,但死在另一道闸**:拿掉契约表里的默认值之后,还有
    # `test_defaults_come_from_the_add_signatures_not_a_second_table`
    # 拿 `add_finding` 的形参签名做对照,会发现对不上。如实记着:那条
    # 才是真正守默认值的,「省略 title 落成同一个 hash」是行为层的额外一层。
    ("C-default", "去掉「省略有默认值的段」那条,同时把默认值从契约表拿掉",
     [(TEST, "def test_omitting_a_part_with_a_default_lands_on_the_same_hash(",
             "@pytest.mark.skip\ndef test_omitting_a_part_with_a_default_lands_on_the_same_hash("),
      (STORAGE, '                 (_REQUIRED, _REQUIRED, "")),',
                '                 (_REQUIRED, _REQUIRED, _REQUIRED)),')], False),

    # 期望**被杀,但死在另一道闸**:加回来的分支直接调 `compute_hash`,
    # 于是 `test_compute_hash_is_only_called_from_the_one_place`
    # 数出两处调用就红了。AST 那条判的是「结构形状」,算调用数那条判的是
    # 「有没有绕过契约表」—— 两者不是一回事,如实记着谁兜住的。
    ("C-ifelse", "去掉 AST 那条,同时把按表名分支的推导加回来",
     [(TEST, "def test_bulk_insert_has_no_identity_if_else_chain():",
             "@pytest.mark.skip\ndef test_bulk_insert_has_no_identity_if_else_chain():"),
      (STORAGE, """                        row["hash"] = asset_identity_of_row(
                            self.workspace_id, table, row)""",
        """                        if table == "hosts":
                            row["hash"] = compute_hash(
                                str(self.workspace_id), row.get("host", ""))
                        else:
                            row["hash"] = asset_identity_of_row(
                                self.workspace_id, table, row)""")], False),
]

TOUCHED = {STORAGE, TEST, FILTER_TEST}


def run_tests() -> tuple[bool, bool]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST, FILTER_TEST,
         "-q", "-p", "no:warnings"],
        capture_output=True, text=True, env=env)
    broken = any(s in p.stdout for s in _BROKEN_SOURCE)
    return p.returncode == 0, broken


def apply(edits) -> bool:
    for path, old, new in edits:
        s = open(path, encoding="utf-8").read()
        if old not in s:
            return False
        open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
    return True


def revert():
    for path in TOUCHED:
        bak = path + ".mutbak"
        if os.path.exists(bak):
            shutil.copy(bak, path)


def sweep(mutants, expect_default: bool) -> list[str]:
    survived = []
    for entry in mutants:
        mid, desc, edits = entry[0], entry[1], entry[2]
        expect_survival = entry[3] if len(entry) > 3 else expect_default
        if not apply(edits):
            print(f"[{mid}] !! 变异没打上(原串不匹配) —— 变异本身失效了")
            survived.append(mid + "(没打上)")
            continue
        passed, broken = run_tests()
        revert()
        if broken:
            print(f"[{mid}] !! 变异把源文件写成了语法错误 —— 这种红不算杀死。{desc}")
            survived.append(mid + "(假杀:语法错误)")
            continue
        killed = not passed
        ok = (not killed) if expect_survival else killed
        print(f"[{mid}] {('杀死' if killed else '*** 存活 ***')}  {desc}")
        if not ok:
            survived.append(mid)
    return survived


def main() -> int:
    for p in TOUCHED:
        shutil.copy(p, p + ".mutbak")
    pristine = {p: open(p, "rb").read() for p in TOUCHED}
    bad = []
    try:
        if not run_tests()[0]:
            print("[对照] 基线就红,测不了变异")
            return 1
        print("[对照] 未变异时全通过 —— 符合预期\n")
        print("── 改契约表/兜底:存活 = 测试有洞 ──")
        bad += sweep(MUTANTS, expect_default=False)
        print("\n── 改调用方(反向变异):两份推导开始分家 ──")
        bad += sweep(M_BACKWARD, expect_default=False)
        print("\n── 同时改测试和生产代码:回答「哪条测试在守这里」 ──")
        bad += sweep(COVERAGE_MUTANTS, expect_default=True)
    finally:
        revert()
        for p in TOUCHED:
            if os.path.exists(p + ".mutbak"):
                os.remove(p + ".mutbak")
        left = [p for p in sorted(TOUCHED)
                if open(p, "rb").read() != pristine[p]]
        if left:
            print(f"!! 复原后这些文件的内容变了:{left}")
            return 1
    print()
    if bad:
        print(f"不符合预期 {len(bad)} 个:{bad}")
        return 1
    print(f"{len(MUTANTS) + len(M_BACKWARD)} 个实现变异全杀,"
          f"{len(COVERAGE_MUTANTS)} 个覆盖变异结果均符合预期")
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
