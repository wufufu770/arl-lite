"""r44 变异测试:字段级变更检测要有真实退化路径

同 r41/r42/r43:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy —— r42 那版用 `shutil.move`,第一次复原就把备份搬走了,
后面每个变异的「复原」全是空操作,变异原地留在工作树里。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

STORAGE = "arl_lite/db/storage.py"
MONITOR = "arl_lite/core/monitor.py"
RUNNER = "arl_lite/core/task_runner.py"
TEST = "tests/test_monitor_field_changes.py"

MUTANTS = [
    ("M1", "整个字段级检测删掉 —— 回到 r44 之前",
     [(STORAGE, "                changes = self._detect_field_changes(existing, fields,\n"
                "                                                    text_cols, plain_cols)\n"
                "                if changes and self._on_field_change is not None:\n"
                "                    try:\n"
                "                        self._on_field_change(table, existing[\"hash\"], changes)\n"
                "                    except Exception as e:      # noqa: BLE001 - 旁路,不能拖垮入库\n"
                "                        log.warning(\"field change not recorded (%s): %s\", table, e)\n",
       "")]),

    ("M2", "SELECT * 退回 SELECT 1 —— 旧行没取出来,比不了",
     [(STORAGE, 'f"SELECT * FROM {table} WHERE hash = ?"',
       'f"SELECT 1 FROM {table} WHERE hash = ?"')]),

    ("M3", "不比对,见存量就报一次 —— 同一轮重扫会刷屏",
     [(STORAGE, "            if (type(effective).__name__, effective) != (type(old_v).__name__, old_v):\n"
                "                out[k] = {\"before\": old_v, \"after\": effective}\n",
       "            out[k] = {\"before\": old_v, \"after\": effective}\n")]),

    ("M4", "空值也算变 —— 判据没跟 UPDATE 的 COALESCE 对齐",
     [(STORAGE, "            effective = new if (k in plain_cols or new) else old_v",
       "            effective = new")]),

    ("M5", "首次入库也报 —— 新资产看起来像「刚换了 IP」",
     [(STORAGE, "            if existing:", "            if True:")]),

    ("M6", "不看字段是不是可变字段 —— 数据库不刷新的字段也会报",
     [(STORAGE, "            if k not in plain_cols and k not in text_cols:\n"
                "                continue\n", "")]),

    ("M7", "不看契约表,全塞进同一个类型 —— 换 IP 和换标题无法区分",
     [(MONITOR, "            change_type = FIELD_CHANGE_TYPES.get(field)",
       '            change_type = "STATUS_CHANGED"')]),

    ("M8", "记录失败会拖垮入库 —— 旁路变成了主路",
     [(STORAGE, "                    except Exception as e:      # noqa: BLE001 - 旁路,不能拖垮入库\n"
                '                        log.warning("field change not recorded (%s): %s", table, e)',
       "                    except Exception:\n                        raise")]),

    ("M9", "拿不到 asset_type 就默认当 host —— 编造不是兜底",
     [(MONITOR, "        if asset_type is None:\n"
                "            # 拿不到就**不记**,不硬猜一个。asset_type 是 record_change\n"
                "            # 的第一等参数,猜错等于把别的表上的变更记成 host 的。\n"
                "            return",
       '        asset_type = asset_type or "host"')]),

    ("M10", "派生字段也配上类型 —— cert_days_left 每天刷一条",
     [(MONITOR, '    "cert_sha256": "FINGERPRINT_CHANGED",',
       '    "cert_days_left": "STATUS_CHANGED",\n'
       '    "cert_sha256": "FINGERPRINT_CHANGED",')]),

    ("M11", "生产入口不接线 —— 字段级检测在生产里完全没工作",
     [(RUNNER, "        from .monitor import attach_field_change_sink\n"
               "        attach_field_change_sink(storage)\n", "")]),

    ("M12", "比相等不比身份 —— true 改成 1 这类整条被吞",
     [(STORAGE, "            if (type(effective).__name__, effective) != (type(old_v).__name__, old_v):",
       "            if effective != old_v:")]),

    ("M13", "存储层把 core 的映射抄一份 —— 契约表出现第二个来源",
     [(MONITOR, "            change_type = FIELD_CHANGE_TYPES.get(field)",
       '            change_type = ("ADDRESS_CHANGED" if field in ("ip", "resolved_ip")\n'
       '                           else "STATUS_CHANGED")')]),
]

TOUCHED = {STORAGE, MONITOR, RUNNER, TEST}


def run_tests() -> bool:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST,
         "tests/test_architecture.py", "-q", "-p", "no:warnings"],
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
