"""源码里不许有 U+FFFD —— 一次多字节 UTF-8 被写坏会**永久留在版本库里**

## 实测:8 个文件、22 个字符,全是同一种病

r97 收尾时顺手扫了全仓,发现:

    arl_lite/core/watcher.py              3 个 U+FFFD
    arl_lite/devloop/accept.py            3 个
    arl_lite/cli.py                       5 个   (r97 顺手修了)
    devloop/mutcheck_r69.py               3 个
    tests/ 里 5 个文件                    14 个
                                    ── 合计 22 个 / 8 个文件

`U+FFFD`(REPLACEMENT CHARACTER)是解码失败时的替身。源码里出现它,
只有一种解释:**某个多字节 UTF-8 汉字在写盘时被截断/错编码**,然后那个
替身被原样写进了文件。

## 为什么这些字符躲过了**所有**门禁

因为它们全都落在**注释和 docstring** 里 —— 不影响任何运行行为。
七道门禁里没有一道看注释:`no_thirdparty_import` 看 import、
`no_import_cycle` 看依赖图、`loc_budget` 数行数、`doc_freshness` 看文档
引用。**没有一道会发现「有 22 个字是乱码」。**

危害也不是运行时(那 22 处都不在代码里),而是:
- 读注释的人看到乱码,一句解释就此失效;
- 更要紧的是**它证明了写盘链路会吃掉汉字**,而这条链路**每次写文件
  都在用**。没有守卫,下一次继续吃,谁也不知道。

## 破坏是发生在**首次写入**时,不是后来改坏的

逐个查 `git log -S`:

    watcher.py       损坏引入于 249e361 —— 正是创建这段注释的那个提交
    accept.py        损坏引入于 b0f7cb8 —— 同上
    monitor_field_   损坏引入于 0f279d4 —— 同上

**没有更早的版本可以对照。** 所以那 22 处的原文是**不可找回**的,
只能按上下文重建(重建 ≠ 找回,措辞未必与原作者一致)。
这件事本身就是守卫存在的理由:损坏发生在**写下的那一刻**,
事后再怎么查也只知道「坏了」,不知道「本来该是什么」。

## 这条判据自己踩过一次坑:字面量把自己告发了

首版在测试样本里**直接写了 U+FFFD 字符**,于是主判据一跑就把自己
列成了 offender —— 10 处,全在本文件里。

修法是所有样本改成 `chr(0xFFFD)` 拼出来,于是本文件自己一个 U+FFFD
都没有。**一条扫「有没有乱码」的守卫,自己必须干净** ——
不然它每次跑都在报自己,而人只会觉得「这守卫一直吵,先不管它」。
"""
from __future__ import annotations

import pathlib

# 用 chr() 而不是字面量:本文件自己也在这条判据的扫描范围内,
# 写了字面量就等于自己告发自己(首版就是这么翻车的)。
REPLACEMENT = chr(0xFFFD)

REPO = pathlib.Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules"}


def _find_replacement_chars(text: str) -> list[tuple[int, int]]:
    """找出 text 里每个 U+FFFD 的 (行号, 列号),行号从 1 开始

    纯函数:只认喂进来的 text,不去碰文件系统,也不依赖任何全局状态。
    判据的所有区分力都在这一个函数里,所以它必须能被单独喂样本验证。
    """
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        start = 0
        while True:
            col = line.find(REPLACEMENT, start)
            if col < 0:
                break
            hits.append((lineno, col + 1))
            start = col + 1
    return hits


def _python_sources(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*.py")
                  if not SKIP_PARTS & set(p.parts))


def test_the_scanner_reports_what_it_was_fed():
    """**正控制**:扫描器必须真的会报

    判据恒过是最大的风险,不是「写得太严」(r90)。一个从不报警的守卫
    等于没写,还会占着一个「我们在防这个」的位置。所以先证明这把
    尺子能量出东西:

    - 有 U+FFFD 就得报,连报在哪一行、第几个都得对;
    - 干净的文本必须**一条都不报** —— 只报得出「有的情况」、
      永远不出「没报」的守卫,是那种最省事也最没用的写法。
    """
    R = REPLACEMENT
    # 期望值写成死的:它就是一把独立的尺子。跟着实现一起算出来的
    # 期望值,永远等于实现自己说的数,什么也证明不了。
    assert _find_replacement_chars(f"这一行有{R}坏字\n{R}") == [(1, 5), (2, 1)], (
        "扫描器没找对位置 —— 报出来的行列和实际不符,"
        "那么它在真实文件上报的东西也就不可信")
    assert _find_replacement_chars("干干净净\n没有坏字\n") == [], (
        "干净文本被判成有坏字 —— 那它会满仓库乱叫,最后没人再听它的")
    assert _find_replacement_chars("") == [], "空文本必须返回空,不是崩掉"


def test_the_constant_really_is_the_replacement_character():
    """**M4 变异逮到的洞**:码位必须独立核对,不能自己证明自己

    变异测试把 `REPLACEMENT = chr(0xFFFD)` 改成 `chr(0xFFFE)`,
    **四条判据照样全绿**。原因很直白:测试样本也是用 `REPLACEMENT`
    拼出来的 —— 常量改错,样本跟着一起错,扫描器找 U+FFFE、样本里正好
    是 U+FFFE,自洽。

    真实后果不是「判据红了」,而是**守卫静默失效**:哪天有人把这个
    码位打错,真正的 U+FFFD 一个都扫不出来,而测试一片绿。
    **一个恒过的守卫比没有守卫更坏** —— 它还占着一个「我们在防这个」
    的位置(r90 的规矩,这里又用上一次)。

    修法是拿一个**和这个常量无关**的来源去核对它:`unicodedata` 按名字
    查,查出来就是 U+FFFD。它不认识本文件里的常量,也不该认识。
    """
    import unicodedata

    assert REPLACEMENT == unicodedata.lookup("REPLACEMENT CHARACTER"), (
        f"REPLACEMENT = {REPLACEMENT!r},不是 U+FFFD —— "
        f"扫的码位错了,守卫会静默失效,而所有样本会跟着一起错,全绿")
    assert REPLACEMENT == chr(0xFFFD), "对不上 chr(0xFFFD) 本身"


def test_the_scanner_handles_a_replacement_char_at_every_position():
    """U+FFFD 在行首/行尾/连续出现,一个都不许漏

    r97 实测的 22 处里,FFFD 的**个数和原字符数对不上**
    (有的 1 个汉字留下 3 个替身,有的只留 2 个)。所以这个检查
    不能靠「数一数有几个」,必须逐个位置扫 —— 否则会漏掉连续的那几个。
    """
    R = REPLACEMENT
    assert _find_replacement_chars(R) == [(1, 1)], "行首一个"
    assert _find_replacement_chars(f"ab{R}") == [(1, 3)], "行尾一个"
    assert _find_replacement_chars(R * 3) == [(1, 1), (1, 2), (1, 3)], (
        "连续三个必须报三个 —— 只报一个就说明它在数『行』而不是『字』")
    assert _find_replacement_chars("abc\ndef\n" + R) == [(3, 1)], "行号不能算错行"


def test_no_source_file_carries_a_replacement_char():
    """主判据:仓库里任何 `.py` 都不许有 U+FFFD

    报错里带上 `文件:行:列`,是因为下一次出现时那正是要改的位置;
    只报「有 N 个」等于让人自己去找 —— 而这件事上一轮就没人做。
    """
    offenders = []
    for path in _python_sources(REPO):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            # 严格更糟的形态:文件根本不是合法 UTF-8。
            offenders.append(f"{path.relative_to(REPO)}: 不是合法 UTF-8({e.reason})")
            continue
        for lineno, col in _find_replacement_chars(text):
            offenders.append(f"{path.relative_to(REPO)}:{lineno}:{col}")

    assert not offenders, (
        "源码里出现 U+FFFD —— 说明有汉字在**写盘那一刻**被写坏了:\n  "
        + "\n  ".join(offenders)
        + "\n每个 U+FFFD 都是一段被毁的 UTF-8。修的时候按上下文重建"
        "(原文多半已不可找回:损坏往往和内容首次入库是同一次写入)。")

    # 正控制:扫描范围不能是空的。扫不到文件的守卫等于没写(r91 栽过)。
    scanned = _python_sources(REPO)
    assert len(scanned) >= 90, (
        f"只扫到 {len(scanned)} 个 .py 文件,扫描范围不对 —— "
        f"这条判据在空集上永远是绿的")


def test_the_guard_would_actually_fail_on_a_corrupted_file(tmp_path):
    """**端到端正控制**:造一个坏文件,确认完整收集流程能把它算出来

    光验证纯函数不够 —— 纯函数对了但「怎么在仓库里找文件」写错了,
    主判据照样绿。所以这里跑一遍**完整的收集流程**,只是把根目录换成
    一个装着坏文件的临时目录。

    这里查的就是主判据真正用的那两个函数,不是另写一遍逻辑。
    """
    (tmp_path / "clean.py").write_text("# 没有坏字\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text(
        f"# 有个坏字{REPLACEMENT}\n", encoding="utf-8")

    found = []
    for path in _python_sources(tmp_path):
        for lineno, _col in _find_replacement_chars(
                path.read_text(encoding="utf-8")):
            found.append(f"{path.name}:{lineno}")

    assert found == ["broken.py:1"], (
        f"完整的收集流程没能把坏文件算出来,实际 {found} —— "
        f"那么主判据在真实仓库上大概也没在扫")
