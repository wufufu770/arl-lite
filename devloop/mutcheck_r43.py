"""r43 变异测试:barren 补判要有真实退化路径

同 r41/r42:变异 = 把实现改坏,看测试是否**真的会红**。全绿说明没判别力。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy —— r42 那版用 `shutil.move`,第一次复原就把备份搬走了,
后面每个变异的「复原」全是空操作,变异原地留在工作树里。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

PROTO = "arl_lite/devloop/protocol.py"
STATE = "arl_lite/devloop/state.py"
TEST = "tests/test_barren_counter_sees_done_item.py"

JUDGE = '''        if prev.result != RESULT_NOOP:
            return 0, True, prev.round
        barren = self._git_commits_between(prev.started_at)
        if barren is None:        # 判断不了:不判,也不清零
            return 0, False, prev.round
        if barren:                # 窗口里有产出 → 连续计数清零
            return 0, True, prev.round
        return 1, False, prev.round'''

MUTANTS = [
    ("M1", "退回旧判据:只要是 NOOP 就计数,不看窗口有没有提交",
     [(PROTO, JUDGE,
       "        if prev.result != RESULT_NOOP:\n"
       "            return 0, True, prev.round\n"
       "        return 1, False, prev.round")]),

    ("M2", "只清零,永不计数 —— 退路这层保险彻底没了",
     [(PROTO, JUDGE, "        return 0, True, prev.round")]),

    ("M3", "只计数,什么都清不掉 —— 有产出也接着往上加",
     [(PROTO, JUDGE, "        return 1, False, prev.round")]),

    ("M4", "「判断不了」改成清零 —— 拿测量失败推出结论",
     [(PROTO, JUDGE,
       "        if prev.result != RESULT_NOOP:\n"
       "            return 0, True, prev.round\n"
       "        has = self._git_commits_between(prev.started_at)\n"
       "        if has is None or has:\n"
       "            return 0, True, prev.round\n"
       "        return 1, False, prev.round")]),

    ("M5", "去掉「已判过就不重复判」的守卫",
     [(PROTO, "        if state.barren_judged_round >= prev.round:\n"
              "            return 0, False, 0        # 判过了,别重复计\n", "")]),

    ("M6", "三态塌成 bool:判断不了一律当成「没提交」",
     [(PROTO, "            return False if probe is not None and probe.returncode == 0 else None",
       "            return False")]),

    ("M7", "分不清空仓库和非仓库 —— 给从没提交过的仓库关掉退路",
     [(PROTO, "            probe = self._git(\"rev-parse\", \"--git-dir\")\n"
              "            return False if probe is not None and probe.returncode == 0 else None",
       "            return None")]),

    ("M8", "退路阈值改成严格大于 —— 退路永不触发",
     [(PROTO, "        if state.barren_rounds >= RETREAT_THRESHOLD:",
       "        if state.barren_rounds > RETREAT_THRESHOLD:")]),

    ("M9", "接线断掉:round 不把补判结果带进状态",
     [(PROTO, "                                       barren_delta=b_delta, barren_reset=b_reset,\n"
              "                                       barren_judged_round=b_judged)",
       "                                       )")]),

    ("M10", "落盘又开始自己判 barren",
     [(STATE, "            if barren_reset:\n"
              "                st.barren_rounds = 0\n"
              "            else:\n"
              "                st.barren_rounds = max(0, st.barren_rounds + barren_delta)",
       "            st.barren_rounds = (\n"
       "                st.barren_rounds + 1 if record.result == RESULT_NOOP else 0\n"
       "            )")]),
]

TOUCHED = {PROTO, STATE, TEST}


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
