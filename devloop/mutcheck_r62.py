"""r62 变异测试:「默认不覆盖已提交基线」这条,改坏了测试真的会红吗

同 r41~r61:变异 = 把实现改坏,看测试是否**真的会红**。

必须带 PYTHONDONTWRITEBYTECODE=1 + python3 -B:`.pyc` 会骗人。
复原一律用 copy(r42 那版用 `shutil.move`,第一次复原就把备份搬走了)。
命中 SyntaxError / ERROR collecting 一律判「变异无效」,不算杀死(r47 踩过)。

## 本轮的结构和 r60/r61 不同:变的不是**文案**,是**写不写**

r60/r61 修的是「建议里的命令跑不跑得通」,判据跑一次就能验。
r62 修的是「跑完之后往哪写」—— 所以 M1 不能只改文案,得真的把
「默认不写」退回成早先的「默认直接写」,看那条真跑子进程的判据会不会红。

## 三条我一开始判断错了,实测查出来的

* **M6 我按「存活」写,实测被杀** —— 兜住它的是
  `test_scale_mismatch_is_called_out_in_place`(它断言 stderr 里有「不可比」)。
  首版的理由是「警告是给人读的,同档重跑那条不该被它连坐」,听起来很顺,
  但我**没去查有没有判据在守它**就写进了脚本。查了才发现有。
  教训:别给「这条大概没人测」下结论,先 grep 一遍再写。
* **C-e2e-only 我按「存活」写,实测被杀** —— 杀它的**有两条**:
  一是那条真起子进程的端到端判据(预期之内),二是**没被我 skip 的
  fp-bench 那条** —— 因为 `_resolve_bench_out` 是两个 bench 共用的,
  动它等于同时动两边。这是个真收获:共用 helper 意味着改动会同时被
  两边的守卫覆盖,反过来也意味着**只 skip 一边是关不住它的**。
* **M4 首版是个空操作变异** —— 我写的是在测量输出**前面加**一行,
  并没有真的把输出删掉,结果判据当然不红(r47 那条老规矩又踩了一次:
  空操作变异不证明任何事)。改成真的把两行 `[i]` 和分阶段循环删掉。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

CLI = "arl_lite/cli.py"
TEST = "tests/test_bench_wont_clobber_baseline.py"

_BROKEN_SOURCE = ("SyntaxError", "IndentationError", "TabError",
                  "ERROR collecting")

_RESOLVE = '''    explicit = (getattr(args, "out", "") or "").strip()
    if explicit:
        return _P(explicit), None, False
    if not getattr(args, "in_place", False):
        return None, None, False'''
_RESOLVE_CLOBBER = '''    explicit = (getattr(args, "out", "") or "").strip()
    if explicit:
        return _P(explicit), None, False'''
_MISMATCH = '''    if in_place and old_scale and old_scale != rep.scale:'''
_SCALE_READ = '''                if len(parts) >= 2:
                    old_scale = parts[1]'''
_MEASURE_PRINT = (
    '    print(f"[i] scale={rep.scale}  repeat={repeat}  "\n'
    '          f"{rep.total_rows} 行  {rep.total_seconds:.2f}s  "\n'
    '          f"峰值 {rep.peak_kb / 1024:.1f} MB")\n'
    '    print(f"[i] {rep.rules} 条规则 / {rep.hits} 个命中")\n'
    '    for p in sorted(rep.phases, key=lambda x: -x.seconds)[:5]:\n'
    '        rps = f"{p.rows_per_sec:,.0f} 行/s" if p.rows_per_sec else "-"\n'
    '        print(f"   {p.name:36s} {p.seconds:7.3f}s  {rps}")'
)

MUTANTS = [
    # ── 把「默认不写」原样退回成「默认直接写」:本轮的核心 ──
    ("M1", "默认又直接写已提交的基线(退回 r62 之前的行为)",
     [(CLI, _RESOLVE, _RESOLVE_CLOBBER)]),

    ("M2", "默认不写,但退出码改回 0 —— 用户以为基线已经刷新了",
     [(CLI, "    if out is None:\n        return 2", "    if out is None:\n        return 0")]),

    ("M3", "拒绝时把两条出路的话术删掉 —— 用户只会以为命令坏了",
     [(CLI, '    print("    要写到别处:  --out <路径>", file=sys.stderr)',
       '    pass')]),

    ("M4", "拒绝时把测量结果整个吞掉 —— 毁文件之外还把信息也丢了",
     [(CLI, _MEASURE_PRINT, '    print("[!] nothing measured")')]),

    ("M5", "拒绝发生在写完之后 —— 顺序反了,等于「写完再警告」",
     [(CLI, '''    out, old_scale, in_place = _resolve_bench_out(args, "docs/PERF_BASELINE.md")
    if out is None:
        _refuse_to_clobber("docs/PERF_BASELINE.md")''',
       '''    out, old_scale, in_place = _resolve_bench_out(args, "docs/PERF_BASELINE.md")''')]),

    # 首版按「存活」写,实测**被杀** —— `test_scale_mismatch_is_called_out_in_place`
    # 断言 stderr 里有「不可比」,它就是这条警告的守卫。
    ("M6", "换档警告被去掉 —— 「不可比」三个字没人说了",
     [(CLI, _MISMATCH, "    if False:")]),

    ("M7", "换档判定读不到旧档位 —— 换了档也一声不吭",
     [(CLI, _SCALE_READ, "                pass")]),
]

# 覆盖变异:同时改判据和实现,回答「哪条判据在守这里」。
COVERAGE_MUTANTS = [
    # 期望**被杀**,而且杀它的是**两条**,记着是谁兜的:
    # 一是那条真起子进程的端到端判据(比的是**真文件字节**,预期之内);
    # 二是**没被 skip 的 fp-bench 那条** —— `_resolve_bench_out` 是两边共用的,
    # 动它等于同时动两边。所以「只 skip perf-bench 的守卫」是关不住它的,
    # 共用 helper 带来的是双向覆盖,不是单点。
    ("C-e2e-only", "skip 掉 4 条单元判据,只留真起子进程那条",
     [(TEST, "def test_perf_bench_does_not_clobber_the_baseline_by_default(",
             "@pytest.mark.skip\ndef test_perf_bench_does_not_clobber_the_baseline_by_default("),
      (TEST, "def test_it_says_how_to_actually_write(",
             "@pytest.mark.skip\ndef test_it_says_how_to_actually_write("),
      (TEST, "def test_refusing_to_write_still_gives_you_the_numbers(",
             "@pytest.mark.skip\ndef test_refusing_to_write_still_gives_you_the_numbers("),
      (TEST, "def test_scale_mismatch_is_called_out_in_place(",
             "@pytest.mark.skip\ndef test_scale_mismatch_is_called_out_in_place("),
      (CLI, _RESOLVE, _RESOLVE_CLOBBER)], False),

    # 期望**被杀**,杀它的是 fp-bench 那条 —— 说明两个 bench 各有各的守卫,
    # 修一个不会顺带盖住另一个(它们共用 `_resolve_bench_out`,但守卫是分开的)。
    ("C-fp-only", "skip 掉 perf-bench 的守卫,把 fp-bench 那一侧退回成直接写",
     [(TEST, "def test_perf_bench_does_not_clobber_the_baseline_by_default(",
             "@pytest.mark.skip\ndef test_perf_bench_does_not_clobber_the_baseline_by_default("),
      (CLI, '''    out, _, _ = _resolve_bench_out(args, "docs/FP_RATE.md")
    if out is None:
        _refuse_to_clobber("docs/FP_RATE.md")''',
       '''    from pathlib import Path as _P
    out = _P(args.out) if getattr(args, "out", "") else _P("docs/FP_RATE.md")''')], False),
]

TOUCHED = {CLI, TEST}


def run_tests() -> tuple[bool, bool]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST, "-q", "-p", "no:warnings"],
        capture_output=True, text=True, env=env, timeout=900)
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
    for path in TOUCHED:
        shutil.copy(path, path + ".mutbak")
    pristine = {p: open(p, "rb").read() for p in TOUCHED}
    bad = []
    try:
        if not run_tests()[0]:
            print("[对照] 基线就红,测不了变异")
            return 1
        print("[对照] 未变异时全通过 —— 符合预期\n")
        print("── 写盘行为:存活 = 判据有洞 ──")
        bad = sweep(MUTANTS, expect_default=False)
        print("\n── 同时改判据和实现:回答「哪条判据在守这里」 ──")
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
        print(f"!! {len(bad)} 条变异不符合预期: {bad}")
        return 1
    print(f"{len(MUTANTS)} 个实现变异结果均符合预期,"
          f"{len(COVERAGE_MUTANTS)} 个覆盖变异结果均符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
