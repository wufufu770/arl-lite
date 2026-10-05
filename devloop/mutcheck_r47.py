"""r47 变异测试:标识列名要有真实退化路径

同 r41~r46:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy —— r42 那版用 `shutil.move`,第一次复原就把备份搬走了,
后面每个变异的「复原」全是空操作,变异原地留在工作树里。

## 假杀:变异把文件改成语法错误,pytest 也会红

第一轮跑出来 C-domain / C-site「被杀」,手工复核却全绿 —— 替换串
`("domain",),` → `("resolved_ip",)` 把 dict 字面量的**尾逗号**吞了,
`cli.py` 变成 `{"domain": ("resolved_ip",) "host": ...}`,pytest 在
collection 阶段就报 SyntaxError。那种红和判据无关:文件根本 import 不了,
测试一条都没跑。**比没测更坏** —— 它会被记成「这条测试有用」。

所以 `run_tests()` 额外看输出:命中 SyntaxError / IndentationError /
collection error 一律判成「变异无效」,不算杀死。

## 两组变异,问的问题不一样

`MUTANTS`      :改生产代码。**存活 = 测试有洞**,脚本失败。
`COVERAGE_MUTANTS`:同时改测试参数**和**生产代码,目的是回答
    「到底是哪条测试在守着这个资产类型」。**存活 = 正常且是想要的结果** ——
    它证明那条测试是承重的,不是凑数的。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

CLI = "arl_lite/cli.py"
TEST = "tests/test_change_label_columns.py"

# 整个 except 体(r47 的修法本体)
HANDLER = '''        except Exception as e:      # noqa: BLE001
            # 静默降级比降级本身更坏。实测:标识列名写错 → SQL 抛
            # OperationalError → 旧的 `except: pass` 全吞 → 输出退化成
            # hash 前缀,全程零警告,用户只会以为「标识本来就长这样」。
            # 注意这和「行不存在」(`got is None`)是两回事:后者是正常
            # 的(资产可能已删),走下面的快照兜底,不算异常。
            log.warning(
                "monitor changes: 读 %s 的标识列 %s 失败(%s: %s),"
                "标识回退到快照/hash —— 列名写错或表结构变了?",
                table, ", ".join(fields), type(e).__name__, e)
            got = None'''

MUTANTS = [
    ("M1", "警告降级成 debug —— 等于回到静默",
     [(CLI, "            log.warning(\n"
            '                "monitor changes: 读 %s 的标识列',
            "            log.debug(\n"
            '                "monitor changes: 读 %s 的标识列')]),

    ("M2", "整段恢复成 r46 之前的 `except: pass` —— 本轮要修的退化路径",
     [(CLI, HANDLER, "        except Exception:\n            pass")]),

    ("M3", "警告里不说是哪张表 —— 人拿到警告也不知道去哪儿查",
     [(CLI, '                table, ", ".join(fields), type(e).__name__, e)',
            '                ", ".join(fields), type(e).__name__, e)')]),

    ("M4", "警告里不说是哪一列 —— 正是列名写错才要查的东西",
     [(CLI, '                table, ", ".join(fields), type(e).__name__, e)',
            '                table, fields[0], type(e).__name__, e)')]),

    ("M5", "不判 got —— 行不存在时直接崩,而不是静默退到 hash",
     [(CLI, "        if got:                      # None = 行不存在,正常,静默往下退",
            "        if True:")]),

    ("M6", "只捕获 ZeroDivisionError —— OperationalError 直接往上抛崩掉",
     [(CLI, "        except Exception as e:      # noqa: BLE001",
            "        except ZeroDivisionError as e:")]),

    ("M7", "host 标识列顺序换过来 —— 标识退化成会变的 ip",
     [(CLI, '    "host": ("host", "ip"),', '    "host": ("ip", "host"),')]),

    ("M8", "port 标识列去掉 ip —— 端口号顶替了 ip 身份",
     [(CLI, '    "port": ("ip", "port", "protocol"),',
            '    "port": ("port", "protocol"),')]),

    ("M9", "finding 标识列去掉 cve —— 标题顶替了 CVE 身份",
     [(CLI, '    "finding": ("cve", "title", "description"),',
            '    "finding": ("title", "description"),')]),

    ("M10", "domain 标识列换成 resolved_ip —— 用户看到的是 IP 不是域名",
     [(CLI, '    "domain": ("domain",),', '    "domain": ("resolved_ip",),')]),

    ("M11", "site 标识列换成 host —— URL 不见了",
     [(CLI, '    "site": ("url",),', '    "site": ("host",),')]),

    ("M12", "查表彻底关掉 —— 标识全靠快照,退化成会变的字段",
     [(CLI, "    table = _ASSET_TYPES_TO_TABLE.get(at)\n    if table and fields:",
            "    table = None\n    if table and fields:")]),
]

# 每一条 = 「去掉这个参数的测试 + 改坏这个类型的标识列」。
# 存活说明:除了被去掉的那条,没有任何测试在守这个资产类型。
# 最后一项是**期望**的存活与否,而不是一刀切。
COVERAGE_MUTANTS = [
    ("C-port", "只去掉 port 那条参数化用例,同时改坏 port 的标识列",
     [(TEST, ' ("port", "ports"),', ""),
      (CLI, '    "port": ("ip", "port", "protocol"),',
            '    "port": ("port", "protocol"),')], True),

    ("C-finding", "只去掉 finding 那条参数化用例,同时改坏 finding 的标识列",
     [(TEST, ' ("finding", "findings"),', ""),
      (CLI, '    "finding": ("cve", "title", "description"),',
            '    "finding": ("title", "description"),')], True),

    ("C-domain", "只去掉 domain 那条参数化用例,同时改坏 domain 的标识列",
     [(TEST, ' ("domain", "domains"),', ""),
      (CLI, '    "domain": ("domain",),', '    "domain": ("resolved_ip",),')], True),

    ("C-site", "只去掉 site 那条参数化用例,同时改坏 site 的标识列",
     [(TEST, ' ("site", "sites"),', ""),
      (CLI, '    "site": ("url",),', '    "site": ("host",),')], True),

    # host 是唯一一个**期望被杀**的:它有第二道防线
    # (`test_table_wins_over_snapshot_fallback` 直接断言
    #  `_change_label(...) == "web.example.com"`),另外 4 种只有参数化那一条。
    ("C-host", "host 去掉参数化用例后仍被另一条测试守住 —— 这就是覆盖的不均匀",
     [(TEST, ' ("host", "hosts"),', ""),
      (CLI, '    "host": ("host", "ip"),', '    "host": ("ip", "host"),')], False),
]

TOUCHED = {CLI, TEST}


# 变异把源文件改成语法错误时的特征输出。命中即「变异无效」而非「杀死」。
_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")


def run_tests() -> tuple[bool, bool]:
    """返回 (全通过, 变异是否把源文件写坏了)

    第二个值是**必需**的:变异改坏语法时 pytest 同样会红,但那和判据
    无关 —— 文件 import 不了,一条测试都没跑。混在一起会把
    「变异写坏了」记成「测试有用」。
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
            print(f"[{mid}] !! 变异把源文件写成了语法错误 —— "
                  f"这种红不算杀死,判据没被检验到。{desc}")
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
        print("── 改生产代码:存活 = 测试有洞 ──")
        bad += sweep(MUTANTS, expect_default=False)
        print("\n── 同时改测试和生产代码:回答「哪条测试在守这个资产类型」 ──")
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
    print(f"{len(MUTANTS)} 个实现变异全杀,{len(COVERAGE_MUTANTS)} "
          f"个覆盖变异结果均符合预期")
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
