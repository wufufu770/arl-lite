"""r107:轮次索引不能停摆 —— 补索引容易,不补它就烂在那儿更容易

## 起于 r103 写下的一句错话,又被 r106 查清

r103 收尾时我在 `backlog.md` 里写:

> r53–r102 每轮的来龙去脉**全部只存在于一个不进版本库的文件里** ——
> 换台机器、或者 `git clean` 之后就没了。**这些内容压根就不在库里。**

r106 实测 `git log` 推翻了它:**r70–r105 每轮都有 1654–6597 字符的
commit message,全部入库、永久可查**;r70 之前用 `fix:` / `v0.7.8:` 之类的前缀,
正文也有 729–4179 字符。**丢的只是索引,不是记录。**

所以 r107 补的是**索引**:`devloop/ROUND_INDEX.md`,从 `git log` 生成,
每轮一行(轮次 / 提交 hash / 标题 / 这一轮做了什么)。

**为什么单独一个文件,不塞进 `backlog.md`**:`backlog.md` 有两条硬约束 ——
字段分隔符是竖线加空格(detail 里一个都不能有,r106 在同一条记录里连踩两次),
而且**标了 ✅ 的行必须在 `queue.json` 里是 done/dropped**。补 37 轮索引若全标 ✅,
就得往队列里补 37 条 done 记录 —— 那是拿「待办系统」当「日志」用,两码事。

## 这份判据要防的是「索引自己烂掉」

一个索引如果没有东西在守,它的失效方式和不存在一模一样。r103 那句错话能
存在整整一轮,就是因为**没人查过「到底还剩多少索引」**。

## 最新一轮允许缺席 —— 这是本文件最容易写错的地方

索引靠脚本从 `git log` 生成,而**每一轮 commit 都会让索引立刻过期**。
如果判据要求「每条 `rNN:` 开头的 commit 都必须在索引里」,那么:

- r107 生成索引(覆盖到 r106)→ commit r107 → **索引当场缺 r107**
- 下一轮跑门禁 → 红 → 每一轮都会红

**一条永远红的门禁等于没有门禁**(r104 刚把这件事的代价付了一遍)。
所以这里显式放过 `HEAD` 那一轮,并把理由写死:它刚产生,索引在下一轮补。
**这条规则是取舍,不是漏洞** —— 代价是最多一轮的索引滞后。

## 判据

- 每条 `rNN:` 开头的 commit(除最新一轮外)都在索引里
- 索引里每个 hash 都对应一个真实存在的 commit
- 索引里的轮次号不重复、且严格递增
- 索引里必须**有内容**(否则「全删了」会让前两条恒绿)
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "devloop" / "ROUND_INDEX.md"

_ROW_RE = re.compile(r"^\|\s*r(\d+)\s*\|\s*`([0-9a-f]{7})`\s*\|", re.M)


def _round_commits() -> dict[int, str]:
    """git 里所有 `rNN:` 开头的提交 → {轮次: 短 hash}"""
    out = subprocess.run(
        ["git", "log", "--format=%H%x1f%s"],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    ).stdout
    got: dict[int, str] = {}
    for line in out.splitlines():
        if "\x1f" not in line:
            continue
        h, subj = line.split("\x1f", 1)
        m = re.match(r"^r(\d+):", subj)
        if m:
            got[int(m.group(1))] = h[:7]
    return got


def _index_rows() -> list[tuple[int, str]]:
    return [(int(m.group(1)), m.group(2)) for m in _ROW_RE.finditer(
        INDEX.read_text(encoding="utf-8"))]


# =====================================================================
# 索引本身
# =====================================================================


def test_the_index_is_not_empty():
    """前置条件:索引里得有东西。

    少了这条,「整个索引被清空」会让下面两条一起恒绿 —— 因为空索引
    确实「没有漏掉的轮次」也没有「不存在的 hash」。
    """
    rows = _index_rows()
    assert len(rows) >= 30, (
        f"索引里只有 {len(rows)} 轮 —— 少于 30 轮说明它被清过或者生成脚本坏了。"
        f"完整记录在 commit message 里(入库),这份文件只是索引"
    )


def test_round_numbers_are_unique_and_strictly_increasing():
    """重复的轮次号意味着生成脚本把两行写成了同一个,或有人手工插过。"""
    rows = [r for r, _ in _index_rows()]
    assert rows == sorted(rows), f"轮次号不是递增的:{rows[:12]}…"
    dupes = sorted({r for r in rows if rows.count(r) > 1})
    assert not dupes, f"这些轮次号出现了不止一次:{dupes}"


def test_every_hash_in_the_index_is_a_real_commit():
    """索引里的 hash 必须真的存在 —— 否则它指向的是空气。"""
    real = {h for h in subprocess.run(
        ["git", "log", "--format=%h"], capture_output=True, text=True,
        cwd=REPO, timeout=120).stdout.split()}
    bogus = sorted({h for _, h in _index_rows()} - real)
    assert not bogus, f"索引里这些 hash 在 git 里不存在:{bogus}"


# =====================================================================
# 覆盖率:这才是「索引不烂」的那条
# =====================================================================


def test_every_round_commit_except_the_newest_is_indexed():
    """除最新一轮外,每条 `rNN:` 开头的 commit 都得在索引里。

    ## 放过最新一轮,是有意的

    索引从 `git log` 生成,而每一轮 commit 都会让它当场过期:r107 生成索引
    (覆盖到 r106)→ commit r107 → 索引此刻缺 r107。如果这里不放行,
    **下一轮门禁必红,而且会一直红下去**。一条永远红的门禁等于没有门禁 ——
    r104 刚为这件事付过代价(把公网 fixture 换成本机)。

    代价是明摆着的:最多一轮的索引滞后。换来的是索引不会烂在那儿没人管。
    """
    commits = _round_commits()
    assert commits, "git 里一条 `rNN:` 开头的提交都找不到 —— 范围不对"
    head = max(commits)
    indexed = {r for r, _ in _index_rows()}

    missing = sorted(r for r in commits if r != head and r not in indexed)
    assert not missing, (
        f"这些轮次有 commit 但索引里没有:{missing}\n"
        f"  (r{head} 是最新一轮,本文件**故意**放过它 —— 它刚产生,下一轮补)\n"
        f"  补法:重跑生成脚本,或者手工加一行"
    )


def test_the_index_does_not_claim_to_cover_rounds_that_do_not_exist():
    """反向:索引不许收录不存在的轮次。

    只查「漏」不查「多」的话,凭空插一行 r999 也过得去 —— 而那是在
    索引里放一条指向空气的记录,比缺一行更难发现。
    """
    commits = _round_commits()
    phantom = sorted({r for r, _ in _index_rows()} - set(commits))
    assert not phantom, f"索引里这些轮次在 git 里没有对应的 commit:{phantom}"


def test_the_titles_starting_round_is_the_first_round_that_actually_exists():
    """标题里**第一个**出现的轮次号,必须真的是 git 里最早的那个轮次

    r110 实测:`ROUND_INDEX.md` 标题写「# 轮次索引(r53 起)」,而
    `git log --format=%s` 里带 `rNN:` 前缀的提交是 **r70 起** —— r53–r69
    **根本不存在**这种形式的提交。标题是照着「devloop 从 r53 开始」想的,
    不是照着 git 里实际有什么查的。

    下面那条 `test_the_index_does_not_claim_to_cover_rounds_that_do_not_exist`
    查的是**行**,查不到**标题**:标题里那个 r53 不是一行,所以它压根不在那条
    的检查范围里。这和 r109 查出的「解析失败的行对所有判据隐形」是同一族 ——
    **每条判据都有一个「不在它遍历范围内」的死角**。

    ## 判据为什么盯「第一个」而不是「所有」

    现在的标题是「r70 起,**不是 r53** —— 标题原来写错了」,r53 仍然出现在
    标题里(作为被更正的错误)。所以规则是「**第一个**出现的那个才是它声称的
    起始轮次」,不是「标题里不许出现别的轮次」。后者会逼着人把更正也删掉 ——
    那是 r108 明确反对的:记录为什么这么改,和改完之后是什么,都要留着。

    `backlog.md` 的文件头也一起查:两个索引对「最早一轮」的说法不能分叉。
    """
    commits = _round_commits()
    assert commits, "git 里一条 `rNN:` 开头的提交都找不到 —— 范围不对"

    title = INDEX.read_text(encoding="utf-8").splitlines()[0]
    m = re.search(r"r(\d+)", title)
    assert m, f"标题里没有轮次号,查不到它声称的起始轮次:{title!r}"
    claimed = int(m.group(1))
    assert claimed == min(commits), (
        f"标题声称从 r{claimed} 起,而 git 里最早的 `rNN:` 提交是 r{min(commits)}"
        f"({sorted(commits)[:4]}…)\n"
        f"  标题: {title}\n"
        f"  查证: git log --format=%s —— 两秒钟的事,别靠印象写轮次号"
    )

    # backlog.md 的文件头对「最早一轮」的说法不能和索引分叉
    text = (REPO / "devloop" / "backlog.md").read_text(encoding="utf-8")
    a = "<!-- devloop:" + "where-records-live -->"
    b = "<!-- /devloop:" + "where-records-live -->"
    assert text.count(a) == 1 and text.count(b) == 1, (
        "backlog.md 的「记录在哪」标记块缺失或重复 —— "
        "见 tests/test_devloop_queue_invariant.py 里那条 verify")
    blk = text.split(a)[1].split(b)[0]
    hm = re.search(r"r(\d+)", blk)
    assert hm, f"backlog.md 文件头里没有轮次号:{blk[:80]!r}"
    assert int(hm.group(1)) == min(commits), (
        f"backlog.md 文件头说最早是 r{hm.group(1)},git 里是 r{min(commits)},"
        f"而 ROUND_INDEX.md 标题说的是 r{claimed} —— 三个说法必须一致")
