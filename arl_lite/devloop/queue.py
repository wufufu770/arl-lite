"""arl_lite.devloop.queue — 待办队列模块(自持迭代协议的"下一步供给器")

设计意图
--------
这是 arl-lite 自持迭代协议(arl_lite.devloop.protocol)的"动力源"。

协议不变量:`待办队列永远非空`(见 devloop/__init__.py 第 12 行)。当所有 item
都 done/dropped 时,系统不能停下,必须能自动从两个来源补充新待办:

    Tier 1 — 人工追加:devloop/backlog.md(每行一条,人写的长期待办清单)
    Tier 2 — 保底:3 条长期演进项(依赖审计 / 性能基线 / 误报率实测),
            **只提已经到期的**(verify 当前不通过的)

原来中间还有一层「扫项目现状自动推导」,第 16 轮整层删除 ——
它的五条检查逐条实测没有一条能产出真活,详见 `REMOVED_TIER2_WHY`。

队列的核心职责:
    1. 持久化:JSON 落盘,原子写(tmp + os.replace),崩溃后可恢复
    2. 调度:next() 按 (priority asc, created_round asc, id asc) 稳定排序
    3. 状态机:pending → in_progress → done | dropped,attempts 在 in_progress 时 +1
    4. 自愈:seed_if_empty() 永远能产生至少 1 条新 item(协议不变量)
    5. 多 agent:claim / release / finish / recover_stale 是加锁的原子操作,
       绕开它们直接 load/save 在并发下会丢更新

零依赖:只用标准库(json / os / subprocess / tempfile / pathlib /
dataclasses / re / time / logging / fcntl / msvcrt)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import hashlib
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# ─── 协议常量(对外公开,protocol.py 引用) ────────────────────────────────
P0 = 0  # 紧急:P0=阻断生产/安全洞
P1 = 1  # 高:本轮必须做
P2 = 2  # 中:本迭代内做
P3 = 3  # 低:有精力再说

_VALID_KINDS = {"change", "test", "doc", "research", "refactor"}
_VALID_STATUS = {"pending", "in_progress", "done", "dropped"}

# 复查信号的 id。它不是工作项,是"当前没有活可干"的显式声明 ——
# 详见 `_seed_fallback` 里兜底分支的说明。提成常量是因为它同时
# 出现在三处(产出、去重判断、测试),散着写迟早会漂。
_SIGNAL_ID = "no-due-maintenance-review"

# 队列锁的默认等待秒数。临界区只有一次 json 读写,几毫秒就该结束,
# 等 5 秒还没拿到基本就是死锁或别人卡住了。
_LOCK_TIMEOUT = 5.0


def _slugify_id(title: str, idx: int = 0) -> str:
    """把标题派生成稳定可读的 id。

    中文标题 slug 化会退化成空串（正则只认 [a-z0-9]），所以：
    - 有 ASCII 部分 → 用它（`给规则加置信度 add-confidence-12`）
    - 纯中文 → 用 `item-` 前缀 + 标题哈希前 6 位

    ## 为什么**不能**把行号算进 id

    早期版本是 `f"{ascii_part}-{idx}"` / `f"item-{idx}-{digest}"`。
    看上去是为了"同一份 backlog 里两条相似标题不会撞 id",但代价是
    id **依赖文件里的行位置**:只要在任意条目上方插入或删除一行,
    下面所有条目的 id 全变。

    而 `backlog.md` 是人手工维护的源文件 —— 加一段说明、调整顺序、
    删掉一条做过的,都会让 `queue.json` 里所有 `done` 记录瞬间失效,
    于是播种把它们当成全新待办重新排队。

    实测踩过:给 backlog.md 顶部加了 6 行说明,8 条待办 id 全变。
    队列"非空"但没有一件真活可干,不变式 #4 成了文字游戏。

    哈希已经足够区分不同标题(同标题才会撞,那本来该合并)。
    `idx` 现在只作**同名时的序号**,不参与正常 id 的生成。

    Args:
        title: 条目标题(决定 id 的唯一输入)
        idx: 同名条目的序号,默认 0
    """
    ascii_part = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:6]
    suffix = f"-{idx}" if idx else ""
    if ascii_part:
        return f"{ascii_part}{suffix}"
    return f"item-{digest}{suffix}"


def is_signal_id(item_id: str) -> bool:
    """这是不是"复查信号"条目,而不是一件工作

    信号的内容是「当前无到期维护项,请人工确认下一步」—— 它**不可完成**,
    存在的唯一目的是让不变式 #4(队列永远非空)有个交代。把它和真活
    一视同仁地标成 done,history 里就会出现"完成了一项任务",而实际
    一行代码都没动。

    这里给唯一判据(r28):`round` / CLI / 测试各写一份前缀匹配,迟早
    有一处漏掉 `-rN` 派生副本 —— 真实队列里就有那种 id。
    """
    return item_id == _SIGNAL_ID or item_id.startswith(_SIGNAL_ID + "-r")


REMOVED_TIER2_WHY = """\
「扫项目现状自动推导」这一层播种已被整层删除(2026-10,第 16 轮)。

它原本有 5 条检查。逐条实测,**没有一条能产出一件真活**:

| 检查 | 依据 | 实测结果 |
|---|---|---|
| (a) risk>=7 的规则缺 confidence | 字段是否存在 | 25 条 risk>=7,**0 条缺**。永远产不出 |
| (b) 规则数 < 40 | 魔法数字 40 | 37 条,永远产出一条"补到 40" |
| (c) 集成源数 < 12 | 魔法数字 12 | 实际 18,阈值早就过了,永不触发 |
| (d) 缺 test_phaseN.py | 固定 1..7 | 一条不缺,永不触发 |
| (e) 对齐 PROJECT_PLAN.md | —— | **它自己的 verify 此刻就通过**(文件在、18KB>200) |

## 病根:把三类东西混在了一个"自动推导"里

1. **常驻不变式** —— (a)「每条规则都要有 confidence」。这不是一件一次性的
   活,是每轮都该成立的事实。放在"缺了才提一条待办"里,后果是它**只在
   第一次被检查**:提过一次之后就不响了,之后谁新加一条漏写 confidence 的
   规则都没人知道。→ 已搬进 `rules_have_advice` 门禁,每轮阻塞级检查。

2. **目标数字** —— (b)(c)。"规则数要到 40""数据源要到 12"是**决定**,
   不是从仓库现状推导出来的事实。而这个决定没有人拥有:阈值从哪来的?
   第 14 轮已经判定「规则数<40 是拍脑袋定的,凑数字是自欺」。一个没人
   负责的数字永远不会被更新,于是它要么永不触发,要么逼着人为了凑数而
   干活。→ 目标属于人写的 `backlog.md`,不属于自动推导。

3. **已经满足的验收条件** —— (e)。verify 写的是
   `test -f docs/PROJECT_PLAN.md` 且大小 >200 —— 这条此刻就成立。
   于是它**永远完不成**(没人能"重新做一遍"一个已经通过的条件),
   却一直挂在队列头等人去干。

> **假活比队列空掉更坏。** 队列空掉会报错;假活会让人真的去干一遍
> 已经做完的事,干完还会被标成 done,污染后续所有轮次的判断依据。

删掉这一层之后不变式 #4 依然成立:剩下 Tier 1(人工 backlog)和
Tier 2 保底层(长期演进项),保底层永远能产出。

**不要把这一层原样加回来。** 要往队列里放东西,先过两道:
  - 这个待办的 `verify` 此刻是否已经通过?已经通过 = 假活
  - 它的判据里有没有"对比某个数字"?有 = 那是目标,写进 backlog.md
"""


# ─── 数据结构 ──────────────────────────────────────────────────────────────
@dataclass
class Item:
    """待办条目。

    字段语义见 devloop/__init__.py 中的不变量说明。
    """

    id: str  # 短横线命名,如 "add-confidence-grading"
    title: str
    detail: str  # 具体做什么(给执行者看的指令)
    priority: int = P1  # P0-P3
    kind: str = "change"  # change | test | doc | research | refactor
    verify: str = ""  # 怎么算做完(门禁命令或可检查的判据)
    tags: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | in_progress | done | dropped
    attempts: int = 0  # mark_in_progress 时 +1,识别反复失败的项目
    created_round: int = 0  # 创建时的轮次(protocol.round_no)
    done_round: int | None = None
    note: str = ""  # 执行备注/失败原因
    # 谁认领了这条(多 agent 并行时用来避免两个 agent 做同一件事)
    owner: str = ""
    # 认领时刻(unix 时间戳,0 = 没被认领过)。
    # 只有 owner 时没法判断"活人还在干"还是"人已经没了" —— owner
    # 只是个字符串。认领时间是第二个独立信号,见 recover_stale 的策略。
    claimed_at: float = 0.0


def _FRESH_WITHIN(rel_path: str, days: int) -> str:
    """周期性任务的 verify:产物存在**且**在 N 天内被更新过

    ## 为什么不能只写 `test -f <报告>`

    这三条保底项都是**周期性**的(季度审计 / 性能基线 / 误报率实测) ——
    做完一次还会再来。而报告文件是持久的:第 14 轮产出了 FP_RATE.md,
    于是 `test -f docs/FP_RATE.md` 从那一刻起**永远成立**。

    后果是这条待办**只能被完成一次**,之后每次被"总是提出"地重新
    播种出来,它的验收条件都是已满足的 —— 永远完不成,却一直占着
    队列。这跟被删掉的 Tier2 (e) 是同一种病,只是慢一点发作。

    带时间边界之后,判据回到"这一轮有没有真的重做":
    90 天内更新过 = 本周期做过了;超过 90 天 = 该重做了。

    纯 stdlib(stat 的 st_mtime + time),不引第三方。
    """
    return (
        "python3 -c \"import pathlib,time,sys;"
        f"p=pathlib.Path('{rel_path}');"
        f"sys.exit(0 if p.exists() and (time.time()-p.stat().st_mtime)<{days * 86400} else 1)\""
    )


# ─── 持久化 ────────────────────────────────────────────────────────────────
def _atomic_write_json(path: Path, payload: dict) -> None:
    """原子写 JSON:先写临时文件,再 os.replace,避免崩溃时半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _coerce_item(raw: dict) -> Item | None:
    """把 dict 转成 Item,缺字段用默认值,字段不合法则丢弃。"""
    if not isinstance(raw, dict) or "id" not in raw or "title" not in raw:
        return None
    try:
        prio = int(raw.get("priority", P1))
    except (TypeError, ValueError):
        prio = P1
    if prio not in (P0, P1, P2, P3):
        prio = P1
    kind = str(raw.get("kind", "change"))
    if kind not in _VALID_KINDS:
        kind = "change"
    status = str(raw.get("status", "pending"))
    if status not in _VALID_STATUS:
        status = "pending"
    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t) for t in tags if t]
    done_round = raw.get("done_round")
    if done_round is not None:
        try:
            done_round = int(done_round)
        except (TypeError, ValueError):
            done_round = None
    try:
        attempts = int(raw.get("attempts", 0))
    except (TypeError, ValueError):
        attempts = 0
    try:
        created_round = int(raw.get("created_round", 0))
    except (TypeError, ValueError):
        created_round = 0
    try:
        claimed_at = float(raw.get("claimed_at", 0) or 0)
    except (TypeError, ValueError):
        claimed_at = 0.0
    return Item(
        id=str(raw["id"]),
        title=str(raw["title"]),
        detail=str(raw.get("detail", "")),
        priority=prio,
        kind=kind,
        verify=str(raw.get("verify", "")),
        tags=tags,
        status=status,
        attempts=attempts,
        created_round=created_round,
        done_round=done_round,
        note=str(raw.get("note", "")),
        owner=str(raw.get("owner", "") or ""),
        claimed_at=claimed_at,
    )


def _item_to_dict(it: Item) -> dict:
    """Item → JSON-safe dict。"""
    d = asdict(it)
    return d


# ─── 主类 ──────────────────────────────────────────────────────────────────
class Queue:
    """待办队列。

    用法:
        q = Queue(Path(".devloop/queue.json"))
        q.load()                       # 读盘
        q.seed_if_empty(round_no=0)    # 空了自动播种
        item = q.next()                # 取下一个
        q.mark_in_progress(item)
        ... 干活 ...
        q.mark_done(item, round_no=5)
        q.save(q.load())               # 落盘
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ── 加载 / 保存 ─────────────────────────────────────────────────────
    def load(self) -> list[Item]:
        """读盘,返回 Item 列表。文件不存在/格式错则返回空列表。"""
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("queue.load failed (%s): %s; returning []", self.path, e)
            return []
        if not isinstance(raw, dict):
            return []
        items_raw = raw.get("items", [])
        if not isinstance(items_raw, list):
            return []
        out: list[Item] = []
        for r in items_raw:
            it = _coerce_item(r)
            if it is not None:
                out.append(it)
        return out

    def save(self, items: list[Item]) -> None:
        """落盘,原子写。"""
        payload = {
            "version": self.SCHEMA_VERSION,
            "items": [_item_to_dict(i) for i in items],
        }
        _atomic_write_json(self.path, payload)

    # ── 多 agent:加锁的原子操作 ────────────────────────────────────────
    #
    # 上面 load()/save() 各自原子,但"load → 改 → save"这一整段不是。
    # 两个 agent 各领一条活、各自保存时,后保存的会覆盖先保存的:
    # 实测 A 领到 task-0、B 领到 task-1,A.save() 后 B.save(),
    # task-0 退回 pending —— A 正在干的活被第三个 agent 重复领走。
    #
    # 下面这几个方法是"加锁 + 读 + 改 + 写"打包,多 agent 并行的
    # **唯一正确入口**。绕开它们直接 load/save 的代码在并发下不安全。

    def _locked(self, owner: str, timeout: float = _LOCK_TIMEOUT):
        """取队列锁的上下文管理器(独立出来方便测试替换)"""
        from .lock import file_lock
        return file_lock(self.path, timeout=timeout, owner=owner)

    def claim(self, owner: str = "", timeout: float = _LOCK_TIMEOUT,
              item_id: str | None = None) -> Item | None:
        """原子地认领一条待办。返回认领到的条目,没有可领的返回 None。

        这是多 agent 并行的基本动作:在**一把锁里**完成
        load → 挑一条 → 标 in_progress + 记 owner → save,
        所以两个 agent 同时调用,拿到的一定是**不同的条目**。

        Args:
            owner: 认领者标识(pid / agent 名),写进 Item.owner
            timeout: 等锁秒数
            item_id: 指定要领哪一条;None = 按优先级自动挑

        Returns:
            认领到的 Item(同时也在队列里,状态 in_progress),或 None
        """
        with self._locked(owner, timeout):
            items = self.load()
            if item_id is not None:
                cand = next(
                    (i for i in items
                     if i.id == item_id and i.status in ("pending", "in_progress")),
                    None,
                )
            else:
                cand = self.next(items)
            if cand is None:
                return None
            cand.status = "in_progress"
            cand.owner = self._owner_with_pid(owner)
            cand.claimed_at = time.time()
            cand.attempts = int(cand.attempts or 0) + 1
            self.save(items)
            return cand

    def release(self, item_id: str, note: str = "",
               timeout: float = _LOCK_TIMEOUT) -> bool:
        """放弃认领:把 in_progress 退回 pending,清掉 owner。

        agent 干一半挂掉时必须走这里,否则那条会被永远锁在
        in_progress,别人不敢碰。`recover_stale_in_progress` 是
        跨进程启动时的兜底,这个是 agent 自己的主动释放。
        """
        with self._locked("release", timeout):
            items = self.load()
            target = next((i for i in items if i.id == item_id), None)
            if target is None or target.status != "in_progress":
                return False
            target.status = "pending"
            target.owner = ""
            target.claimed_at = 0.0
            if note:
                target.note = (target.note + " | " if target.note else "") + note
            self.save(items)
            return True

    def finish(self, item_id: str, ok: bool, owner: str = "",
               round_no: int | None = None,
               timeout: float = _LOCK_TIMEOUT) -> bool:
        """收尾:ok=True 标 done,False 退回 pending。带 owner 校验。

        带 owner 校验是关键:两个 agent 领了同一条(不该发生,但万一),
        后收的那个会发现 owner 不对,拒绝覆盖别人的结果。
        """
        with self._locked("finish", timeout):
            items = self.load()
            target = next((i for i in items if i.id == item_id), None)
            if target is None:
                return False
            # r33:claim 现在保证 owner 带 pid,而 claim/finish 两侧传的
            # owner 可能只有名字("agent-x")。不规范化的话,claim 写的是
            # "agent-x#123"、finish 校验的是 "agent-x",精确比较必然不等,
            # 于是**每一次 finish 都会被自己的 owner 校验拒掉**。
            norm_owner = self._owner_with_pid(owner) if owner else ""
            if norm_owner and target.owner and target.owner != norm_owner:
                log.warning(
                    "queue.finish: %s 的 owner 是 %r,与提交方 %r 不符,拒绝覆盖",
                    item_id, target.owner, owner,
                )
                return False
            if ok:
                target.status = "done"
                target.done_round = round_no
            else:
                target.status = "pending"
            target.owner = ""
            target.claimed_at = 0.0
            self.save(items)
            return True

    def claimed_by(self) -> dict[str, str]:
        """当前被认领的条目 → {id: owner}(status 用来判断状态里,给 CLI 显示用)"""
        return {
            i.id: i.owner
            for i in self.load()
            if i.status == "in_progress"
        }

    # ── 认领是否还活着 ─────────────────────────────────────────────────
    #
    # `recover_stale_in_progress` 每轮开头复位上轮残留的 in_progress。
    # 在只有单人同步轮次的世界里这条规则是对的(跑完的轮次不该有残留,
    # 残留就是软死局)。但认领机制一落地,这个"无条件复位"就变成了
    # **主动破坏别人的活**:
    #
    #     agent A: claim task-x   → in_progress, owner=A, A 开始改代码
    #     agent B: devloop round  → 复位 → task-x 回 pending
    #     agent B: next()          → 捡起 task-x,也去改同一处代码
    #
    # 文件锁防不了这个:锁保证的是"写不撕裂",而这里是**语义上合法地
    # 覆盖了别人的进度**。两个 agent 同时改同一处代码,正是这把锁
    # 想防的后果,却从后门进来了。
    #
    # 所以复位必须**认领感知**。判断信号有两个,独立且互补:
    #   1. owner 里的 pid 还在不在(硬信号,准)
    #   2. 认领了多久(软信号,兜底)
    # 详见 `is_stale_claim`。

    # pid 出现在 owner 串末尾:CLI 默认名是 `agent#1234`,内部兜底是 `pid-1234`。
    #
    # r33:`-pid-` 这一支是给 `round-pid-1234` 用的。原来只有 `#` 和
    # `^pid-`,而 `^` 锚在**字符串开头**,所以 r31 给 round 认领加的
    # `round-pid-<pid>` 解析不出 pid —— owner 确实记上了,`owner_alive`
    # 却照样返回 None,recover_stale 仍只能按认领时长猜。
    # r31 的测试只断言 owner 非空且以 `round-pid-` 开头,没验这条,
    # 于是"记上了"被当成了"探测得出来"(存在检查冒充行为检查)。
    _OWNER_PID = re.compile(r"(?:#|^pid-|-pid-)(\d+)$")

    @classmethod
    def _owner_with_pid(cls, owner: str) -> str:
        """保证 owner 里带 pid —— recover_stale 的整套机制全靠它

        传了不含 pid 的标识(比如 `--owner r33-agent`)时补成
        `r33-agent#<pid>`。不补的后果不是报错,而是**静默退化**:
        owner_alive 返回 None,一条死掉的认领会一直占着 in_progress
        直到 STALE_CLAIM_SEC 超时才被复位,而不是立刻。
        """
        owner = (owner or "").strip()
        if not owner:
            return f"pid-{os.getpid()}"
        if cls.owner_pid(owner) is not None:
            return owner
        return f"{owner}#{os.getpid()}"

    @classmethod
    def owner_pid(cls, owner: str) -> int | None:
        """从 owner 标识里解析 pid;解析不出来返回 None"""
        m = cls._OWNER_PID.search((owner or "").strip())
        return int(m.group(1)) if m else None

    @staticmethod
    def owner_alive(owner: str) -> bool | None:
        """owner 那个进程还活着吗?None = 判断不了。

        ## Windows 上不能用 `os.kill(pid, 0)`

        这是个会出人命的坑。CPython 在 Windows 上把 `os.kill(pid, sig)`
        实现成 `TerminateProcess(handle, sig)` —— 也就是说
        **`os.kill(pid, 0)` 不是"探测",是"把这个进程杀掉,退出码 0"**。
    测活性的时候拿别人的 pid 去探测,等于在探测的一瞬间把它杀了。

        所以 POSIX 走 `os.kill(pid, 0)`(signal 0 只做权限/存在性检查),
        Windows 走 ctypes `OpenProcess`,拿到句柄立刻关掉,**不碰任何
        终止函数**。
        """
        pid = Queue.owner_pid(owner)
        if pid is None:
            return None
        if pid <= 0:
            return False
        if os.name == "nt":  # pragma: no cover — 仅 Windows
            try:
                import ctypes
                # PROCESS_QUERY_LIMITED_INFORMATION:只要"存在吗"的信息,
                # 权限要求最低,别人的进程也能开
                SYNCHRONIZE = 0x00100000
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                if not handle:
                    # 87 = ERROR_INVALID_PARAMETER → 这个 pid 不存在
                    return kernel32.GetLastError() != 87
                kernel32.CloseHandle(handle)
                return True
            except Exception:
                return None  # 探测不了就说探测不了,不猜
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 存在,只是不是我们的进程
        except OSError:
            return None
        return True

    # 认领超过这么久,且无法证明 owner 还活着 → 判定为过期。
    # 默认 6 小时:足够覆盖一个 agent 从领活到交活的时长,又不至于
    # 让"人跑了忘了 release"的条目永久锁死。
    STALE_CLAIM_SEC = 6 * 3600.0

    @classmethod
    def is_stale_claim(
        cls,
        item: Item,
        now: float | None = None,
        stale_after_sec: float | None = None,
    ) -> tuple[bool, str]:
        """这条 in_progress 是不是"上一轮残留"?返回 (是否复位, 依据)。

        判定规则,按可靠性排序 —— **不确定时倾向不复位**:
        1. owner 为空 → 复位。这是 r11 软死局的老形态:没有任何人
           认领它却卡在 in_progress,留着它队列就永远选不出待办。
        2. owner 的进程死了 → 复位。人没了,活不可能还在推进。
        3. owner 活着 → **不复位**。有人在正经干活,抢它就是重复劳动。
        4. 判断不了(没有 pid / 探测失败)且认领已超过
           `stale_after_sec` → 复位。不复位的话,一个崩在 CI 里的
           agent 能把队列永久锁死,那比重复劳动更难恢复。
        5. 判断不了但认领还很新 → 不复位。刚领的东西当它是活的。

        ## pid 复用的已知局限

        规则 2 认的是"pid 还在不在",不是"还是不是同一个进程"。
        原来那个 agent 崩了之后,它的 pid 被系统分给别的进程,规则 2
        就会误判成"活着",于是该条一直不复位,直到规则 4 的超时兜底。
        代价是**多锁一段时间**,不是丢工作或重复劳动,所以这个方向的
        误差是安全的。真要根治得记录进程启动时间,超出当前范围。
        """
        if item.status != "in_progress":
            return False, "not in_progress"
        if not item.owner:
            return True, "no owner (r11 软死局形态)"

        alive = cls.owner_alive(item.owner)
        if alive is True:
            return False, f"owner {item.owner} 仍在运行"
        if alive is False:
            return True, f"owner {item.owner} 的进程已退出"

        # 到这里 = 判断不了
        now = time.time() if now is None else now
        limit = cls.STALE_CLAIM_SEC if stale_after_sec is None else stale_after_sec
        # claimed_at 为 0 = 这条是老格式文件里的 in_progress,没有认领时间。
        # 那只能当成"很老"处理,否则 r11 的软死局又会回来。
        age = now - item.claimed_at if item.claimed_at else float("inf")
        if age >= limit:
            return True, f"认领者无法探测且已超过 {limit:.0f}s 未交活"
        return False, f"认领者无法探测,但认领才 {age:.0f}s,当它是活的"

    def recover_stale(self, timeout: float = _LOCK_TIMEOUT,
                      stale_after_sec: float | None = None
                      ) -> tuple[int, list[tuple[str, str]]]:
        """把**真正没人管**的 in_progress 退回 pending。

        返回 (复位条数, [(id, 保留原因)])。

        ## 为什么这个方法住在 Queue 而不是 protocol

        它需要三样东西:文件锁、判据(`is_stale_claim`)、读写。
        早先把它写进 `Loop.recover_stale_in_progress`,于是 protocol 里
        出现了一套独立的 lock+load+判+save —— 而 Queue 里同时又有一套
        加锁的原子操作。**同一条"读-改-写必须加锁"的规则存在两个实现**,
        而两个实现里只要有一个漏了锁,它不会报错,只会安静地丢更新。

        早先那一版真的漏了:load 在锁外、save 在锁内。丢的不是"我们要
        复位的这几条",而是"我们压根没打算碰的那些" —— 别的 agent 期间
        刚抢到的活,被我们的过期快照一并抹掉了。

        规则只该有一处实现。
        """
        stale, kept = [], []
        with self._locked("recover_stale", timeout):
            # load 在锁**内**:整段读-改-写必须是一次临界区。
            # 锁外的 load 看着无害,恰恰因为丢的是"没打算碰的那些"。
            items = self.load()
            for i in items:
                reset, why = self.is_stale_claim(i, stale_after_sec=stale_after_sec)
                if reset:
                    stale.append((i, why))
                elif i.status == "in_progress":
                    kept.append((i.id, why))
            for it, why in stale:
                it.status = "pending"
                it.owner = ""
                it.claimed_at = 0.0
                it.note = (it.note + " | " if it.note else "") + f"recovered: {why}"
            if stale:
                self.save(items)
        return len(stale), kept

    # ── 调度 ─────────────────────────────────────────────────────────────
    @staticmethod
    def _sort_key(it: Item):
        # 稳定排序:priority 升序 → created_round 升序 → id 升序
        return (it.priority, it.created_round, it.id)

    def next(self, items: list[Item] | None = None) -> Item | None:
        """取下一个 pending,跳过 done/dropped/in_progress。

        `items` 可以传入调用方已经 load() 出来的列表——**这很重要**:
        内部重新 load() 会返回全新的对象,调用方在返回的 Item 上做的
        原地修改(mark_*)保存时会丢,导致状态永远不落库。

        不传 items 时自行 load(),方便独立使用。
        """
        if items is None:
            items = self.load()
        candidates = [i for i in items if i.status == "pending"]
        if not candidates:
            return None
        candidates.sort(key=self._sort_key)
        return candidates[0]

    # ── 状态机 ───────────────────────────────────────────────────────────
    def mark_in_progress(self, item: Item) -> None:
        """把 item 设为 in_progress,attempts +1。原地修改,需 save()。"""
        item.status = "in_progress"
        item.attempts += 1

    def mark_done(self, item: Item, round_no: int) -> None:
        """把 item 设为 done,记录完成轮次。原地修改,需 save()。"""
        item.status = "done"
        item.done_round = int(round_no)
        item.note = item.note or ""

    def mark_dropped(self, item: Item, reason: str) -> None:
        """把 item 设为 dropped,记录原因。原地修改,需 save()。"""
        item.status = "dropped"
        item.done_round = item.done_round  # 不强制覆盖,保留原值
        item.note = (item.note + "\n" if item.note else "") + f"dropped: {reason}"

    # ── 添加 ─────────────────────────────────────────────────────────────
    def add(self, item: Item, timeout: float = _LOCK_TIMEOUT) -> None:
        with self._locked("add", timeout):
            """追加一条 pending item(若 id 已存在则替换原条目状态为 pending)。"""
            items = self.load()
            for i, ex in enumerate(items):
                if ex.id == item.id:
                    # 重置为 pending,但保留 done_round/note 用于审计
                    item.attempts = ex.attempts
                    item.created_round = ex.created_round or item.created_round
                    item.done_round = ex.done_round
                    item.note = ex.note
                    items[i] = item
                    self.save(items)
                    return
            items.append(item)
            self.save(items)

    # ── 统计 ─────────────────────────────────────────────────────────────
    def stats(self) -> dict:
        """返回队列状态快照。"""
        items = self.load()
        by_priority: dict[int, int] = {P0: 0, P1: 0, P2: 0, P3: 0}
        pending = done = dropped = in_progress = 0
        for it in items:
            if it.priority in by_priority:
                by_priority[it.priority] += 1
            if it.status == "pending":
                pending += 1
            elif it.status == "in_progress":
                in_progress += 1
            elif it.status == "done":
                done += 1
            elif it.status == "dropped":
                dropped += 1
        return {
            "pending": pending,
            "in_progress": in_progress,
            "done": done,
            "dropped": dropped,
            "by_priority": by_priority,
            "total": len(items),
        }

    # ── 状态查询 ─────────────────────────────────────────────────────────
    def exhausted(self) -> bool:
        """是否耗尽(没有 pending 也没有 in_progress)。"""
        items = self.load()
        return not any(i.status in ("pending", "in_progress") for i in items)

    # ── 播种(核心:队列永远非空) ──────────────────────────────────────────
    def seed_if_empty(self, timeout: float = _LOCK_TIMEOUT) -> int:
        with self._locked("seed", timeout):
            """空了就播种。返回新增条数。

            两级策略(优先级递减):
                1. 读 devloop/backlog.md(人工长期待办清单,跳过已完成的)
                2. 保底 3 条长期演进项(只提已经到期的)

            旋转规则:补进来的 id 若与**任何**已有条目重名,一律加 -r{round}
            后缀,保证 id 全局唯一。

            ## 这里曾经有个能把队列彻底废掉的 bug

            原实现传给旋转逻辑的是 `active_ids`(只含 pending/in_progress)。
            而 docstring 写的是"若已存在且处于 done/dropped 则加后缀"——
            文档说一回事,代码做另一回事。后果是:

            队列里所有条目都 done 时 `active_ids` 是空集 → 旋转不触发
            → 补进来一批**和已有条目完全同 id** 的新条目。

            而 `mark_in_progress` / `mark_done` / `round()` 全都靠
            `next(i for i in items if i.id == ...)` 按 id 定位,永远命中
            **第一条**。于是引擎反复翻转那条早已 done 的记录,新补的条目
            永远卡在 pending —— 循环原地空转,永远走不到下一步。

            实测:真实队列被搞成 8 个 id 各出现两次,8 条全"活跃"。
            所以这里传的是**全部**已有 id,不是活跃的。
            """
            items = self.load()
            # 注意:必须是全部 id,不是只有活跃的。见上方 docstring。
            existing_ids = {i.id for i in items}
            # 已经**做完**的 id。人工 backlog 里的条目做完就不该再被捡回来。
            done_ids = {i.id for i in items if i.status in ("done", "dropped")}
            max_round = max((i.created_round for i in items), default=0)
            next_round = max_round + 1  # 每次 seed 视为新轮次

            added: list[Item] = []
            # _disambiguate 要拿"加之前"的 id 集合。上面那几行 `existing_ids |=`
            # 是给下一层做级联去重的,不能拿来当这个。
            taken_before = set(existing_ids)

            # ── Tier 1:backlog.md ──
            # 语义是"**跳过已完成的**",不是"改名复活"。
            # backlog.md 是人写的清单,引擎做完一条就把它在 queue.json 里标 done,
            # 但文件本身不会自动删行。于是播种再读它时会读到同一批条目。
            # 如果这里"改名复活",就等于把已完成的活无限量地重新排队 ——
            # 实测第 12 轮后队列里 8 条待办全是已完成工作的重推导
            # (置信度/架构测试/UNION/证书/storage/DISAPPEARED ...),
            # 队列"非空"但没有一件真活可干。不变式 #4 被满足成了文字游戏。
            #
            # 人想重做同一件事时,显式从 backlog.md 删掉或改 id 即可 ——
            # "重做"应该是一个动作,不该是副作用。
            added += self._seed_from_backlog(done_ids, next_round, skip_existing=True)
            existing_ids |= {i.id for i in added}


        # ── Tier 2:保底 ──
            if not added:
                # 长期演进项本来就是"做到就算一轮、还会再来"的,所以永远提出
                # + 改名。保底层必须总能提出东西,否则它不是保底。
                #
                # 原来这里上面还有一层"扫项目现状自动推导"(Tier 2)。它已经
                # 被整层删掉了,理由见 `REMOVED_TIER2_WHY`。删除之后不变式 #4
                # 依然成立:本层是保底,永远能产出。
                added += self._seed_fallback(set(), next_round)

            if added:
                # 兜底:任何重复 id 都在这里被就地改名。
                # 两级策略里哪一层写错了都不至于产出重复 id。
                #
                # 注意传的是**加之前**的快照。早先直接传 existing_ids,而上面
                # 那些 `existing_ids |= {added}` 早就把新条目自己的 id 塞进去了,
                # 于是 _disambiguate 拿新条目跟它自己比,全部改名 ——
                # 连空队列播种都产出 `item-2-97ad0c-r1` 这种"本该是首次出现"
                # 的 id。比 id 冲突更糟的是它让首次播种看起来像重跑。
                added = self._disambiguate(added, taken_before)
                items.extend(added)
                self.save(items)
                log.info("queue.seed_if_empty: +%d items", len(added))
            return len(added)

    @staticmethod
    def _disambiguate(items: list[Item], taken: set[str]) -> list[Item]:
        """给新增条目去重:撞上已占用的 id 就加递增后缀"""
        out: list[Item] = []
        for it in items:
            iid = it.id
            n = 1
            while iid in taken:
                iid = f"{it.id}-r{n}"
                n += 1
            if iid != it.id:
                log.warning(
                    "queue._disambiguate: id %r 已存在,新条目改名为 %r", it.id, iid
                )
                it.id = iid
            taken.add(iid)
            out.append(it)
        return out

    def repair_duplicates(self, timeout: float = _LOCK_TIMEOUT) -> int:
        with self._locked("repair", timeout):
            """修复重复 id。返回被删除的条目数。

            ## 为什么需要它

            重复 id 会让引擎的所有按 id 定位(`mark_in_progress` / `mark_done` /
            `round()` 取待办)全部命中第一条记录,导致循环原地空转。
            历史上 `seed_if_empty` 真的造出过这种队列(见该方法 docstring),
            修好播种逻辑只能保证**将来**不再产生,已经写坏的文件仍需要修。

            ## 保留哪一条

            同一个 id 保留**进度最靠前**的那条:
                in_progress > pending > done > dropped
            进度靠前的说明引擎已经认可它在干活;同状态下保留 attempts 最大的
            (重试次数多 = 引擎已经为它付出过更多),再并列时保留 id 字典序最小的
            以保证结果确定(同样的输入永远得到同样的队列)。

            调用方拿到的是被删条目的 id 列表,便于人工确认。
            """
            items = self.load()
            if not items:
                return 0

            rank = {"in_progress": 0, "pending": 1, "done": 2, "dropped": 3}
            by_id: dict[str, list[Item]] = {}
            for it in items:
                by_id.setdefault(it.id, []).append(it)

            keepers: list[Item] = []
            removed = 0
            for iid, group in by_id.items():
                if len(group) == 1:
                    keepers.append(group[0])
                    continue
                best = min(
                    group,
                    key=lambda g: (
                        rank.get(g.status, 9),
                        -int(g.attempts or 0),
                        g.id,
                    ),
                )
                keepers.append(best)
                removed += len(group) - 1
                log.warning(
                    "queue.repair_duplicates: id %r 有 %d 条重复,保留 %s(%s),删除 %d 条",
                    iid, len(group), best.id, best.status, len(group) - 1,
                )

            # 保持原有顺序感:按原文件里首次出现的位置排
            first_pos = {}
            for idx, it in enumerate(items):
                first_pos.setdefault(it.id, idx)
            keepers.sort(key=lambda g: first_pos.get(g.id, 0))

            if removed:
                self.save(keepers)
            return removed

    def find_duplicates(self) -> dict[str, int]:
        """找出重复 id 及其出现次数(只读,不改文件)"""
        counts: dict[str, int] = {}
        for it in self.load():
            counts[it.id] = counts.get(it.id, 0) + 1
        return {k: v for k, v in counts.items() if v > 1}

    def drop(self, item_id: str, reason: str, timeout: float = _LOCK_TIMEOUT) -> tuple[bool, str]:
        with self._locked("drop", timeout):
            """把一条待办标成 dropped。返回 (成功, 说明)。

            ## 为什么需要这个操作

            `unmark` 是反方向的:把误标的 done 改回 pending。
            但还有第三种情况它处理不了 —— **这条待办本身就是假的**。

            实测抓到过三种假活:
            - 判据是没人拥有的魔法数字(「规则数补到 40」)
            - 它的 verify 此刻就已经通过(「对齐 PROJECT_PLAN.md」,而文件在)
            - 判据是常驻不变式,不是「这次工作做没做」(「季度依赖审计」
              验的是 `dependencies == []`,而它本来就成立)

            对这类条目,改回 pending 是错的 —— 它会再次变成队列头,
            再次被选中,再次做不出任何东西。必须让它**消失**,而且要
            **带上原因**,否则三个月后没人记得当初为什么删。

            为什么不能直接改 queue.json:那会让"为什么丢弃"这一条信息
            绕过 review。而丢弃原因恰恰是这件事里最值钱的部分 ——
            它记录的是"我们试过这条路,它不成立"。

            dropped 的 id 也在 `done_ids` 里,所以人工 backlog 不会把它捡回来。
            """
            items = self.load()
            matches = [it for it in items if it.id == item_id]
            if not matches:
                return False, f"no such item: {item_id!r}"
            if len(matches) > 1:
                return False, (
                    f"{item_id!r} has {len(matches)} records with the same id; "
                    f"run 'arl-lite devloop repair' first"
                )
            target = matches[0]
            if target.status == "dropped":
                return False, f"{item_id} is already dropped"
            if not reason.strip():
                return False, (
                    "丢弃必须给理由。丢弃原因记录的是'我们试过,它不成立',"
                    "那是这条记录里最值钱的部分 —— 没有它,三个月后"
                    "下一个人会把同样的东西再加回来。用 --reason 写清楚。"
                )
            old = target.status
            target.status = "dropped"
            target.owner = ""
            target.claimed_at = 0.0
            target.note = (target.note + "\n" if target.note else "") + \
                f"dropped from {old}: {reason}"
            self.save(items)
            return True, f"{item_id}: {old} -> dropped"

    def unmark(self, item_id: str, reason: str = "", timeout: float = _LOCK_TIMEOUT) -> tuple[bool, str]:
        with self._locked("unmark", timeout):
            """把误标的 done/dropped 改回 pending。返回 (成功, 说明)。

            ## 为什么需要这个操作

            引擎在没有 build_fn 时无法知道执行者到底做了什么,
            门禁一绿就把队首标成 done。于是"这一轮其实没做那件事"会被
            记成已完成 —— 队列和现实对不上,后面每轮都建在假记录上。

            有了它,纠正记录是一等操作,不用手改 queue.json。

            只认 id **唯一命中**的那条。有重复 id 时直接拒绝并提示先 repair,
            因为那时"改哪一条"是歧义的,猜错等于把记录改得更乱。
            """
            items = self.load()
            matches = [it for it in items if it.id == item_id]
            if not matches:
                return False, f"no such item: {item_id!r}"
            if len(matches) > 1:
                return False, (
                    f"{item_id!r} has {len(matches)} records with the same id; "
                    f"run 'arl-lite devloop repair' first"
                )
            target = matches[0]
            if target.status == "pending":
                return False, f"{item_id} is already pending"
            old = target.status
            target.status = "pending"
            target.done_round = None
            target.note = f"unmarked from {old}" + (f": {reason}" if reason else "")
            self.save(items)
            return True, f"{item_id}: {old} -> pending"

    # ── Tier 1 ────────────────────────────────────────────────────────────
    _BACKLOG_LINE = re.compile(
        r"^\s*-\s*\[P([0-3])\]\s*(change|test|doc|research|refactor)\s*:\s*"
        r"([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*(.*?)\s*$"
    )

    def _backlog_path(self) -> Path:
        """backlog.md 与 queue.json 同目录(都在 devloop/)。"""
        return self.path.parent / "backlog.md"

    def _seed_from_backlog(
        self, existing_ids: set[str], round_no: int, skip_existing: bool = False
    ) -> list[Item]:
        """读 devloop/backlog.md;不存在则创建空模板。

        Args:
            existing_ids: 已占用的 id 集合
            round_no: 新条目的 created_round
            skip_existing: True=撞上 existing 就**丢弃这条**(不复活);
                False=撞上就加 -r{round} 后缀(复活)。
                Tier 1 用 True —— 人写下的待办做完了就不该自己回来。
        """
        bp = self._backlog_path()
        if not bp.exists():
            bp.parent.mkdir(parents=True, exist_ok=True)
            bp.write_text(
                "# devloop 待办清单(人工追加)\n"
                "# 格式: - [P0..P3] kind: title | detail | verify\n"
                "# 示例: - [P2] change: 加缓存 | 在 core/cache.py 加 LRU | "
                "python -m pytest tests/test_cache.py\n"
                "\n",
                encoding="utf-8",
            )
            return []
        out: list[Item] = []
        try:
            text = bp.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("backlog read failed: %s", e)
            return out
        # 同名条目的出现次数。**不是行号** —— 行号会随文件里任何一处增删
        # 而变化,那样每次编辑 backlog.md 都会让下方所有条目的 id 全变,
        # queue.json 里的 done 记录瞬间失效,已完成的活被重新排队。
        # (实测踩过:文件顶部加 6 行说明,8 条待办 id 全变。)
        title_seen: dict[str, int] = {}
        for line in text.splitlines():
            m = self._BACKLOG_LINE.match(line)
            if not m:
                continue
            prio = int(m.group(1))
            kind = m.group(2)
            title = m.group(3).strip()
            detail = m.group(4).strip()
            verify = m.group(5).strip()
            # id 由 title 派生。中文标题 slug 化会退化成 "item"（正则只认
            # [a-z0-9]），所以中文为主时退化为 "item-" + 标题短哈希——
            # 稳定、可读，且不依赖它在文件里的位置。
            nth = title_seen.get(title, 0)
            title_seen[title] = nth + 1
            base_id = _slugify_id(title, nth)
            iid = base_id
            if iid in existing_ids:
                if skip_existing:
                    # 这条已经做过(或已丢弃),不复活。
                    # 人想重做就显式改 id 或从 backlog.md 删掉 ——
                    # "重做"应该是一个动作,不该是播种的副作用。
                    continue
                iid = f"{base_id}-r{round_no}"
            out.append(
                Item(
                    id=iid,
                    title=title,
                    detail=detail,
                    priority=prio,
                    kind=kind,
                    verify=verify,
                    tags=["backlog"],
                    created_round=round_no,
                )
            )
        return out

    # ── Tier 2:保底 ──────────────────────────────────────────────────────
    _FALLBACK: tuple[dict, ...] = (
        {
            "id": "quarterly-dep-audit",
            "title": "季度依赖审计",
            "detail": (
                "巡检 pyproject 与安全公告,记录 optional-dependencies 与"
                "运行时风险的边界;目标保持核心零依赖(见 pyproject.toml)。"
            ),
            "priority": P3,
            "kind": "research",
            # verify 必须是**本次工作留下的证据**,不能是常驻不变式。
            # 原先写的是 `assert t['project']['dependencies']==[]` ——
            # 那是"核心保持零依赖"这条不变式,它**此刻就成立**,于是这条
            # 审计永远不可能被判定为"做过":没人能重新做一遍已经为真的
            # 条件。它和被删掉的 Tier2 (e) 是同一种病。
            # 改成查审计报告本身存在,这才区分得了"做了"和"本来就成立"。
            "verify": _FRESH_WITHIN("docs/DEP_AUDIT.md", 90),
            "tags": ["deps", "audit"],
        },
        {
            "id": "performance-baseline-record",
            "title": "性能基线记录",
            "detail": (
                "对核心流水线(subdomain enum → probe → cross-ref →"
                "rule)做一次基线跑,记录吞吐/内存,落到 docs/"
                "PERF_BASELINE.md。"
            ),
            "priority": P3,
            "kind": "research",
            "verify": _FRESH_WITHIN("docs/PERF_BASELINE.md", 90),
            "tags": ["perf", "baseline"],
        },
        {
            "id": "false-positive-rate-measurement",
            "title": "误报率实测",
            "detail": (
                "在受控样本上跑规则集,统计 false positive 率,"
                "输出到 docs/FP_RATE.md,作为规则调优的客观输入。"
            ),
            "priority": P3,
            "kind": "research",
            "verify": _FRESH_WITHIN("docs/FP_RATE.md", 90),
            "tags": ["rules", "quality"],
        },
    )

    @staticmethod
    def verify_passes(verify: str, timeout: float = 30.0) -> bool:
        """跑一条 verify,返回是否通过。
        跑不起来(超时/命令不存在/异常)一律按**不通过**处理 ——
        宁可当成"没做",也不要因为判断不了就下结论。
        `verify` 全部来自本模块的源码常量或本仓库的 backlog.md,
        是只读检查;仍然加超时,一条写坏的 verify 不能挂死播种。
        """
        if not verify.strip():
            return False
        try:
            r = subprocess.run(
                ["bash", "-c", verify],
                cwd=Path.cwd(), capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return r.returncode == 0

    def _seed_fallback(
        self, existing_ids: set[str], round_no: int
    ) -> list[Item]:
        """保底:3 条长期演进项,**只提已经到期的**。

        ## 为什么加"到期"判断

        早先这里是"总是提出 + 改名",理由是保底层必须总能产出,
        否则不变式 #4(队列永远非空)会破。

        但这三条是**周期性**任务,而 verify 已经带 90 天时间边界了 ——
        它回答的正是"这条到点了吗"。到点没做的才该被提;刚做过的
        每轮被重新提一次,就是噪音,而且是第 16 轮刚清掉的那类假活
        换个马甲又回来了(实测:FP_RATE.md 刚在第 14 轮产出,
        `false-positive-rate-measurement` 就成了永远完不成的条目)。

        所以改成:**verify 当前不通过的才提**。这让 verify 同时承担
        两个角色,它们本来就是同一件事 ——
          - 播种时:这条到点了吗?
          - 验收时:这件事做完了吗?

        ## 到期判断失败时保守处理

        verify 跑不起来(命令写错/超时/非零退出码之外的异常)时,
        按**已到期**处理 —— 宁可多提一条让人看一眼,也不要因为
        判断不了而静默地什么都不提。静默是这类系统最坏的失败模式。

        真要是三条全都还没到期,本层返回空,队列会空掉。那是对
        "眼下确实没有维护活要干"的诚实回答,不是不变式被破坏 ——
        该做的是往 `backlog.md` 里加新的人写待办,或者往 `_FALLBACK`
        里加新的长期项(那是个决定,应该由人做)。
        """
        out: list[Item] = []
        for spec in self._FALLBACK:
            if spec["id"] in existing_ids:
                continue
            if self.verify_passes(spec["verify"]):
                log.info(
                    "queue._seed_fallback: %s 尚未到期(verify 通过),不提出",
                    spec["id"],
                )
                continue
            out.append(
                Item(
                    id=spec["id"],
                    title=spec["title"],
                    detail=spec["detail"],
                    priority=spec["priority"],
                    kind=spec["kind"],
                    verify=spec["verify"],
                    tags=list(spec["tags"]),
                    created_round=round_no,
                )
            )
        # ── 兜底:到期判断全过时的复查信号 ──
        #
        # 实测踩到过这个空档(r18):三条周期项全部未到期,保底层
        # 返回空,队列空掉,不变式 #4(#2 轮起就有测试守着)真的破了。
        #
        # 这里要小心:最省事的做法是"反正要提点什么,随便提一条" ——
        # 那正是第 16 轮删掉的整层假活制造机。所以兜底项**不是工作**,
        # 它是一条**显式声明「当前无到期维护项」的复查信号**。
        #
        # ## r22 的三轮反复:这个条件被改错了两次才改对
        #
        # 初版(r19)是 `not out and not existing_ids`,但 `existing_ids`
        # 只在调用方显式传入时非空,而 `seed_if_empty` 传的是 `set()`。
        #
        # r22 第一次改成"只要没有 **pending** 信号就提"。看着对了,
        # 但**我自己的测试是恒真的** —— 它 close 信号时写成
        # `q.save(q.load())`,存回去的是重新读出来的、没被改动的那份,
        # 信号自始至终是 pending。于是"每轮只有一条"的原因是
        # "它一直没被关掉",不是"修复起了作用"。
        # 三个变异(判据恒假/删掉 id 判断/前缀退化)全都全绿通过。
        #
        # 把 close 真正做对之后,真相才露出来:
        #
        #     信号被 close → 队列空 → 播种 → 又提一条(-r1)
        #
        # **答完还追问。** 这才是真实队列里
        # `no-due-maintenance-review`(done) + `-r1`(dropped)
        # + `-r2`(dropped) 三条并存的成因 —— 不是引擎抽风,
        # 是在配合不变式 #4 反复重挂提醒。
        #
        # ## 那到底该不该重提
        #
        # 该 —— 但**必须留给人一个明确的关闭开关**。
        #
        # - `done` 后重提:正确。不变式 #4 要求队列永远非空,
        #   而"眼下没有活"这个事实得有人持续面对。信号就是那个逼迫。
        # - `dropped` 后重提:**错误**。drop 必须带理由(`r17` 加的),
        #   语义是"我明确不要这条"。真实队列里那两条 dropped 就是人
        #   在试图让它闭嘴,而它无视了 —— 人的显式指令被静默覆盖,
        #   比队列空掉更坏。
        #
        # 所以判据是:队里没有 pending 信号、**且**没有 dropped 信号。
        #
        # ## 一并做掉的历史数据
        #
        # 真实 `devloop/queue.json` 里原本躺着 `no-due-maintenance-review-r1`
        # 和 `-r2`(状态 dropped)。在新语义下它们会**永久关掉**复查信号,
        # 所以做了迁移:两条都已从队列移除。
        #
        # 移除的理由不是"它们碍事",而是**它们的 drop 理由说的不是这件事**:
        # 那两条的 note 写的是「自指空转:信号类条目被 close 后下一轮又
        # 播种出一条新副本」—— 是在说**副本重复**这个 bug,不是在说
        # 「我不想再被提醒」。
        #
        # 用新语义追溯解释,等于给一个旧手势强加它从未想表达的、
        # 强得多的含义(永久静默)。旧数据不能自动获得新含义。
        if not out and self._should_propose_signal():
            out.append(Item(
                id=_SIGNAL_ID,
                title="当前无到期维护项,请人工确认下一步",
                detail=(
                    "自动播种检查后没有发现任何到期项:三条长期演进项"
                    "(依赖审计 / 性能基线 / 误报率实测)的 90 天周期都未到。"
                    "这不是故障,是「眼下确实没有自动推导的活」。"
                    "请人工决定:(a) 往 devloop/backlog.md 加真实的待办;"
                    "(b) 往 Queue._FALLBACK 加新的长期项;"
                    f"(c) 确认当前不需要推进,用 'arl-lite devloop done-item "
                    f"{_SIGNAL_ID}' 记录这次确认;"
                    f"(d) 不想再被提醒,用 'arl-lite devloop drop "
                    f"{_SIGNAL_ID} --reason ...' 明确关掉 —— "
                    "dropped 之后不会再提。"
                    "注意:不要为了'让队列非空'而随便造一条工作 —— "
                    "那是第 16 轮删掉的假活制造机。"
                ),
                priority=P2,
                kind="research",
                verify="true",  # 恒真:它的完成判据是「人确认过」,不是某个文件存在
                tags=["maintenance", "review"],
                created_round=round_no,
            ))
        elif not out:
            log.info(
                "queue._seed_fallback: 无到期项,且队里已有复查信号"
                "(待确认或已被明确 drop)—— 不重复提出"
            )
        return out

    def _should_propose_signal(self) -> bool:
        """现在该不该再提一条复查信号

        返回 False 的两种情况:

        1. 队里已有一条 **pending** 信号 —— 再提一条只是让同一个问题
           在队列里占两个位置,人还是得回答一次。
        2. 队里已有一条 **dropped** 信号 —— 人明确说过"别再提"。
           这条是 r22 补的:真实队列里躺着两条 dropped 副本,
           说明人当时就在试图让它闭嘴,而它照提不误。
           **静默覆盖人的显式指令,比队列空掉更坏。**

        返回 True 的情况:信号已被 close(done)。这时重提是**故意的** ——
        不变式 #4 要求队列永远非空,而"眼下确实没有活"这个事实
        需要有人持续面对。要停,人得用 drop,那是显式的开关。

        匹配含 `-rN` 派生副本:真实队列里就有,不是假想场景。
        """
        return not any(
            is_signal_id(i.id) and i.status in ("pending", "dropped")
            for i in self.load()
        )
