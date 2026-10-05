"""r87 变异测试:验「机器级 /tmp 噪音不得让判据假红」这个修复在守。

## 主题

`TestNoStrayTmpDirsSurviveARun` 原来量的是**整台机器**的 `/tmp`:快照收
`tmp*` 目录,子进程跑完再收一次,多的就算「泄漏」。r36 把它从「整个 /tmp」
收窄到「`tmp*`」,理由是 `tempfile.mkdtemp()` 一律以 `tmp` 开头 —— 理由
对,结论错了一半:**`tmp*` 是每一个 Python 程序的约定,不是本项目的**。

r87 实测(零 arl-lite 测试在跑的 60 秒窗口内,本机有别的项目在写 /tmp):

    起点 tmp* 目录数: 143
      t+16s  新增=['tmp4sfrus9a'] 消失=0
    60 秒内外部进程新建的 tmp* 目录: ['tmp4sfrus9a']

修法:子进程的 `TMPDIR` 指向私有地,快照对象**由子进程自己报**。

## 这轮最值钱的两个变异,都是我自己真犯过的错

M2 就是 r87 第一版的原样。第一版把快照目标**写死**成那块私有地,于是
「快照看哪儿」和「TMPDIR 生不生效」被解耦:删掉 `TMPDIR` 那行,子进程照样
跑、快照照样只看私有地,判据 **2 passed rc=0** —— 修复等于没修。
是实测 rc=0 才发现的,不是想出来的。

M3 是「注入的噪音压根没种」。判据的判别力全在那次 `mkdtemp()` 上,
没有它,假红就测不出来 —— 而「判据自己没注入」这件事,必须由判据自己
说出来(`assertEqual(len(planted), 1)`),不能指望人记得。

## 变异清单

实现变异(期望全被杀):
  M1 删掉 TMPDIR 隔离                      → 噪音测试 + 机制测试双杀
  M3 noise() 不再往真实临时区种噪音         → 「刺激落在该落的地方」那条杀
  M4 _child_strays 不再调 noise()           → 同上
  M5 _tmp_dirs 恒返回空集                   → 快照牙齿测试杀
  M6 _child_tempdir 改报外层的临时地        → 同 M1,机制测试杀

覆盖变异(期望全存活):
  C1 噪音测试里的断言放宽成恒真
  C2 快照目标改成直接读 TMPDIR,不再问子进程

## 这轮踩的坑,全是同一个病:变异不成立

写完第一版直接跑,7 条里 2 条不符合预期,查下来**全是变异自己的错**,
没有一条是判据的错:

| 变异 | 名义 | 实际 | 结论 |
|---|---|---|---|
| M2 | 快照目标写死 → 该杀 | `env["TMPDIR"]` 就是私有地,子进程也报它,**完全等价** | 假变异(第 9 次) |
| M3 | 噪音没种进真实 /tmp | 那行写的是 `tempfile.mkdtemp()`,跑在**外层**进程,落点就是真实 /tmp,照种不误 | 假变异(第 8 次),注释还写「落在私有地」 |
| M6 | 我们替子进程撒谎 | `env.get("TMPDIR") or …` 里前半恒真,改前改后等价 | 假变异(第 7 次) |
| C1 | 断言放宽 | 锚点里的文案已经被我中途改过,没命中 | 锚点失配,不是变异不成立 |

三次假变异的共同点:**注释和代码说的不是一回事**。M3 那行注释白纸黑字
写着「落在私有地,不在真实 /tmp」,而 `tempfile.mkdtemp()` 恰恰落在真实
`/tmp`。跟 r83 逮到的那次是同一个病 —— 文档描述了代码没有的行为。

修法不是把期望改成「存活」了事:C2 留下了(它的存活有真实含义,见
docstring),M3/M6 换成了真能生效的写法,判据侧补上「刺激必须落在真实
临时区且符合 tmp* 命名」——**探针得先证明自己的 stimulus 落在该落的地方**,
这是 r84 那条规矩的直接应用。

r88 回补:第一版这里写的是「M2 就是 r87 第一版的原样」。M2 后来被证明是
假变异、改名 C2 挪进了覆盖变异,但这段叙述和 `CLAIMS` 里那条 M2 声明
都忘了跟着改 —— 于是脚本里出现了一个**指哪儿都指不到**的 M2。
r88 加的 `test_every_claim_has_a_mutant` 当场逮住了 CLAIMS 那半边;
这半边(散文)是人读的时候才发现的。**两处漏的是同一个错误**。

另外顺手修了一个会掩盖问题的毛病:一条坏变异原来直接把整个 run 打断,
后面的变异一条都没跑。现在报成 `BAD-MUTANT` 继续走 —— C1 那次就是这么
暴露的。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量(r86 被 `ast.literal_eval` 拦过一次),且 must_have
必须改前不存在(r83 的规矩,M1 上栽过一次)。
所有变异统一走 _write_checked,写盘前 ast.parse。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_no_real_home_writes.py"

TARGET = ["tests/test_no_real_home_writes.py::TestNoStrayTmpDirsSurviveARun"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 锚点(整体照抄完整语句)──

CHILD_ENV = (
    '        return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",\n'
    '                "TMPDIR": str(private)}\n'
)
CHILD_ENV_NO_ISOLATION = (
    '        return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}\n'
)

# M2:快照目标写死成私有地 —— r87 第一版的原样。
# 锚在「问子进程」那一行,不在 _child_tempdir 函数体里,好跟 M6 区分开。
ASK_CHILD = "        where = self._child_tempdir(env)        # ← 快照对象由子进程自己说\n"
HARDCODED_WHERE = "        where = Path(env[\"TMPDIR\"])            # 变异:快照目标写死,不再问子进程\n"

# M6:子进程报上来的路径被我们自己覆盖 —— 同一类谎,下在另一个地方。
#
## 第一版这里写的是 `Path(env.get("TMPDIR") or r.stdout.strip())`,实测**存活**。
## 原因是它是假变异(第 7 次):`_child_env` 永远设了 TMPDIR,所以
## `env.get("TMPDIR")` 恒为真值,改前改后在**所有可达状态下完全等价**。
## 现在改成真的说谎 —— 报**外层进程**的临时地,那才是「我们替子进程撒谎」。
CHILD_TEMPDIR_TAIL = "        return Path(r.stdout.strip())\n"
CHILD_TEMPDIR_LIES = "        return Path(tempfile.gettempdir())  # 变异:我们替子进程撒谎\n"

NOISE_MKDTEMP = (
    '            d = tempfile.mkdtemp()          # 真实 /tmp,不是私有地\n'
)
NOISE_NO_PLANT = (
    '            d = private_ref                  # 变异:不往真实 /tmp 种了\n'
)

NOISE_CALL = "        if noise is not None:\n            noise()\n"
NOISE_CALL_REMOVED = "        if False:  # 变异:不调 noise,窗口里根本没噪音\n            noise()\n"

TMP_DIRS_RETURN = (
    "            return {p.name for p in root.iterdir()\n"
    '                    if p.is_dir() and p.name.startswith("tmp")}\n'
)
TMP_DIRS_ALWAYS_EMPTY = "            return set()  # 变异:快照恒空\n"

CLAIMS = {
    "M1-删掉TMPDIR隔离": (
        # 声明的是**替换后整行**。早先写的是 '"PYTHONDONTWRITEBYTECODE": "1"}',
        # 它在改前就已经存在 —— 改前改后都满足,声明等于没有判别力(r83 的规矩)。
        ['        return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}\n'],
        ['"TMPDIR": str(private)'],
    ),
    # r88:这个位置原来还有一条 "M2-快照目标写死成私有地(r87第一版的错)"。
    # M2 实测是假变异(见上面那张表),改名成 C2 挪进覆盖变异时,两个列表
    # 都改了,唯独这条声明忘了删 —— 留下一条指向不存在变异的 CLAIMS。
    # r88 加的 `test_every_claim_has_a_mutant` 当场把它逮了出来。
    "M3-noise不再往真实tmp种噪音": (
        ["d = private_ref                  # 变异:不往真实 /tmp 种了"],
        ["d = tempfile.mkdtemp()          # 真实 /tmp,不是私有地"],
    ),
    "M4-窗口内不再调用noise": (
        ["if False:  # 变异:不调 noise,窗口里根本没噪音"],
        ["        if noise is not None:\n            noise()"],
    ),
    "M5-快照函数恒返回空集": (
        ["return set()  # 变异:快照恒空"],
        ['p.name.startswith("tmp")'],
    ),
    "M6-子进程tempdir被我们自己替它撒谎": (
        ["return Path(tempfile.gettempdir())  # 变异:我们替子进程撒谎"],
        ["        return Path(r.stdout.strip())"],
    ),
    "C1-噪音测试断言放宽成恒真": (
        ["            [], [],"],
        ['            strays, [],\n            f"别的进程在真实临时区里造了'],
    ),
    "C2-快照直接读TMPDIR而不问子进程": (
        ['where = Path(env["TMPDIR"])'],
        ["where = self._child_tempdir(env)"],
    ),
}

# M3 需要 `private_ref` 在作用域里,否则会 NameError —— 那不是「变异生效」
# 是「变异把文件改坏了」。这里让它指向**私有地里面**的一个路径,于是语法
# 合法、运行合法、且真的没在真实 /tmp 造出 tmp* 目录。
#
# 第一版写的是 `private_ref = tempfile.mkdtemp()`,注释还信誓旦旦写着
# 「落在私有地,不在真实 /tmp」—— 假的:那行跑在**外层**进程里,落点就是
# 真实 /tmp,噪音照种不误,变异存活,什么都没测到。第 8 次假变异。
PRIVATE_REF_DECL = "        planted: list[str] = []\n"
PRIVATE_REF_DECL_M3 = (
    "        planted: list[str] = []\n"
    "        private_ref = str(private / 'planted-inside-private')\n"
)

MUTANTS = [
    ("M1-删掉TMPDIR隔离",
     lambda p: _apply(p, CHILD_ENV, CHILD_ENV_NO_ISOLATION), False, (CRIT,)),
    ("M3-noise不再往真实tmp种噪音", lambda p: _apply(
        p, NOISE_MKDTEMP, NOISE_NO_PLANT,
        also=(PRIVATE_REF_DECL, PRIVATE_REF_DECL_M3)), False, (CRIT,)),
    ("M4-窗口内不再调用noise",
     lambda p: _apply(p, NOISE_CALL, NOISE_CALL_REMOVED), False, (CRIT,)),
    ("M5-快照函数恒返回空集",
     lambda p: _apply(p, TMP_DIRS_RETURN, TMP_DIRS_ALWAYS_EMPTY), False, (CRIT,)),
    ("M6-子进程tempdir被我们自己替它撒谎",
     lambda p: _apply(p, CHILD_TEMPDIR_TAIL, CHILD_TEMPDIR_LIES), False, (CRIT,)),
]

# C1/C2 实测存活,见 docstring。如实记,不做平账。
#
# C2 原来是实现变异 M2,期望被杀,实测**存活**。查清了:不是判据漏了,
# 是那个变异本身不成立 —— `env["TMPDIR"]` 就是那块私有地,子进程报的也是
# 它,写死和问子进程答案完全一样(第 9 次假变异)。所以改放覆盖变异,
# 并把它的真实含义写进名字:现在的设计**故意**有两条独立路径 ——
# 「问子进程」和「验 TMPDIR 真的被认」—— 删掉哪一条都不塌,这正是它
# 抗得住 r87 第一版那种错的原因。
COVERAGE_MUTANTS = [
    ("C1-噪音测试断言放宽成恒真", lambda p: _apply(
        p,
        '            strays, [],\n'
        '            f"别的进程在真实临时区里造了 {planted_dir.name},"\n',
        "            [], [],\n"
        '            f"别的进程在真实临时区里造了 {planted_dir.name},"\n'),
     True, (CRIT,)),
    ("C2-快照直接读TMPDIR而不问子进程", lambda p: _apply(
        p, ASK_CHILD, HARDCODED_WHERE), True, (CRIT,)),
]


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    for piece in CLAIMS[name][1]:
        n = original.count(piece)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {piece!r} 在改前出现 {n} 次 —— 指不准位置")


def _verify_claim(name: str, mutated: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    missing = [s for s in must_have if s not in mutated]
    leftover = [s for s in must_not if s in mutated]
    if missing or leftover:
        raise AssertionError(
            f"变异 {name!r} 的替换没有做到它名字声称的事: 缺 {missing} 仍在 {leftover}")


def _write_checked(path: pathlib.Path, out: str) -> None:
    import ast
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str, also: tuple | None = None) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    if also is not None and also[0] not in src:
        raise AssertionError(f"变异附加锚点没命中 {path.name}:{also[0][:70]!r}")
    out = src.replace(old, new, 1)
    if also is not None:
        out = out.replace(also[0], also[1], 1)
    _write_checked(path, out)


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=1800,
    )


def _sweep(mutants) -> list:
    out = []
    for name, mutate, expect, targets in mutants:
        backups = {t: t.read_bytes() for t in targets}
        try:
            try:
                _check_claim_points_at_one_place(
                    name, "\n".join(t.read_text(encoding="utf-8") for t in targets))
                for t in targets:
                    mutate(t)
                _verify_claim(name, "\n".join(
                    t.read_text(encoding="utf-8") for t in targets))
            except AssertionError as e:
                # 变异**自己**写坏了文件(锚点没命中 / 语法错 / 空操作)。
                # 早先这里直接往上抛,一条坏变异就把整个 run 打断,后面的
                # 变异一条都没跑 —— 报告出来,别让一条错盖住六条对。
                out.append((name, "BAD-MUTANT", str(e)[:400]))
                continue
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if any(bad in outp for bad in COLLECTION_FAILED):
                out.append((name, "BAD-SYNTAX", outp[-400:]))
                continue
            killed = r.returncode != 0
            detail = "killed" if killed else "survived"
            if killed == expect:
                detail = f"UNEXPECTED-{detail}"
            out.append((name, detail, outp[-500:] if killed == expect else ""))
        finally:
            for t, data in backups.items():
                t.write_bytes(data)
    return out


def main() -> int:
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r87 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:38s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:400])
    print(f"\nr87 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
