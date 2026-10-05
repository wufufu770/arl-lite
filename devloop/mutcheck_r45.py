"""r45 变异测试:CLI choices 派生要有真实退化路径

同 r41~r44:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy —— r42 那版用 `shutil.move`,第一次复原就把备份搬走了,
后面每个变异的「复原」全是空操作,变异原地留在工作树里。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

CLI = "arl_lite/cli.py"
TEST = "tests/test_cli_choices_derive_from_contracts.py"

DERIVED = '''    pmc.add_argument("-t", "--type", choices=sorted(_ASSET_TYPES),
                    help="资产类型")
    pmc.add_argument("-c", "--change-type", choices=list(CHANGE_TYPES),
                    help="变更类型")'''

MUTANTS = [
    ("M1", "把手写列表加回来(r45 修复前的原始形状) —— 少一个 ADDRESS_CHANGED",
     [(CLI, DERIVED,
       '    pmc.add_argument("-t", "--type", choices=["domain", "host", "port", "site", "finding"])\n'
       '    pmc.add_argument("-c", "--change-type", choices=["NEW_ASSET", "DISAPPEARED",\n'
       '                     "TITLE_CHANGED", "TECH_CHANGED", "FINGERPRINT_CHANGED", "STATUS_CHANGED"])')]),

    ("M2", "CLI 多给一个不存在的类型 —— 只查方向一的话这条会漏",
     [(CLI, DERIVED,
       '    pmc.add_argument("-t", "--type", choices=sorted(_ASSET_TYPES), help="资产类型")\n'
       '    pmc.add_argument("-c", "--change-type", choices=list(CHANGE_TYPES) + ["NOPE"],\n'
       '                    help="变更类型")')]),

    ("M3", "干脆不校验 —— 拼错会变成「查出来是空的」这种沉默的错",
     [(CLI, DERIVED,
       '    pmc.add_argument("-t", "--type", help="资产类型")\n'
       '    pmc.add_argument("-c", "--change-type", help="变更类型")')]),

    ("M4", "只从表名派生资产类型 —— 拿到的是复数(域名表)不是单数类型",
     [(CLI, "_ASSET_TYPES = tuple(_Monitor._ASSET_TABLES)",
       "_ASSET_TYPES = tuple(_Monitor._ASSET_TABLES.values())")]),

    # 这里**没有**「help 文本写死旧列表」那个变异,原先写了、被杀不掉。
    # 查实是那个变异的前提站不住:argparse 的 usage 行本来就从
    # `choices` 渲染出完整取值,所以 help 里抄的那一份不会骗到任何人 ——
    # 它只会制造第二处会漂移的地方。正确做法是在源头删掉这个重复
    # (已删),而不是为一个不该存在的重复编测试。
    # 这个变异能杀,但杀它的是「没有重复」本身,不是任何测试。
    ("M6", "资产类型从空集合派生 —— `--type` 变成什么都收",
     [(CLI, "_ASSET_TYPES = tuple(_Monitor._ASSET_TABLES)",
       "_ASSET_TYPES = ()")]),
]

TOUCHED = {CLI, TEST}


def run_tests() -> bool:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST, "-q", "-p", "no:warnings"],
        capture_output=True, text=True, env=env)
    return p.returncode == 0


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


def main() -> int:
    for p in TOUCHED:
        shutil.copy(p, p + ".mutbak")
    pristine = {p: open(p, "rb").read() for p in TOUCHED}
    try:
        if not run_tests():
            print("[对照] 基线就红,测不了变异"); return 1
        print("[对照] 未变异时全通过 —— 符合预期")
        survived = []
        for mid, desc, edits in MUTANTS:
            if not apply(edits):
                print(f"[{mid}] !! 变异没打上(原串不匹配)"); survived.append(mid); continue
            killed = not run_tests()
            revert()
            print(f"[{mid}] {'杀死' if killed else '*** 存活 ***'}  {desc}")
            if not killed:
                survived.append(mid)
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
    if survived:
        print(f"存活 {len(survived)} 个:{survived}"); return 1
    print(f"全部 {len(MUTANTS)} 个变异被杀")
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
