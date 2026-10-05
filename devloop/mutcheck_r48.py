"""r48 变异测试:名字 → hash 必须真的是那一个来源

同 r41~r47:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy —— r42 那版用 `shutil.move`,第一次复原就把备份搬走了,
后面每个变异的「复原」全是空操作,变异原地留在工作树里。

## 假杀:变异把文件改成语法错误,pytest 也会红

r47 踩过:替换串吞掉 dict 字面量的尾逗号,`cli.py` 变成 SyntaxError,
pytest 在 collection 阶段就红。那种红和判据无关 —— 文件 import 不了,
一条测试都没跑,却会被记成「这条测试有用」。命中
`SyntaxError` / `IndentationError` / `TabError` / `ERROR collecting`
一律判「变异无效」,不算杀死。

## 本轮最要紧的一条:反向变异

`M_FORWARD` 改契约表(存储侧),`M_BACKWARD` 改**消费侧**。
只做正向的话,「CLI 自己另拼一份 hash」这种漂移照样能溜过去 ——
而那正是 r45 那条教训的重演。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

CLI = "arl_lite/cli.py"
STORAGE = "arl_lite/db/storage.py"
MONITOR = "arl_lite/core/monitor.py"
TEST = "tests/test_monitor_changes_filter.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

# 契约表在 storage.py 里的三行,改分隔符就是改「同一个资产是谁」
IDENT_LINES = '''    "domains": (("domain",), ""),
    "hosts": (("host",), ""),
    "ports": (("host", "port"), ":"),
    "sites": (("url",), ""),
    "findings": (("target", "finding_type", "title"), "|"),'''

MUTANTS = [
    ("M1", "ports 的分隔符从冒号换成竖线 —— 同一个端口算出另一个 hash",
     [(STORAGE, '    "ports": (("host", "port"), ":"),',
               '    "ports": (("host", "port"), "|"),')]),

    ("M2", "findings 的分隔符换掉 —— 三段拼法变了",
     [(STORAGE, '    "findings": (("target", "finding_type", "title"), "|"),',
               '    "findings": (("target", "finding_type", "title"), ":"),')]),

    ("M3", "hosts 的身份漏掉 host 段 —— `add_host` 立刻报段数不对",
     [(STORAGE, '    "hosts": (("host",), ""),', '    "hosts": (),')]),

    ("M4", "段数不对时改成凑合算 —— 少一段也能算出 16 位 hash",
     [(STORAGE, '''    if len(identity) != len(names):
        raise ValueError(''', '''    if False:
        raise ValueError(''')]),

    ("M5", "表名不认识时静默退回 compute_hash —— 打错表名算出一个永远查不到的 hash",
     [(STORAGE, '''    spec = _ASSET_IDENTITY.get(table)
    if spec is None:
        raise ValueError(
            f"unknown asset table: {table!r} "
            f"(known: {', '.join(sorted(_ASSET_IDENTITY))})")''',
        '''    spec = _ASSET_IDENTITY.get(table)
    if spec is None:
        return compute_hash(str(workspace_id), identity[0] if identity else "")''')]),

    ("M6", "workspace_id 不进 hash —— 看起来一样,其实少了一段输入",
     [(STORAGE, "    return compute_hash(str(workspace_id), sep.join(str(p) for p in identity))",
               "    return compute_hash(sep.join(str(p) for p in identity))")]),

    ("M7", "port 从**左边**切第一个冒号 —— IPv6 地址被切烂",
     [(CLI, '        host, sep, port = value.rpartition(":")',
            '        host, sep, port = value.partition(":")')]),

    ("M8", "port 不校验就拼 —— 写法不对时静默算出一个查不到的 hash",
     [(CLI, '''        if not sep or not host or not port.isdigit():
            raise ValueError(
                f"port 的 --asset 写法是 host:port(比如 1.1.1.1:443),"
                f"给的是 {value!r}")
        return (host, port)''', "        return (host, port)")]),

    ("M9", "给名字却没给 --type 时静默当 hash —— 返回空结果,用户以为没变过",
     [(CLI, '''        if len(v) == 16 and all(c in string.hexdigits for c in v):
            return v
        raise ValueError(
            f"--asset {value!r} 不带 --type 时只能是 16 位资产 hash;"
            f"要按名字过滤请加 --type(如 --type host)")''',
        '''        return v''')]),

    ("M10", "findings 按名字改成前缀匹配 —— 拼错格式也算命中,静默",
     [(CLI, '''    raise ValueError(
        f"--type {asset_type} 不支持按名字过滤(身份是多段复合的),"
        f"请直接给 16 位资产 hash")''',
        '''    return (value,)''')]),

    ("M11", "`list_changes` 漏掉 asset_hash 条件 —— --asset 完全不生效",
     [(MONITOR, '''    if asset_hash:
        sql += " AND asset_hash = ?"
        params.append(asset_hash)''', "    pass")]),

    ("M12", "`list_changes` 漏掉 workspace_id 条件 —— 工作区之间互相串",
     [(MONITOR, '''    sql = "SELECT * FROM asset_changes WHERE workspace_id = ?"
    params: list = [storage.workspace_id]''',
        '''    sql = "SELECT * FROM asset_changes"
    params: list = []''')]),

    ("M13", "CLI 绕开契约表自己拼 hash —— 漂移的正路(r45 教训的重演)",
     [(CLI, "    return compute_asset_hash(storage.workspace_id, table, *parts)",
            "    from .db.storage import compute_hash\n"
            "    return compute_hash(str(storage.workspace_id), parts[0])")]),

    ("M14", "死参数 hash_key 悄悄回来 —— 签名在,但仍然没人读",
     [(STORAGE, """        identity: tuple,
        fields: dict,""", """        identity: tuple,
        hash_key: str,
        fields: dict,""")]),
]

# 反向:改**消费侧**,契约表不动。
# 「CLI 自己另拼一份」和「库里那份与契约表脱节」是本轮最该被拦的两种漂移。
M_BACKWARD = [
    ("B1", "add_host 的 identity 传错段 —— 入库的 hash 和契约表说的不是一回事",
     [(STORAGE, "            identity=(host,),\n            fields={\n"
                '                "host": host,',
               '            identity=(host.lower(),),\n            fields={\n'
                '                "host": host,')]),

    ("B2", "add_port 把 port 换成字符串再传 —— 段的值变了 hash 就变了",
     [(STORAGE, "            identity=(host, port),",
               "            identity=(host, str(port) + \" \"),")]),

    ("B3", "add_site 传 url.lower() —— URL 大小写不同被当成两个资产",
     [(STORAGE, "identity=(url,),\n", "identity=(url.lower(),),\n")]),
]

# 覆盖变异:同时改测试和生产代码,回答「哪条测试在守这个资产类型」。
# 存活 = 那条测试是承重的(想要的结果);被杀 = 还有别处兜着。
COVERAGE_MUTANTS = [
    # 期望**被杀**,而且是被另一道闸杀的:从契约表删掉 `sites` 之后,
    # `add_site` 会先撞上 `compute_asset_hash` 的段数/表名检查而抛 ValueError,
    # 大量用例会直接报错。说明真正兜住「契约表被改坏」的是那道运行时检查,
    # 键集合那条测试只是**额外**的一层,不是唯一防线 —— 如实记着,
    # 不假装它是承重的。
    ("C-contract-keys", "从契约表删掉 sites(键集合那条测试被 skip 掉)",
     [(TEST, "def test_identity_keys_are_exactly_the_monitor_asset_tables():",
             "@pytest.mark.skip\ndef test_identity_keys_are_exactly_the_monitor_asset_tables():"),
      (STORAGE, '    "sites": (("url",), ""),\n', "")], False),

    ("C-stored-hash", "去掉「存的 hash == 算出来的 hash」那条,"
                      "同时改坏 add_finding 的身份段",
     [(TEST, "def test_finding_stored_hash_equals_compute_asset_hash(ws):",
             "@pytest.mark.skip\ndef test_finding_stored_hash_equals_compute_asset_hash(ws):"),
      (STORAGE, "            identity=(target, finding_type, title),",
                "            identity=(target, finding_type, title.lower()),")], True),

    # 这里第一版把 `WHERE workspace_id = ?` 整段删掉,结果是拼出
    # `SELECT * FROM asset_changes AND ...` —— SQL 直接语法错、进程崩。
    # 那不算「隔离失效」,那只是代码坏了。恒真的 `WHERE ? IS NOT NULL`
    # 才是真实的退化路径:查询合法、不崩,只是**不再按工作区筛**。
    ("C-workspace", "去掉「工作区之间互不可见」那条,同时让 workspace_id 条件恒真",
     [(TEST, "def test_filtering_in_one_workspace_never_sees_the_other(",
             "@pytest.mark.skip\ndef test_filtering_in_one_workspace_never_sees_the_other("),
      (MONITOR, '''    sql = "SELECT * FROM asset_changes WHERE workspace_id = ?"
    params: list = [storage.workspace_id]''',
        '''    sql = "SELECT * FROM asset_changes WHERE ? IS NOT NULL"
    params: list = [storage.workspace_id]''')], True),
]

TOUCHED = {CLI, STORAGE, MONITOR, TEST}


def run_tests() -> tuple[bool, bool]:
    """返回 (全通过, 变异是否把源文件写坏了)

    第二个值必需:变异改坏语法时 pytest 同样会红,但那和判据无关。
    """
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST, "-q", "-p", "no:warnings"],
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
            print(f"[{mid}] !! 变异把源文件写成了语法错误 —— 这种红不算杀死。"
                  f"{desc}")
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
        print("── 改契约表/消费侧:存活 = 测试有洞 ──")
        bad += sweep(MUTANTS, expect_default=False)
        print("\n── 改消费侧(反向变异):同一张表开始漂 ──")
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
    raise SystemExit(mutkit.locked(main))
