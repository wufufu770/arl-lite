"""arl_lite.devloop.queue — 待办队列模块(自持迭代协议的"下一步供给器")

设计意图
--------
这是 arl-lite 自持迭代协议(arl_lite.devloop.protocol)的"动力源"。

协议不变量:`待办队列永远非空`(见 devloop/__init__.py 第 12 行)。当所有 item
都 done/dropped 时,系统不能停下,必须能自动从三个来源补充新待办:

    Tier 1 — 人工追加:devloop/backlog.md(每行一条,人写的长期待办清单)
    Tier 2 — 项目状态自动推导(扫规则库/集成数/测试覆盖/计划文档)
    Tier 3 — 保底:3 条长期演进项(依赖审计 / 性能基线 / 误报率实测)

队列的核心职责:
    1. 持久化:JSON 落盘,原子写(tmp + os.replace),崩溃后可恢复
    2. 调度:next() 按 (priority asc, created_round asc, id asc) 稳定排序
    3. 状态机:pending → in_progress → done | dropped,attempts 在 in_progress 时 +1
    4. 自愈:seed_if_empty() 永远能产生至少 1 条新 item(协议不变量)

零依赖:只用标准库(json / os / tempfile / pathlib / dataclasses / re / logging)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import hashlib
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
            cand.owner = owner or f"pid-{os.getpid()}"
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
            if owner and target.owner and target.owner != owner:
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

    # pid 出现在 owner 串末尾:CLI 默认名是 `agent#1234`,内部兜底是 `pid-1234`
    _OWNER_PID = re.compile(r"(?:#|^pid-)(\d+)$")

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
    def add(self, item: Item) -> None:
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
    def seed_if_empty(self) -> int:
        """空了就播种。返回新增条数。

        三级策略(优先级递减):
            1. 读 devloop/backlog.md(人工长期待办清单)
            2. 扫项目状态自动推导(规则/集成/测试/文档)
            3. 保底 3 条长期演进项

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

        # ── Tier 2:自动推导 ──
        if not added:
            # 语义是"**总是提出**":这一层按仓库现状判断"还有没有活",
            # 条件仍然成立就说明活确实没干完(如规则数仍 < 40),
            # 所以复活是对的,只是 id 要换。传空集让 _disambiguate 处理冲突。
            #
            # 这里不能用"跳过":上一轮实测三个保底项被全跳过后
            # seed_if_empty 返回 0,队列空掉,不变式 #4 真的破了。
            added += self._seed_from_project_state(set(), next_round)
            existing_ids |= {i.id for i in added}

        # ── Tier 3:保底 ──
        if not added:
            # 长期演进项(L1..L5)本来就是"做到就算一轮、还会再来"的,
            # 所以永远提出 + 改名。保底层必须总能提出东西,否则它不是保底。
            added += self._seed_fallback(set(), next_round)

        if added:
            # 兜底:任何重复 id 都在这里被就地改名。
            # 三级策略里哪一层写错了都不至于产出重复 id。
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

    def repair_duplicates(self) -> int:
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

    def unmark(self, item_id: str, reason: str = "") -> tuple[bool, str]:
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

    # ── Tier 2:项目状态自动推导 ──────────────────────────────────────────
    _RULES_DIR = Path("arl_lite/modules/analysis/rules")
    _INTEGRATIONS_DIR = Path("arl_lite/integrations")
    _TESTS_DIR = Path("tests")
    _PLAN_DOC = Path("docs/PROJECT_PLAN.md")
    _RULE_RISK = re.compile(r"^risk:\s*(\d+)\s*$", re.MULTILINE)
    _RULE_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
    # 置信度字段。第 1 轮把置信度从 `low-confidence` **标签**改成了
    # `confidence:` **字段**(high/medium/low 三档)。
    _RULE_CONFIDENCE = re.compile(r"^confidence:\s*(\S+)\s*$", re.MULTILINE)

    def _scan_repo_root(self) -> Path | None:
        """尽量推断 repo 根目录:从 queue.json 向上找 pyproject.toml。"""
        cur = self.path.resolve().parent
        for _ in range(6):
            if (cur / "pyproject.toml").exists() and (cur / "arl_lite").is_dir():
                return cur
            cur = cur.parent
        return None

    def _seed_from_project_state(
        self, existing_ids: set[str], round_no: int
    ) -> list[Item]:
        """扫项目状态,产生结构化待办。

        推导规则(每一条都必须能对着仓库现状独立核实):
          - 规则 yml 中 risk >= 7 且**没有 confidence 字段** → "加置信度标注"
          - 规则数 < 40 → "补充关联分析规则覆盖"
          - 集成源模块数 < 12 → "新增数据源"
          - 测试 phase 文件缺失 → "补测试"
          - docs/PROJECT_PLAN.md 与实现差距 → "对齐项目计划文档"

        ## 这一层最容易出的错:拿旧事实推新待办

        规则改过之后(比如置信度从 `low-confidence` 标签改成
        `confidence:` 字段),这个推导不会跟着改,于是对每一条高风险
        规则都报"缺标注"。第 13 轮实测:37 条规则全部有 confidence 字段,
        标签 0 条,而推导照样挑出风险最高的 3 条报成新活。

        **假活比队列空掉更坏** —— 队列空掉会报错,假活会让人真的去干
        一遍已经做完的事,干完还会被标成 done。

        所以改完规则模型之后要回来核对这一层。
        `tests/test_devloop_seed_truthfulness.py` 把每条断言都独立验了一遍。
        """
        repo = self.scan_repo_root()
        if repo is None:
            return []
        out: list[Item] = []

        # (a) 高风险规则缺置信度标注
        #
        # 这里判的是 `confidence:` **字段**是否存在,不是找 `low-confidence` 标签。
        # 第 1 轮把置信度从标签改成了字段,标签已经全部消失(0/37),
        # 而这个检查还在找标签 —— 于是 25 条 risk≥7 的规则**全部**被误判成
        # "缺置信度标注",取风险最高的 3 条报成新待办。
        # 实测:`database_with_public_web` / `docker_api_exposed` /
        # `elasticsearch_public` 三条明明都有 confidence 字段,却被要求补标注。
        #
        # 播种必须基于**当下**的事实。规则改过一轮之后,这个检查不跟着改,
        # 产出的就是假活 —— 比队列空掉更坏:空队列会报错,假活会让人白干。
        rules_dir = repo / self._RULES_DIR
        if rules_dir.is_dir():
            high_risk_no_conf = []
            all_rules = sorted(
                p for p in rules_dir.glob("*.yml") if p.is_file()
            )
            for yml in all_rules:
                try:
                    txt = yml.read_text(encoding="utf-8")
                except OSError:
                    continue
                risk_m = self._RULE_RISK.search(txt)
                name_m = self._RULE_NAME.search(txt)
                if not risk_m or not name_m:
                    continue
                risk = int(risk_m.group(1))
                conf_m = self._RULE_CONFIDENCE.search(txt)
                if risk >= 7 and not conf_m:
                    high_risk_no_conf.append((name_m.group(1), risk))
            # 限 3 条,按风险降序
            high_risk_no_conf.sort(key=lambda x: (-x[1], x[0]))
            for name, risk in high_risk_no_conf[:3]:
                iid = f"add-confidence-{name}"
                if iid in existing_ids:
                    continue
                out.append(
                    Item(
                        id=iid,
                        title=f"为规则 {name} 加置信度标注",
                        detail=(
                            f"规则 {name} 的 risk={risk} ≥ 7,但 tags 中无 "
                            f"low-confidence。建议在 advice 中补充置信度来源、"
                            f"误报场景与样本不足时的处理建议。"
                        ),
                        priority=P2,
                        kind="change",
                        verify=(
                            f"grep -q low-confidence arl_lite/modules/analysis/"
                            f"rules/{name}.yml"
                        ),
                        tags=["confidence", "rules", name],
                        created_round=round_no,
                    )
                )

            # (b) 规则数 < 40 → 补充
            if len(all_rules) < 40:
                iid = "supplement-rules-coverage"
                if iid not in existing_ids:
                    out.append(
                        Item(
                            id=iid,
                            title="补充关联分析规则覆盖",
                            detail=(
                                f"目前 {len(all_rules)} 条规则,目标 ≥ 40。"
                                f"优先补充:云服务暴露(S3/OSS/Azure Blob)、"
                                f"API 网关未鉴权、Web 框架 CVE、备份文件残留。"
                            ),
                            priority=P2,
                            kind="change",
                            verify=(
                                f"python3 -c \"import pathlib; "
                                f"n=len(list(pathlib.Path('arl_lite/modules/"
                                f"analysis/rules').glob('*.yml'))); "
                                f"assert n>=40, n\""
                            ),
                            tags=["rules", "coverage"],
                            created_round=round_no,
                        )
                    )

        # (c) 集成源数 < 12
        integ_dir = repo / self._INTEGRATIONS_DIR
        if integ_dir.is_dir():
            srcs = [
                p
                for p in integ_dir.glob("*.py")
                if p.stem not in ("__init__", "tool_checker")
            ]
            if len(srcs) < 12:
                iid = "add-data-source"
                if iid not in existing_ids:
                    out.append(
                        Item(
                            id=iid,
                            title="新增数据源集成",
                            detail=(
                                f"目前 {len(srcs)} 个数据源,目标 ≥ 12。"
                                f"候选:Censys/Shodan/ThreatBook/ZoomEye/"
                                f"Binaryedge。复用 arl_lite.integrations 子类模板。"
                            ),
                            priority=P3,
                            kind="change",
                            verify=(
                                f"python3 -c \"import pathlib; "
                                f"n=len([p for p in pathlib.Path('arl_lite/"
                                f"integrations').glob('*.py') "
                                f"if p.stem not in ('__init__','tool_checker')]); "
                                f"assert n>=12, n\""
                            ),
                            tags=["integrations", "data-source"],
                            created_round=round_no,
                        )
                    )

        # (d) 测试 phase 缺失
        tests_dir = repo / self._TESTS_DIR
        if tests_dir.is_dir():
            present = {
                p.stem
                for p in tests_dir.glob("test_phase*.py")
            }
            missing = [
                n
                for n in range(1, 8)
                if f"test_phase{n}" not in present
            ]
            if missing:
                iid = "add-missing-phase-tests"
                if iid not in existing_ids:
                    out.append(
                        Item(
                            id=iid,
                            title="补缺失阶段测试",
                            detail=(
                                f"缺失 phase 测试: {missing}。"
                                f"参考 test_phase1.py 的 pytest 风格补齐。"
                            ),
                            priority=P2,
                            kind="test",
                            verify=(
                                "python3 -m pytest "
                                + " ".join(f"tests/test_phase{n}.py" for n in missing)
                                + " -q"
                            ),
                            tags=["tests", "coverage"],
                            created_round=round_no,
                        )
                    )

        # (e) 计划文档对齐
        plan = repo / self._PLAN_DOC
        if plan.is_file():
            iid = "align-project-plan-doc"
            if iid not in existing_ids:
                out.append(
                    Item(
                        id=iid,
                        title="对齐 PROJECT_PLAN.md 与实现",
                        detail=(
                            "逐节检查 docs/PROJECT_PLAN.md 中声明的功能/阶段,"
                            "对照 arl_lite/* 实现,标注未完成/不一致条目,"
                            "并在 plan 中更新状态。"
                        ),
                        priority=P3,
                        kind="doc",
                        verify=(
                            "python3 -c \"import pathlib; "
                            "p=pathlib.Path('docs/PROJECT_PLAN.md'); "
                            "assert p.exists() and p.stat().st_size>200\""
                        ),
                        tags=["docs", "plan"],
                        created_round=round_no,
                    )
                )

        return out

    def scan_repo_root(self) -> Path | None:
        """公开别名,方便测试/协议层调用。"""
        return self._scan_repo_root()

    # ── Tier 3:保底 ──────────────────────────────────────────────────────
    _FALLBACK: tuple[dict, ...] = (
        {
            "id": "quarterly-dep-audit",
            "title": "季度依赖审计",
            "detail": (
                "pip-audit 或安全公告巡检,记录 optional-dependencies 与"
                "运行时风险的边界;目标保持核心零依赖(见 pyproject.toml)。"
            ),
            "priority": P3,
            "kind": "research",
            "verify": (
                "python3 -c \"import tomllib,pathlib; "
                "t=tomllib.loads(pathlib.Path('pyproject.toml')."
                "read_text()); assert t['project']['dependencies']==[]\""
            ),
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
            "verify": "test -f docs/PERF_BASELINE.md",
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
            "verify": "test -f docs/FP_RATE.md",
            "tags": ["rules", "quality"],
        },
    )

    def _seed_fallback(
        self, existing_ids: set[str], round_no: int
    ) -> list[Item]:
        """保底:3 条长期演进项。"""
        out: list[Item] = []
        for spec in self._FALLBACK:
            if spec["id"] in existing_ids:
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
        return out
