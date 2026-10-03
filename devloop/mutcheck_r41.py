"""r41 变异测试:给新增的每条判据配一个真实退化路径

变异 = 把实现改坏,看测试是否**真的会红**。全绿说明测试没判别力,
那这些测试就是装饰。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人 ——
上一轮(r35)就因为缓存的字节码,变异打上去测试照样绿。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

SRC = "arl_lite/core/monitor.py"
TEST = "tests/test_monitor_baseline.py"

# (编号, 变异描述, 原串, 替换串)
MUTANTS = [
    ("M1", "去掉「变回过」判据(只剩条数) —— 即 r40 的原实现",
     "return int(n) >= int(threshold) and came_back",
     "return int(n) >= int(threshold)"),

    ("M2", "只要条数够就静默(「变回过」恒真) —— 退回吃演进",
     "return int(n) >= int(threshold) and came_back",
     "return int(n) >= int(threshold) and True"),

    ("M3", "阈值判据 > 写成 >= —— 学习期少一轮",
     "return int(n) >= int(threshold) and came_back",
     "return int(n) > int(threshold) and came_back"),

    ("M4", "用「取值不超过 2 个」代替「变回过」—— 三值轮转会漏判",
     "came_back = any(v in values[:i] for i, v in enumerate(values))",
     "came_back = len(set(map(str, values))) <= 2"),

    ("M5", "取值身份去掉类型名 —— JSON 的 true 和 1 被当成同一个",
     'return (type(value).__name__, value)',
     'return value'),

    ("M6", "不再 parse,LIKE 命中就算(退回文本子串当结构判据)",
     '        entry = parsed.get(field) if isinstance(parsed, dict) else None',
     '        entry = {"before": 1, "after": 2} if parsed is not None or True else None'),

    ("M7", "认不出来的行也算证据 —— 坏 diff 能把阈值凑够",
     '                      row["id"], field)\n            continue',
     '                      row["id"], field)\n            n += 1\n            continue'),

    ("M8", "去掉接缝去重 —— 连续链的接缝被误读成「回来过」",
     "        if not values or values[-1] != b:",
     "        if True:"),

    ("M9", "field=None 退回按条数判 —— 没有字段也敢下噪声结论(即 r40 行为)",
     "    if threshold <= 0 or field is None:\n        return False",
     "    if threshold <= 0:\n        return False\n"
     "    if field is None:\n"
     "        sql = ('SELECT COUNT(*) FROM asset_changes WHERE workspace_id = ?'\n"
     "               ' AND asset_hash = ? AND change_type = ?')\n"
     "        with storage._conn() as conn:\n"
     "            return int(conn.execute(\n"
     "                sql, [storage.workspace_id, asset_hash, change_type]\n"
     "            ).fetchone()[0]) >= int(threshold)"),

    ("M10", "字段是否变化的判断退回 `==`(r41 挖出来的第二处真 bug)",
     "            if _val_identity(bv) != _val_identity(av):\n"
     "                diff[k] = {\"before\": bv, \"after\": av}",
     "            if bv != av:\n"
     "                diff[k] = {\"before\": bv, \"after\": av}"),
]


def run_tests() -> bool:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST, "-q", "-p", "no:warnings"],
        capture_output=True, text=True, env=env)
    return p.returncode == 0


def main() -> int:
    shutil.copy(SRC, SRC + ".orig")
    baseline = run_tests()
    print(f"[对照] 未变异时测试 {'通过' if baseline else '不通过'}"
          f" —— {'符合预期' if baseline else '!! 基线本身就红,测不了变异'}")
    if not baseline:
        shutil.move(SRC + ".orig", SRC)
        return 1

    survived = []
    try:
        for mid, desc, old, new in MUTANTS:
            src = open(SRC, encoding="utf-8").read()
            if old not in src:
                print(f"[{mid}] !! 原串没匹配上,变异没打上去 —— 判为存活并报警")
                survived.append(mid + "(变异没打上)")
                continue
            open(SRC, "w", encoding="utf-8").write(src.replace(old, new, 1))
            killed = not run_tests()
            shutil.copy(SRC + ".orig", SRC)
            print(f"[{mid}] {'杀死' if killed else '*** 存活 ***'}  {desc}")
            if not killed:
                survived.append(mid)
    finally:
        shutil.move(SRC + ".orig", SRC)

    print()
    if survived:
        print(f"存活 {len(survived)} 个:{survived}")
        return 1
    print(f"全部 {len(MUTANTS)} 个变异被杀")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
