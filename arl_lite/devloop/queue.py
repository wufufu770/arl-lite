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


def _slugify_id(title: str, idx: int) -> str:
    """把标题派生成稳定可读的 id。

    中文标题 slug 化会退化成空串（正则只认 [a-z0-9]），所以：
    - 有 ASCII 部分 → 用它（`给规则加置信度 add-confidence-12`）
    - 纯中文 → 用序号 + 标题哈希前 6 位（`item-3-a1b2c3`）

    哈希保证同一份 backlog 里两条相似标题不会撞 id。
    """
    ascii_part = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:6]
    if ascii_part:
        return f"{ascii_part}-{idx}"
    return f"item-{idx}-{digest}"


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
        max_round = max((i.created_round for i in items), default=0)
        next_round = max_round + 1  # 每次 seed 视为新轮次

        added: list[Item] = []

        # ── Tier 1:backlog.md ──
        # backlog 内部自己会加 -r{round} 后缀,所以这里传全部已有 id
        added += self._seed_from_backlog(existing_ids, next_round)
        existing_ids |= {i.id for i in added}

        # ── Tier 2:自动推导 ──
        if not added:
            # 刻意传**空集**:这一层是"按仓库现状提出候选项",
            # 跳过已存在的 id 等于让保底失效(实测:三个保底项全被跳过后
            # seed_if_empty 返回 0,队列空掉,不变式 #4 破)。
            # 去重统一交给末尾的 _disambiguate,这里只管提。
            added += self._seed_from_project_state(set(), next_round)
            existing_ids |= {i.id for i in added}

        # ── Tier 3:保底 ──
        if not added:
            # 同上:保底层必须**总能**提出东西,否则它就不是保底。
            # id 冲突由 _disambiguate 加后缀解决。
            added += self._seed_fallback(set(), next_round)

        if added:
            # 兜底:任何重复 id 都在这里被就地改名。
            # 三级策略里哪一层写错了都不至于产出重复 id。
            added = self._disambiguate(added, existing_ids)
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
        self, existing_ids: set[str], round_no: int
    ) -> list[Item]:
        """读 devloop/backlog.md;不存在则创建空模板。"""
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
        for idx, line in enumerate(text.splitlines()):
            m = self._BACKLOG_LINE.match(line)
            if not m:
                continue
            prio = int(m.group(1))
            kind = m.group(2)
            title = m.group(3).strip()
            detail = m.group(4).strip()
            verify = m.group(5).strip()
            # id 由 title 派生。中文标题 slug 化会退化成 "item"（正则只认
            # [a-z0-9]），所以中文为主时退化为稳定序号 + 短哈希——保证 id
            # 唯一、可排序、可读，而不是一堆同名 "item-N"
            base_id = _slugify_id(title, idx)
            iid = base_id
            if iid in existing_ids:
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
    _RULE_TAGS = re.compile(r"^tags:\s*\[([^\]]*)\]\s*$", re.MULTILINE)
    _RULE_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)

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

        推导规则:
          - 规则 yml 中 risk >= 7 且没有 low-confidence 标签 → "加置信度标注"
          - 规则数 < 40 → "补充关联分析规则覆盖"
          - 集成源模块数 < 12 → "新增数据源"
          - 测试 phase 文件缺失 → "补测试"
          - docs/PROJECT_PLAN.md 与实现差距 → "对齐项目计划文档"
        """
        repo = self.scan_repo_root()
        if repo is None:
            return []
        out: list[Item] = []

        # (a) 高风险规则缺置信度标注
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
                tags_m = self._RULE_TAGS.search(txt)
                name_m = self._RULE_NAME.search(txt)
                if not risk_m or not name_m:
                    continue
                risk = int(risk_m.group(1))
                tags_raw = tags_m.group(1) if tags_m else ""
                tags_list = [t.strip().strip("\"'") for t in tags_raw.split(",")]
                tags_list = [t for t in tags_list if t]
                if risk >= 7 and "low-confidence" not in tags_list:
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


# ─── 自测 ──────────────────────────────────────────────────────────────────
def _self_test() -> int:
    """跑一遍最小回路,作为 `python3 -m arl_lite.devloop.queue` 的入口。"""
    import shutil
    import sys
    import tempfile as _tf

    tmpdir = Path(_tf.mkdtemp(prefix="devloop-q-self-"))
    try:
        qpath = tmpdir / "queue.json"
        q = Queue(qpath)
        # 0. 初始:空文件 → 空列表
        assert q.load() == []
        assert q.exhausted() is True
        # 1. 种子第一次
        n1 = q.seed_if_empty()
        assert n1 > 0, "seed_if_empty 没产出任何 item"
        items1 = q.load()
        assert any(i.status == "pending" for i in items1)
        # 2. next() 可取
        first = q.next()
        assert first is not None
        # 3. 模拟一轮: in_progress → done(原地修改,需直接 save 当前 list)
        items_after_seed = q.load()
        q.mark_in_progress(first)
        q.mark_done(first, round_no=1)
        # first 还在 items_after_seed 里(同一对象引用),已就地修改
        # 4. 把所有 pending 都 done
        for it in items_after_seed:
            if it.status == "pending":
                q.mark_in_progress(it)
                q.mark_done(it, round_no=1)
        q.save(items_after_seed)
        assert q.exhausted() is True
        # 5. 再 seed 一次(可能产出 Tier 2 / Tier 3)
        n2 = q.seed_if_empty()
        assert n2 > 0, "二次 seed 失败"
        # 6. 排序验证
        items2 = q.load()
        pending = [i for i in items2 if i.status == "pending"]
        pending.sort(key=q._sort_key)
        for a, b in zip(pending, pending[1:]):
            assert (a.priority, a.created_round, a.id) <= (
                b.priority,
                b.created_round,
                b.id,
            )
        # 7. stats()
        st = q.stats()
        assert "pending" in st and "done" in st and "dropped" in st
        assert "by_priority" in st and len(st["by_priority"]) == 4
        # 8. mark_dropped
        items_now = q.load()
        target = q.next()
        if target is not None:
            # 找到并原地修改
            for i, it in enumerate(items_now):
                if it.id == target.id:
                    q.mark_in_progress(it)
                    q.mark_dropped(it, "self-test reason")
                    items_now[i] = it
                    break
            q.save(items_now)
        # 9. atomic write smoke:并发触发 save 不破坏文件
        for _ in range(5):
            q.save(q.load())
        assert qpath.exists()
        # 10. backlog.md 被自动创建
        bp = q._backlog_path()
        assert bp.exists()

        print(
            f"[OK] self-test passed. "
            f"first seed +{n1}, second seed +{n2}, "
            f"final stats={q.stats()}"
        )
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_self_test())
