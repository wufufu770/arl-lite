"""r42 变异测试:白名单要有真实退化路径

同 r41:变异 = 把实现改坏,看测试是否**真的会红**。全绿说明没判别力。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

SRC = "arl_lite/core/monitor.py"
TEST = "tests/test_monitor_change_type_whitelist.py"
WATCHER = "arl_lite/core/watcher.py"
BACKUP = SRC + ".mutbak"

CHECK = '''    if change_type not in CHANGE_TYPES:
        raise ValueError(
            f"unknown change_type: {change_type!r} "
            f"(choose from {list(CHANGE_TYPES)})")
'''

# 每个变异是一串 (文件, 原串, 替换串);多段是为了能表达「把校验挪走」
# 这种搬动型改动,单串替换做不到。
MUTANTS = [
    ("M1", "整个白名单删掉 —— 回到 r42 之前的行为", [(SRC, CHECK, "")]),

    ("M2", "校验挪到写入之后 —— 报错照报,库已经脏了", [
        (SRC, CHECK, ""),
        (SRC, '        log.warning(f"record_change failed: {e}")\n'
              '        return False\n    return True',
              '        log.warning(f"record_change failed: {e}")\n'
              '        return False\n'
              '    if change_type not in CHANGE_TYPES:\n'
              '        raise ValueError(f"unknown change_type: {change_type!r}")\n'
              '    return True'),
    ]),

    ("M3", "静默改写成第一个合法类型 —— 拼错变成另一种拼错",
     [(SRC, CHECK,
       "    if change_type not in CHANGE_TYPES:\n"
       "        change_type = CHANGE_TYPES[0]\n")]),

    ("M4", "白名单扩到 schema 注释的 6 种词表 —— 回到「注释写过就算实现」",
     [(SRC, 'CHANGE_TYPES = ("NEW_ASSET", "DISAPPEARED")',
       'CHANGE_TYPES = ("NEW_ASSET", "DISAPPEARED", "TITLE_CHANGED",\n'
       '                "TECH_CHANGED", "FINGERPRINT_CHANGED", "STATUS_CHANGED")')]),

    ("M5", "空串放行 —— `if change_type and ...`",
     [(SRC, "    if change_type not in CHANGE_TYPES:",
       "    if change_type and change_type not in CHANGE_TYPES:")]),

    ("M6", "报错不说合法取值是什么 —— 只说非法",
     [(SRC, '            f"(choose from {list(CHANGE_TYPES)})")', '            ")')]),

    ("M7", "报错不回显实际传进去的值 —— 拼错时看不出是哪一处",
     [(SRC, '            f"unknown change_type: {change_type!r} "',
       '            f"unknown change_type "')]),

    ("M8", "AST 扫描器被改成什么都扫不到 —— 静态检查形同虚设",
     [(TEST, '    for path in sorted(ARL_ROOT.rglob("*.py")):',
       "    for path in []:")]),
]


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


def revert(paths):
    """复原必须用 copy 而不是 move

    第一版这里写的是 `shutil.move(bak, path)` —— 第一次复原就把备份
    **搬走**了,后面每个变异的"复原"全是空操作,于是 M2 和 M9 的变异
    原地留在工作树里。是 `git status` 看出来的,不是测试报的。
    备份要活到整个 run 结束,最后再删。
    """
    for path in set(paths):
        bak = path + ".mutbak"
        if os.path.exists(bak):
            shutil.copy(bak, path)


def cleanup(paths):
    for path in set(paths):
        bak = path + ".mutbak"
        if os.path.exists(bak):
            os.remove(bak)


def main() -> int:
    touched = {SRC, WATCHER, TEST}
    for p in touched:
        shutil.copy(p, p + ".mutbak")
    pristine = {p: open(p, "rb").read() for p in touched}
    if not run_tests():
        print("[对照] 基线就红,测不了变异"); revert(touched); return 1
    print("[对照] 未变异时全通过 —— 符合预期")

    survived = []
    try:
        for mid, desc, edits in MUTANTS:
            if not apply(edits):
                print(f"[{mid}] !! 变异没打上(原串不匹配)"); survived.append(mid); continue
            killed = not run_tests()
            revert(touched)
            print(f"[{mid}] {'杀死' if killed else '*** 存活 ***'}  {desc}")
            if not killed:
                survived.append(mid)

        # M9:静态检查这条要靠改**生产代码**来验 —— 在 watcher 里塞一个
        # 非法取值的调用点,看扫描器抓不抓得到。
        w = open(WATCHER, encoding="utf-8").read()
        open(WATCHER, "w", encoding="utf-8").write(
            w.replace('self.storage, asset_type, "NEW_ASSET",',
                      'self.storage, asset_type, "TITLE_CHANGE",', 1))
        killed = not run_tests()
        revert(touched)
        print(f"[M9] {'杀死' if killed else '*** 存活 ***'}  "
              f"在 watcher 里塞一个白名单外的调用点,静态检查抓不抓得到")
        if not killed:
            survived.append("M9")
    finally:
        revert(touched)
        cleanup(touched)
        # 「这个脚本有没有留下半截现场」——按内容哈希判,不能用
        # `git diff`:monitor.py / test_monitor_baseline.py 本来就有
        # 未提交的 r42 改动,拿 git 当基准只会一直误报。
        left = [p for p in sorted(touched)
                if open(p, "rb").read() != pristine[p]]
        if left:
            print(f"!! 复原后这些文件的内容变了:{left}")
            return 1

    print()
    if survived:
        print(f"存活 {len(survived)} 个:{survived}"); return 1
    print(f"全部 {len(MUTANTS) + 1} 个变异被杀")
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
