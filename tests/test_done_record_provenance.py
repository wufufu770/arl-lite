"""r34:引擎写下的每条 done 都必须自带 provenance,否则假账和真账长得一样

## 这个 bug 是怎么被发现的

r33 收尾时队列播报耗尽,我回头查 `confidence-risk` 这条为什么是 done
却没在 `backlog.md` 里打完成标记。挖出来的 note 是:

    unmarked from done: r22:引擎在无 build_fn 时「门禁绿就标 done」,
    把这条标成了 done,但我根本没做 —— note 为空是证据。

这条 note 推理的链条是:**note 为空 → 活没做**。

链条的第一环是真的 —— `q.finish()` 成功路径一个字都不写,
`devloop round` 标出来的每条 done,`note` 全是空串。
但结论是错的:`docs/CONFIDENCE_VS_RISK.md` **r22 就产出了**
(commit 7c7cec3,文档开头自己写着"轮次:r22")。活做了,
只是 note 没写。我拿"没写"当"没做"的证据,写了一条假账去指控它,
又按这条假账把它 unmark 掉 —— 记录反而更脏了。

## 为什么要修机制而不是修那一条记录

把 `confidence-risk` 的 note 改对,只能擦掉这一处擦痕。真正的问题是
**引擎的 done 记录和人手编的假账在字段上无法区分**。r22 那次误判不是
我一时糊涂:在一个 note 一律为空的表里,"空"本来就承载不了任何信息,
我却拿它当信息用了。

失败路径一直有 note(`release` 传的是 `gates failed: ...`),
只有成功路径是空的。这个不对称正是缺陷本身 ——
出问题时看得到依据,一切正常时反而什么都没有。

所以下面这几条守的是:**done 的 provenance 由引擎自己写,人不用记得写**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from arl_lite.devloop.gates import GateResult
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.queue import Item, Queue

REPO = Path(__file__).resolve().parents[1]

GREEN = [GateResult("fake", True, "ok", 0, 0, True)]


@pytest.fixture
def loop(tmp_path, monkeypatch):
    lp = Loop(REPO, state_dir=tmp_path)
    (tmp_path / "devloop").mkdir(exist_ok=True)
    (tmp_path / "devloop" / "backlog.md").write_text(
        "# backlog\n\n- [P1] change : 播种出来的假条目 |  | test -f 不存在\n",
        encoding="utf-8")
    monkeypatch.setattr(lp.gates, "run_all", lambda repo, only=None: list(GREEN))
    return lp


def _q(loop) -> Queue:
    return Queue(loop.dev_dir / "queue.json")


def _seed(q: Queue, item_id: str) -> None:
    q.add(Item(id=item_id, title=f"标题 {item_id}", priority=2,
               kind="change", verify="true", detail="", tags=[]))  # r35: 占位 verify 必须是**通过**的,收尾闸门会真跑它


# --- 引擎写的 done 必须自带 provenance ---

def test_round_done_record_is_not_untagged(loop):
    """round 标 done 之后,note 不能是空的

    这条断言直接对着 r22 的误判来源:空 note 曾被当成"活没做"的证据。
    空 note 之所以能被这样用,是因为它跟假账长得一样。
    """
    _seed(_q(loop), "task-x")
    loop.round(item_id="task-x")

    it = next(i for i in _q(loop).load() if i.id == "task-x")
    assert it.status == "done"
    assert it.note.strip(), (
        "引擎写下的 done 没有 provenance —— 与人手编的假账无法区分"
    )


def test_provenance_names_the_round_and_the_assertion_source(loop):
    """provenance 要同时说清"第几轮"和"谁断言的完成"

    只写一个泛泛的 "done" 没用:那还是分不出真账假账。要能拿它去
    对 state.json 的 history 交叉核对 —— 两边写的必须是同一件事。
    """
    _seed(_q(loop), "task-x")
    out = loop.round(item_id="task-x")

    it = next(i for i in _q(loop).load() if i.id == "task-x")
    assert f"r{out.record.round}" in it.note, (
        f"provenance 里没有轮次:{it.note!r},对不上 history 的第 "
        f"{out.record.round} 轮"
    )
    assert out.record.completion_source in it.note, (
        f"provenance 里没有断言来源:{it.note!r};"
        f"record 说的是 {out.record.completion_source!r}"
    )


def test_operator_assertion_is_visible_in_the_queue_record(loop):
    """纯人工模式下,队列记录里要能看出完成是**人工断言**的

    这一轮没有 build_fn,引擎只看到门禁全绿,看不到活干了没有。
    引擎替执行者背书是这套循环最大的记账风险,而队列里的 done 记录
    是留得最久的凭据 —— 它必须自己承认这一点。
    """
    _seed(_q(loop), "task-x")
    out = loop.round(item_id="task-x")

    assert out.record.completion_source == "operator", "本轮无 build_fn,应记 operator"
    it = next(i for i in _q(loop).load() if i.id == "task-x")
    assert "operator" in it.note, (
        f"队列记录看不出这是人工断言:{it.note!r}"
    )


def test_build_fn_completion_is_tagged_differently(loop):
    """有 build_fn 时 provenance 要标成 build_fn,和 operator 区分得开

    这条是上一条的**反证**。如果两条路写出来的 note 一样,
    "谁断言的完成"这个信息就等于没记 —— 那正是 r22 推理链断掉的地方。
    """
    _seed(_q(loop), "task-x")
    loop._build_fn = lambda item: (True, "干了")

    out = loop.round(item_id="task-x")

    assert out.record.completion_source == "build_fn"
    it = next(i for i in _q(loop).load() if i.id == "task-x")
    assert "build_fn" in it.note, (
        f"有执行器却没标 build_fn:{it.note!r}"
    )
    assert "operator" not in it.note, (
        f"有执行器的完成被标成了人工断言:{it.note!r}"
    )


# --- finish 的 note 是追加,不是覆盖 ---

def test_finish_appends_to_an_existing_note(tmp_path):
    """人写过的 note 不能被引擎的 provenance 冲掉

    `confidence-status` 那条 note 记着"验收方式是 CLI 导出 HTML 后 grep,
    不是声明" —— 这种信息是这条 done 最有价值的部分。追加而不是覆盖。

    上一版这条测试写的是 `assert "人写的" in note or note.count("|") >= 0`,
    第二个分支恒真,于是**人写的部分被冲掉也照样绿**。恒真分支不是
    "宽松断言",是把这条测试作废了。
    """
    q = Queue(tmp_path / "queue.json")
    q.add(Item(id="t", title="t", priority=2, kind="change",
               verify="true", detail="", tags=[], note="人写的:验收方式是 grep 不是声明"))
    it = q.claim(owner="o", item_id="t")

    ok = q.finish(it.id, ok=True, owner="o", round_no=3, note="done r3 · source=operator")

    after = next(i for i in q.load() if i.id == "t")
    assert ok is True
    assert "人写的:验收方式是 grep 不是声明" in after.note, (
        f"引擎的 provenance 冲掉了人写的部分:{after.note!r}"
    )
    assert "source=operator" in after.note, (
        f"引擎的 provenance 没写进去:{after.note!r}"
    )


def test_finish_without_note_still_works(tmp_path):
    """不传 note 的老调用方式不能被这次改动打断

    `arl-lite devloop finish` 和既有测试都走这条路径。
    """
    q = Queue(tmp_path / "queue.json")
    q.add(Item(id="t", title="t", priority=2, kind="change",
               verify="true", detail="", tags=[]))
    it = q.claim(owner="o", item_id="t")

    ok = q.finish(it.id, ok=True, owner="o", round_no=3)

    after = next(i for i in q.load() if i.id == "t")
    assert ok is True
    assert after.status == "done"
    assert after.done_round == 3


def test_finish_without_note_leaves_an_existing_note_untouched(tmp_path):
    """不传 note 时,已有的 note 要**原样**留着,不能多个尾巴

    这条是被变异测试逼出来的:把 `if note:` 守卫删掉,7 条测试照样全绿。
    但删掉之后,`arl-lite devloop finish`(它不传 note)碰上**已经有人
    写过 note** 的条目,会执行 `note + " | " + ""`,给记录缀上一个悬空的
    `" | "`。记录就开始慢慢长出这种垃圾:

        r21 实际完成:... | done r22 · source=operator |

    恒绿说明没测到,不是没影响。
    """
    q = Queue(tmp_path / "queue.json")
    q.add(Item(id="t", title="t", priority=2, kind="change",
               verify="true", detail="", tags=[], note="r21 实际完成:验收方式是 grep"))
    it = q.claim(owner="o", item_id="t")

    q.finish(it.id, ok=True, owner="o", round_no=22)

    after = next(i for i in q.load() if i.id == "t")
    assert after.note == "r21 实际完成:验收方式是 grep", (
        f"没传 note 却把记录改动了:{after.note!r}"
    )


def test_failed_finish_does_not_write_a_done_provenance(tmp_path):
    """退回 pending 时不能写 done 的 provenance

    否则一条"退回 pending"的记录上会挂着 "done rN" 字样,
    读记录的人(和下一轮的播种)会被误导。
    """
    q = Queue(tmp_path / "queue.json")
    q.add(Item(id="t", title="t", priority=2, kind="change",
               verify="true", detail="", tags=[]))
    it = q.claim(owner="o", item_id="t")

    q.finish(it.id, ok=False, owner="o", note="done r9 · source=operator")

    after = next(i for i in q.load() if i.id == "t")
    assert after.status == "pending"
    assert "done r9" not in after.note, (
        f"退回 pending 却写上了 done 的 provenance:{after.note!r}"
    )


def test_cli_done_item_also_writes_provenance(tmp_path):
    """`devloop done-item` 这扇门以前也漏了 note —— r36 补上

    r34 只把 provenance 接在了 `round()` 上,`arl-lite devloop done-item`
    这条路直接调 `q.finish(...)` 而**不传 note**,于是手工交上来的活一样是
    空凭据。r36 补记 r35 那两条从未进过队列的记录时,一眼看见 note 全空。

    手工交活正是最需要凭据的场景:agent 独立干活,交活发生在两次 round
    之间,没有 `round()` 替它写任何东西。

    判据是「是不是**空**」而不是「内容长什么样」—— 这条守的是
    「这扇门有没有漏」,格式改动不该让它红。
    """
    from arl_lite.devloop import cli as climod

    d = tmp_path
    (d / "devloop").mkdir()
    q = Queue(d / "devloop" / "queue.json")
    q.add(Item(id="hand-done", title="手工交上来的活", priority=1, kind="change",
               verify="true", detail="", tags=[]))
    q.claim(owner="agent-x", item_id="hand-done")

    class _Args:
        item_id = "hand-done"
        owner = "agent-x"
        fail = False

    real_loop = climod._loop
    try:
        class _FakeLoop:
            dev_dir = d / "devloop"

            def status(self):
                return type("S", (), {"round": 12})()
        climod._loop = lambda: _FakeLoop()
        climod._cmd_finish_item(_Args())
    finally:
        climod._loop = real_loop

    after = next(i for i in Queue(d / "devloop" / "queue.json").load()
                 if i.id == "hand-done")
    assert after.status == "done"
    assert after.note.strip(), (
        "devloop done-item 交出来的活没有凭据 —— 和引擎写的 done "
        "长得一样,分不出真假"
    )


# --- 同一类病:docstring 看着写了,其实没写成 ---

def test_public_queue_methods_keep_their_docstrings():
    """`def f():` 后面**第一句**必须是 docstring,不能是 `with`

    同一个病,换了个马甲:docstring 写在 `with self._locked(...)` 里面,
    于是它只是个被求值后丢弃的字符串字面量 —— `__doc__` 是 None。
    源码里明明"有注释",`help()` 里却什么都看不到。

    r34 实测中招 5 处:`add` / `seed_if_empty` / `repair_duplicates` /
    `drop` / `unmark`,全是 r32 加锁时把 `with` 提到了函数体开头。
    加锁本身是对的,但它把每个方法的"为什么"都变成了不可见的。

    这几条方法恰好是这套循环里**最需要解释**的几个(为什么加锁、
    为什么 drop 要带理由、为什么 unmark 只认唯一命中)。让它们的
    理由不可见,比没写还糟。
    """
    import ast
    import inspect
    from arl_lite.devloop import queue as qmod

    # 走 AST 而不是文本匹配 —— 文本里找 `def x` + `with` 会被嵌套的
    # 缩进骗到(这几轮已经踩过 r25/r26/r30/r31 四次)。
    tree = ast.parse(inspect.getsource(qmod))
    swallowed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, (ast.With, ast.Try, ast.For, ast.While)):
            continue
        inner = first.body[0] if getattr(first, "body", None) else None
        if (isinstance(inner, ast.Expr) and isinstance(inner.value, ast.Constant)
                and isinstance(inner.value.value, str)):
            swallowed.append(f"{node.name}() @line {inner.lineno}")

    assert not swallowed, (
        "这些方法的 docstring 被 with/try 吞掉了(`__doc__` 是 None):"
        + ", ".join(swallowed)
    )


def test_the_five_locked_methods_expose_their_rationale():
    """r32 加锁的那五个方法,理由必须真的能读到

    上一条只保证"有 docstring"。这条钉住"有**实质内容**" ——
    一个空 docstring 同样能骗过上一条,而这五个方法的理由正是
    后来者最需要的。
    """
    from arl_lite.devloop.queue import Queue

    for name in ("add", "seed_if_empty", "repair_duplicates", "drop", "unmark"):
        doc = getattr(Queue, name).__doc__ or ""
        assert len(doc.strip()) >= 20, f"{name}() 的 docstring 是空的或只有一句话"
