"""r46 变异测试:变更输出要有真实退化路径

同 r41~r45:变异 = 把实现改坏,看测试是否**真的会红**。

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
TEST = "tests/test_monitor_changes_output.py"

MUTANTS = [
    ("M1", "回到只打印元信息的那一版 —— diff 又不显示了(r46 修复前)",
     [(CLI, '''        for line in _change_lines(r):
            print(f"      {line}")''', "        pass")]),

    ("M2", "标识退回 hash 前缀 —— 用户看不出是哪个资产",
     [(CLI, '''    table = _ASSET_TYPES_TO_TABLE.get(at)
    if table and fields:''', "    table = None\n    if table and fields:")]),

    ("M3", "标识只看快照不查表 —— 退化成「会变的那个字段的值」",
     [(CLI, '''    table = _ASSET_TYPES_TO_TABLE.get(at)
    if table and fields:''', "    table = None\n    if table and fields:\n        pass")]),

    ("M4", "单边快照的 diff 显示成空 —— bug 和设计长得一样",
     [(CLI, '''        return ["(无字段级 diff:" + _SINGLE_SIDED.get(
            ct, "这一条本该有 diff 却没存下来 —— 这是异常") + ")"]''',
       "        return []")]),

    ("M5", "解析不了的 diff 静默跳过 —— 坏数据从报表里消失",
     [(CLI, '''    except (TypeError, ValueError):
        return ["(diff 解析不出来,原样无法展示)"]''',
       "    except (TypeError, ValueError):\n        return []")]),

    ("M6", "一条坏行把整份列表搞崩 —— 展示层拿报表换进程",
     [(CLI, "        for line in _change_lines(r):",
       "        for line in _change_lines(r + {'id': None}):")]),

    ("M7", "--json 丢字段 —— 机器消费拿不到 payload",
     [(CLI, '''              "before_value": _row_json(r, "before_value"),''',
       '''              "before_value": {},''')]),

    ("M8", "--json 输出人话 —— 机器解析不了",
     [(CLI, '''    if getattr(args, "json", False):''',
       '''    if False:''')]),

    ("M9", "值截断到 0 长度 —— 前后值全被吃掉",
     [(CLI, '    return s if len(s) <= 60 else s[:57] + "..."',
       "    return s[:0]")]),
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
    raise SystemExit(main())
