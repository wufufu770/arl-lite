"""r111:把所有已标完成条目的 verify 真跑一遍(引擎的 30 秒口径)

## 为什么要有这个脚本

`tests/test_backlog_verify_is_commandable.py` 只验 verify 的**形状**(首词在
白名单里 = 是命令不是散文),**从不真跑**。于是 r111 查出的这条:

    verify = `python3 -m arl_lite fp-bench`      首词是 python3,形状合格
    实际   = exit 2 —— 「未写入 docs/FP_RATE.md,那是版本库里已提交的基线,
             默认不覆盖」

`fp-bench` 后来加了「默认不覆盖已提交的基线」的保护,这条验收命令就失效了。
**工具行为变了,验收标准没跟着变,而没有任何东西会响。** 条目的 detail 里
还写着「`arl-lite fp-bench` 可复跑」—— 说谎,而且没人查。

## 为什么它**不在门禁里**

跑一遍要按 `Queue.verify_result` 的口径(**每条 30 秒上限**)。实测耗时见
本文件末尾的输出。`test_baseline` 已经在 600s 硬上限的 535s 上,
**再加这一段就会撞上限**。所以它是手动跑的,不是门禁的一部分。

**这不是「好设计」,是「预算不够」**:一个不在门禁里的检查会烂在那里,
而 r107 那条轮次索引就是这么烂掉的。提额是人的决定,这里只把成本量出来。

## 口径必须和引擎一致

用 `Queue.verify_result` 自己的白名单和超时,不另抄一份。r109 已经栽过一次
「两处各写一份判定而且已经漂了」—— 那次是 `startswith("✅")` vs `in`。
所以这里直接调引擎那一份。

## 哪些条目要跑

**只跑已标 ✅ 的**。未完成的条目 verify 此刻就该是红的(backlog 文件头自己
写的规则),跑它们没有意义,还会把「未完成」误报成「verify 坏了」。

## **不要把本脚本当成某条待办的 verify**

r111 刚犯过一次:我给「verify 脱节」那条的 verify 填了
`python3 devloop/backlog_verify_sweep.py`,两个问题同时中:

  - **自指**:sweep 会把它自己再跑一遍,递归
  - **超引擎口径**:1m37s > `verify_result` 的 30s 上限 → `unknown` →
    `protocol.py` 不拦 → 那条活**永远验不了**

而 `test_every_backlog_verify_is_a_runnable_command` 是**绿的** ——
首词是 `python3`,形状合格。**形状对 ≠ 跑得通**,这正是这个脚本存在的理由。

条目的 verify 要**快**(秒级)且**指向一条具体的判据**。例:
`python3 -m pytest tests/test_devloop_queue_invariant.py::test_... -q`(0.6s)。

用法:  python3 devloop/backlog_verify_sweep.py [--verbose]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arl_lite.devloop.queue import Queue, _slugify_id, is_backlog_done  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BACKLOG = REPO / "devloop" / "backlog.md"

PASS, FAIL, UNKNOWN = (Queue.VERIFY_PASS, Queue.VERIFY_FAIL, Queue.VERIFY_UNKNOWN)


def entries() -> list[dict]:
    out = []
    for n, line in enumerate(BACKLOG.read_text(encoding="utf-8").splitlines(), 1):
        m = Queue._BACKLOG_LINE.match(line)
        if not m:
            continue
        detail = m.group(4).strip()
        out.append({
            "line": n,
            "id": _slugify_id(m.group(3).strip(), 0),
            "detail": detail,
            "verify": m.group(5).strip(),
            "done": is_backlog_done(detail),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="每条都打,不只是有问题的")
    args = ap.parse_args()

    all_entries = entries()
    done = [e for e in all_entries if e["done"]]
    if not all_entries:
        print("backlog.md 一条都解析不出来 —— 范围不对,下面的结论不能拿来用")
        return 2
    if not done:
        print("backlog.md 里一条 ✅ 都没有 —— 前置条件不成立,这次扫描验不到任何东西")
        return 2

    print(f"backlog.md 共 {len(all_entries)} 条,其中已标完成 {len(done)} 条")
    print(f"口径:Queue.verify_result(引擎自己的白名单 + {Queue.verify_result.__defaults__[0]}s 超时)\n")
    buckets = {PASS: [], FAIL: [], UNKNOWN: []}
    for e in done:
        r = Queue.verify_result(e["verify"])
        buckets[r].append(e)
        if args.verbose or r != PASS:
            print(f"  L{e['line']:3} {r:8} {e['id'][:40]}")
            if r != PASS:
                print(f"        verify: {e['verify'][:72] or '（空）'}")

    bad = buckets[FAIL] + buckets[UNKNOWN]
    print(f"\n通过 {len(buckets[PASS])} / 失败 {len(buckets[FAIL])} / "
          f"判断不了 {len(buckets[UNKNOWN])}")
    if bad:
        print("\n这些已完成的活,验收命令现在跑不通(工具行为变了 / 写成了散文 / "
              "慢到超引擎的 30s):")
        for e in bad:
            why = "退出码非 0" if Queue.verify_result(e["verify"]) == FAIL else \
                  "unknown —— 散文、首词不在白名单、或超时"
            print(f"  L{e['line']:3} {e['id']}\n        {why}\n"
                  f"        {e['verify'][:72] or '（空字符串）'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
