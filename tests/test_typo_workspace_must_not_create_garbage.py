"""r97:拼错一个 `-w`,磁盘上凭空多出一个**持久垃圾**工作区

## 实测到的退化路径(不是推测)

`Storage(workspace=...)` 会**静默自动创建**工作区。CLI 里有一批命令直接
调它、没先确认工作区存在,于是拼错一个 `-w` 的后果不是「报错」,而是:

    $ arl-lite monitor add example.com -w TYPO
    $ arl-lite monitor prune --older-than 30d -w TYPO
    rc=0                                    <- 一个「清理」命令,报成功
    $ arl-lite monitor remove nosuch -w TYPO
    workspace 'TYPO' created                <- 删除命令建出了工作区
    $ arl-lite monitor enable 1 -w TYPO
    monitor #1 not found                    <- 一边说找不到,一边建出了 TYPO
    $ arl-lite workspace list
    ...  TYPO ...                           <- 之后每一次 list 都要带上它

垃圾目录不是一次性的:它出现在之后**每一条** `workspace list` 里,而用户
从没要求过它。r56 建的 `_ensure_workspace_exists` 守卫当时只落在 7 条
只读命令上 —— monitor 这批**有写副作用**的命令一条都没经过那轮审。

## 本轮把守卫补到 4 条

`monitor add` / `remove` / `enable` / `prune`。

## r97 撤回了它自己的第二个结论(重要)

r97 初版还认定 `watch list -w prod` 报「工作区 'prod' 下没有 watch
target」是**一句假话**,于是也给 `cmd_watch_list` 加了守卫。
**那个结论是错的,守卫已撤回。** 依据是实测:

    watch add a.example.com -w teamA  →  rc=0
      建出 ~/.arl-lite/watch/teamA/watch.json,工作区目录一个都没建

watch 全系的 `-w` 是**给 watch 清单用的标签**,不是工作区引用(r70 早就
删掉了 `cmd_watch_add` 里那个没用过的 `Storage()`)。在这个契约下,
「标签 'prod' 下没有 watch target」是**真话** —— 那个标签底下确实一个
都没有。工具无从知道用户想的是 `default`,那不叫说谎,那叫照实回答。

守卫加在那里还造出一个**自相矛盾**,这是撤回的直接原因:

    watch add -w teamA   → rc=0,清单落盘
    watch list -w teamA  → rc=1,「workspace not found」

同一个名字,加得进去、列不出来,而清单就在磁盘上。
**加得进去的名字,必须列得出来。** 这条不变式由下面第 3 节守着。

## 登记制结构守卫:不在这儿,在既有那份判据里

「哪个 `cmd_*` 碰工作区、要么有守卫要么登记为例外」这件事,
`test_readonly_cmds_same_workspace_contract.py` 的 `EXCEPTIONS` + `_workspace_partition()`
**早就有了**(r69 立的)。r97 初版不知道,自己又手写了一份 ——
决策 #9 的又一处实例:两处手抄同一段逻辑,迟早漂。门禁的 `test_baseline`
当场把冲突逮出来(一份说「已豁免」,另一份说「未豁免」)。

所以本文件**不再**有登记表。r97 在那份既有判据里做的只是:
删掉 4 条因为加了守卫而失效的 `cmd_monitor_*` 豁免 —— 那正是它那条
stale 检测想干的事。判据自己会跟上,不需要第二份。
"""
from __future__ import annotations

import contextlib
import io
import pathlib

import pytest

from arl_lite.cli import main

REPO = pathlib.Path(__file__).resolve().parents[1]

# 拼错的名字。用一个在任何机器上都不可能真实存在的名字 ——
# 「恰好叫 TYPO 的工作区」会让判据在特定机器上假绿。
TYPO = "r97-no-such-ws"


def _run(*argv) -> tuple[int, str, str]:
    """跑一条 CLI,拿 (rc, stdout, stderr)

    必须把 stderr 也收进来:判据盯的是**用户看到什么**,
    而 `workspace not found` 走 stderr,漏了就测不到。
    """
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(list(argv))
        except SystemExit as e:                    # argparse 的用法错误
            rc = e.code if isinstance(e.code, int) else 1
    return rc, out.getvalue(), err.getvalue()


def _ws_root(home: pathlib.Path) -> pathlib.Path:
    return home / ".arl-lite" / "workspaces"


def _ws_dirs(home: pathlib.Path) -> list[str]:
    """磁盘上**真的**存在的工作区目录名

    空环境下连 `workspaces/` 本身都不该被创建 —— 所以目录不存在时
    返回空列表,而不是报错。
    """
    root = _ws_root(home)
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _make_ws(home: pathlib.Path, name: str = "default"):
    """建出一个工作区。

    显式传 `workspace_root` —— `tests/test_no_real_home_writes.py` 守着
    「Storage 构造必须给 root」,漏了会让判据往用户真实数据目录里写。
    """
    from arl_lite.db.storage import Storage

    return Storage(workspace=name, workspace_root=_ws_root(home))


@pytest.fixture
def empty_home(tmp_path, monkeypatch):
    """一台**什么都没有**的机器"""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── 一、主判据:拼错 `-w` 必须 rc=1,而且**磁盘上不能多出任何东西** ──

# (argv, 为什么这条会在磁盘上留垃圾)。这 4 条是 r97 实测出过副作用的全部命令。
TYPO_CASES = [
    (("monitor", "add", "example.com", "-w", TYPO),
     "新增命令:拼错就新建一个工作区"),
    (("monitor", "remove", "nosuch", "-w", TYPO),
     "删除命令:一条**删除**命令建出了一个工作区"),
    (("monitor", "enable", "1", "-w", TYPO),
     "启用命令:一边说 monitor 不存在,一边把工作区建了出来"),
    (("monitor", "prune", "--older-than", "30d", "-w", TYPO),
     "清理命令:报 rc=0 说成功,同时留下垃圾目录"),
]
IDS = [" ".join(c[0]) for c in TYPO_CASES]


@pytest.mark.parametrize("argv,why", TYPO_CASES, ids=IDS)
def test_typo_workspace_creates_nothing_on_disk(empty_home, argv, why):
    """拼错 `-w` → rc=1,且 `workspaces/` 下不能多出一个目录

    `rc` 和「磁盘上有没有垃圾」是**两件独立的事**,必须都查:
    r97 实测 `monitor prune -w TYPO` 就是 rc=0 **且**建了目录;
    只查 rc 会漏掉一半,只查目录会漏掉另一半。
    """
    before = _ws_dirs(empty_home)
    rc, out, err = _run(*argv)

    assert rc == 1, (
        f"`{' '.join(argv)}` 应该 rc=1,实际 rc={rc}。{why}\n"
        f"stdout={out!r}\nstderr={err!r}")
    assert _ws_dirs(empty_home) == before, (
        f"`{' '.join(argv)}` 在磁盘上建出了工作区 "
        f"{set(_ws_dirs(empty_home)) - set(before)}"
        f" —— 它会出现在之后每一条 `workspace list` 里,而用户从没要求过它")
    assert TYPO not in _ws_dirs(empty_home), (
        f"拼错出来的 {TYPO!r} 成了持久工作区 —— 这就是本轮要消灭的那个东西")


@pytest.mark.parametrize("argv,why", TYPO_CASES, ids=IDS)
def test_typo_workspace_says_workspace_not_found(empty_home, argv, why):
    """报错必须点名**工作区**,不能是别的失败理由

    `Storage` 的自动建库会把别的失败(找不到 monitor、没东西可清理)
    一路掩盖掉。只有当错误信息点了工作区的名,用户才知道自己是拼错了,
    而不是以为配置丢了。
    """
    rc, out, err = _run(*argv)
    assert "workspace not found" in err, (
        f"`{' '.join(argv)}` 的报错没有点名工作区:{err!r} —— 用户会以为"
        f"是别的配置问题。stdout={out!r}")


def test_typo_workspace_does_not_say_it_created_something(empty_home):
    """绝不许出现「workspace 'X' created」

    `Storage` 建库时会打这一行。它出现在**用户拼错了名字**的输出里,
    等于亲口告诉用户「我按你打的那个名字建了」—— 那正是要消灭的行为,
    而且是有理有据地被消灭掉。
    """
    for argv, _ in TYPO_CASES:
        rc, out, err = _run(*argv)
        for stream, name in ((out, "stdout"), (err, "stderr")):
            assert "created" not in stream.lower() or "workspace not found" in stream, (
                f"`{' '.join(argv)}` 的 {name} 里出现了「建出来了」:"
                f"{stream!r} —— 守卫生效时不该有这一句")


# ── 二、正控制:守卫不许误伤 —— 工作区存在时一切照旧 ──

def test_existing_workspace_normal_paths_still_work(empty_home):
    """r97 只该拦「拼错」,不该拦「工作区真的存在」

    反向控制:r97 之前这些路径都是 rc=0。如果修完之后它们变红,
    那不是修好了,是把功能改坏了。
    """
    _make_ws(empty_home)
    cases = [
        (("monitor", "add", "example.org"), 0),
        (("monitor", "list"), 0),
        (("monitor", "prune", "--older-than", "30d"), 0),
        (("query", "domains"), 0),
    ]
    for argv, want in cases:
        rc, out, err = _run(*argv)
        assert rc == want, (
            f"`{' '.join(argv)}` 工作区确实存在,应该 rc={want},实际 rc={rc}\n"
            f"stdout={out!r}\nstderr={err!r}")


def test_remove_of_a_real_monitor_still_works(empty_home):
    """`monitor remove` 加了守卫之后,**真删**这条路必须还通

    这条不是 `test_existing_workspace_normal_paths_still_work` 的重复:
    守卫拦的正是 `monitor remove`,所以「加得进去还得删得掉」这个
    往返必须单独钉住。
    """
    _make_ws(empty_home)
    assert _run("monitor", "add", "example.net")[0] == 0
    rc, out, err = _run("monitor", "remove", "example.net")
    assert rc == 0, f"真删一条 monitor 应该 rc=0,实际 {rc}({out!r} {err!r})"
    rc, out, _ = _run("monitor", "list")
    assert "example.net" not in out, f"删了还在列表里:{out!r}"


def test_prune_on_an_existing_empty_workspace_is_honest(empty_home):
    """工作区存在但没有过期项 → 报「没什么可删」,不是「删成功了」

    正控制:守卫只挡「工作区不存在」,对「存在但没东西可清理」
    一律放行 —— 那时候 `prune` 必须照实报,不许借 rc=0 蒙混。
    """
    _make_ws(empty_home)
    rc, out, err = _run("monitor", "prune", "--older-than", "30d", "--yes")
    assert rc == 0, f"工作区存在,prune 应该 rc=0,实际 {rc}({err!r})"


# ── 三、watch 的真实契约:r97 撤回守卫后钉住的那条不变式 ──

def test_watch_add_and_list_agree_on_the_same_name(empty_home):
    """**加得进去的名字,必须列得出来** —— 同一个 `-w`,两边都得 rc=0

    这条是 r97 撤回 `cmd_watch_list` 守卫的直接原因,也是防止下一个人
    「好心」把守卫加回去的钉子。r97 初版给 `watch list` 加了
    `_ensure_workspace_exists`,实测结果是:

        watch add  -w teamA  → rc=0,清单落盘
        watch list -w teamA → rc=1,「workspace not found」

    同理,`watch list` 报告 rc=0 且说「没有 watch target」也不是说谎:
    watch 的 `-w` 是**清单标签**,不是工作区引用(r70 删掉了
    `cmd_watch_add` 里那个没用过的 `Storage()`),标签底下确实没有 target。

    所以正确的判据是**两边一致**,不是「list 必须拒绝」。
    """
    rc_add, _, err_add = _run("watch", "add", "a.example.com", "-w", "teamA")
    assert rc_add == 0, f"`watch add -w teamA` 必须 rc=0,实际 {rc_add}({err_add!r})"

    rc, out, err = _run("watch", "list", "-w", "teamA")
    assert rc == 0, (
        f"刚用 `watch add -w teamA` 存进去的清单,`watch list -w teamA` "
        f"却说 rc={rc}({err!r}) —— 同一个名字一边能加一边列不出来")
    assert "a.example.com" in out, f"刚加的 target 不在列表里:{out!r}"


def test_watch_list_stays_scoped_to_its_own_label(empty_home):
    """两个标签各列各的,互不串(r70 的隔离契约不变)"""
    assert _run("watch", "add", "a.example.com", "-w", "teamA")[0] == 0
    assert _run("watch", "add", "b.example.com", "-w", "teamB")[0] == 0

    a = _run("watch", "list", "-w", "teamA")[1]
    b = _run("watch", "list", "-w", "teamB")[1]
    assert "a.example.com" in a and "b.example.com" not in a, f"teamA 看到了别人的:{a!r}"
    assert "b.example.com" in b and "a.example.com" not in b, f"teamB 看到了别人的:{b!r}"


def test_watch_list_on_an_unused_label_is_not_a_workspace_problem(empty_home):
    """空标签报「没有 watch target」+ rc=0,是真话,不是漏洞

    这条与上一条互为对照,也是 r97 初版判断错的地方:
    同样的措辞,在「这个标签真没有 target」时是真话。
    判据盯的是 `watch` 内部**自洽**,不是要求它去管工作区 ——
    它压根不碰工作区(实测 `watch add` 不建工作区目录)。
    """
    rc, out, err = _run("watch", "list", "-w", "never-used")
    assert rc == 0, f"空标签不该是错误,实际 rc={rc}({err!r})"
    assert "没有 watch target" in err or "没有 watch target" in out, (
        f"空标签就该说「没有 target」,实际 out={out!r} err={err!r}")
    assert _ws_dirs(empty_home) == [], (
        f"`watch list` 不该建出任何工作区目录,实测 {_ws_dirs(empty_home)} —— "
        f"watch 全系压根不碰工作区")


# ── 四、守卫本身不许成为新的副作用源 ──

def test_the_existence_check_creates_nothing_on_an_empty_machine(empty_home):
    """检查本身不许建库 —— 否则「检查有没有」就是「把它建出来」

    实测过的一道陷阱:`_ensure_workspace_exists` 只在 default 库**已存在**
    时才去查注册表,因为查注册表就得先有 default 库。哪天有人把那个前置
    条件去掉,这条命令就变成了「检查即创建」。
    """
    from arl_lite.cli import _ensure_workspace_exists

    assert _ws_dirs(empty_home) == [], "前置条件:这台机器上什么工作区都没有"
    rc = _ensure_workspace_exists(TYPO)
    assert rc == 1, "工作区不存在,检查必须返回 1"
    assert _ws_dirs(empty_home) == [], (
        f"检查工作区存不存在这个动作本身建出了 {_ws_dirs(empty_home)} —— "
        f"那就成了「问一句就多一个垃圾」")
    assert not (_ws_root(empty_home) / "default").exists(), (
        "查注册表用的 default 库也被建了出来 —— 检查自己制造了它要检查的东西")


def test_the_existence_check_accepts_a_workspace_that_really_exists(empty_home):
    """**正控制**:真实存在的工作区必须被认出来(否则守卫变成一刀切拒活)"""
    from arl_lite.cli import _ensure_workspace_exists

    _make_ws(empty_home)
    assert _ws_dirs(empty_home) == ["default"], "前置条件:default 确实建出来了"

    assert _ensure_workspace_exists("default") == 0, (
        "真实存在的 default 被判成不存在 —— 守卫变成一刀切拒活")
