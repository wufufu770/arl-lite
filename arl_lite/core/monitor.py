"""arl_lite.core.monitor

资产监控:
- Monitor CRUD(monitors 表)
- 跑一次 monitor:对比上次结果,产生 change events
- 列出某个 monitor 的 changes

设计:
- monitors 表存监控任务配置
- asset_changes 表存变更事件
- diff 算法:用 last_run_at + first_seen 判断 NEW_ASSET
- DISAPPEARED:靠 first_seen/last_seen 的时间差判断,不需要 snapshot 表
  (资产表的 upsert 纪律「first_seen 永不变、last_seen 每次见到就 UPDATE」
   已经把"存在过"和"最近还活着"编码进去了)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

log = logging.getLogger("arl_lite.core.monitor")

# 变更类型。schema 注释里列了 6 种,目前实现 2 种——
# 其余(TITLE_CHANGED 等)需要字段级快照对比,不在本轮范围内。
CHANGE_TYPES = ("NEW_ASSET", "DISAPPEARED")

# DISAPPEARED 默认宽限期:48 小时。
# 取 2× 常见监控周期(24h),意思是"容得下一次漏扫,拦得住真下线"。
# 0 = 立刻判定,会因单次不完整扫描刷出大量误报,只在明确知道扫描完整时用。
DEFAULT_DISAPPEARED_GRACE = 48 * 3600


def _iso_minus_seconds(iso: str, seconds: int) -> str:
    """把 ISO 时间戳往前推 seconds 秒,返回同格式 ISO 字符串

    解析失败时原样返回 —— 宁可退化成"无宽限"也不要抛异常打断整轮扫描。
    (调用方 detect_disappeared 对参数已经做过类型/范围校验)
    """
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        log.debug("_iso_minus_seconds: 无法解析 %r,按无宽限处理", iso)
        return iso
    return (dt - timedelta(seconds=seconds)).isoformat()


def parse_ts(value) -> datetime | None:
    """把 SQLite / Python 两种时间戳格式统一成可比较的 naive datetime

    ## 为什么不能直接字符串比较

    同一个库里的时间戳有两种来源,格式不一样:

    - `record_change` 写入 → `datetime.utcnow().isoformat()` → `2026-10-02T04:46:53.083355`
    - 走 schema 默认值的行 → SQLite `CURRENT_TIMESTAMP` → `2026-10-02 04:46:53`

    分隔符一个是 `T`(0x54)一个是空格(0x20),**空格排在 T 前面**。
    所以哪怕是同一秒,`'... 04:46:53' >= '...T04:46:53'` 也是 False。
    按字符串比大小会得出"资产在上报之后还活着"的相反结论,
    于是已经报过的下线被反复上报(刷屏),或者该报的没报。

    ## 边界处理

    - 带时区的 ISO(`+00:00`)→ 去掉 tzinfo 统一成 naive。
      库里存的都是无时区的本地时间语义,混着比较会直接抛 TypeError。
    - 解析不了 → 返回 None,交给调用方决定。**不猜**。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    s = str(value).strip()
    if not s:
        return None
    # SQLite CURRENT_TIMESTAMP 用空格分隔且不带微秒
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=None)


class Monitor:
    """资产监控管理"""

    def __init__(self, storage):
        self.s = storage

    def add(self, target: str, monitor_type: str = "full",
            interval_seconds: int = 86400, webhook: str | None = None) -> int:
        """添加监控"""
        with self.s._conn() as conn:
            # 重复检查
            existing = conn.execute(
                "SELECT id FROM monitors WHERE workspace_id = ? AND target = ? AND monitor_type = ?",
                (self.s.workspace_id, target, monitor_type),
            ).fetchone()
            if existing:
                raise ValueError(f"monitor already exists for {target!r} (id={existing['id']})")
            cur = conn.execute(
                """INSERT INTO monitors
                   (workspace_id, target, monitor_type, interval_seconds,
                    enabled, notify_webhook, created_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (self.s.workspace_id, target, monitor_type, interval_seconds,
                 webhook, datetime.utcnow().isoformat())
            )
            return cur.lastrowid

    def remove_by_target(self, target: str) -> int:
        """按 target 删监控(返回删除数)"""
        with self.s._conn() as conn:
            cur = conn.execute(
                "DELETE FROM monitors WHERE workspace_id = ? AND target = ?",
                (self.s.workspace_id, target),
            )
            return cur.rowcount

    def list(self, enabled_only: bool = False) -> list[dict]:
        """列出监控"""
        with self.s._conn() as conn:
            if enabled_only:
                rows = conn.execute(
                    "SELECT * FROM monitors WHERE workspace_id = ? AND enabled = 1 ORDER BY id",
                    (self.s.workspace_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM monitors WHERE workspace_id = ? ORDER BY id",
                    (self.s.workspace_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get(self, monitor_id: int) -> dict | None:
        """取单个监控"""
        with self.s._conn() as conn:
            row = conn.execute(
                "SELECT * FROM monitors WHERE id = ? AND workspace_id = ?",
                (monitor_id, self.s.workspace_id),
            ).fetchone()
        return dict(row) if row else None

    def remove(self, monitor_id: int) -> bool:
        """删除监控"""
        with self.s._conn() as conn:
            cur = conn.execute(
                "DELETE FROM monitors WHERE id = ? AND workspace_id = ?",
                (monitor_id, self.s.workspace_id),
            )
            return cur.rowcount > 0

    def enable(self, monitor_id: int, enabled: bool = True) -> bool:
        """启用/禁用"""
        with self.s._conn() as conn:
            cur = conn.execute(
                "UPDATE monitors SET enabled = ? WHERE id = ? AND workspace_id = ?",
                (int(enabled), monitor_id, self.s.workspace_id),
            )
            return cur.rowcount > 0

    def record_run(self, monitor_id: int, change_count: int = 0) -> None:
        """记录跑过(更新 last_run_at)"""
        with self.s._conn() as conn:
            conn.execute(
                """UPDATE monitors
                   SET last_run_at = ?, last_change_count = ?
                   WHERE id = ? AND workspace_id = ?""",
                (datetime.utcnow().isoformat(), change_count,
                 monitor_id, self.s.workspace_id)
            )

    _ASSET_TABLES = {"domain": "domains", "host": "hosts", "port": "ports",
                     "site": "sites", "finding": "findings"}

    def detect_changes(self, asset_type: str, since_iso: str) -> list[dict]:
        """检测 since 之后的新增资产(基于 first_seen)"""
        # asset_type 白名单(与 storage.query 同纪律),不拼接
        table = self._ASSET_TABLES.get(asset_type)
        if table is None:
            raise ValueError(f"unknown asset_type: {asset_type!r} "
                             f"(choose from {sorted(self._ASSET_TABLES)})")
        with self.s._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE workspace_id = ? AND first_seen >= ?",
                (self.s.workspace_id, since_iso)
            ).fetchall()
        return [dict(r) for r in rows]

    def detect_disappeared(
        self,
        asset_type: str,
        since_iso: str,
        grace_seconds: int = DEFAULT_DISAPPEARED_GRACE,
    ) -> list[dict]:
        """检测「本次运行没再出现」的资产(基于 last_seen)

        ## 判定条件

        某资产相对一次运行(起始 `since_iso`)算 DISAPPEARED,当且仅当:

        - `first_seen < since_iso` —— 运行开始前它就存在(否则是新增,不是消失)
        - `last_seen  < since_iso` —— 本次运行期间一次都没再见到

        资产表的 upsert 纪律是「first_seen 永不变,last_seen 每次见到就 UPDATE」,
        所以这两个时间戳天然编码了"存在过"和"最近还活着"。**不需要额外的
        snapshot 表**——这就是当初把 last_seen 设计成必更新的原因。

        ## 去抖(grace_seconds)

        朴素实现有个致命问题:一次**不完整**的扫描(某个模块超时、crt.sh 限流、
        DNS 解析失败)会让它本该覆盖的资产全部"消失",于是一条抖动就刷出
        上千条 DISAPPEARED 告警。

        所以加宽限期:资产必须连续 `grace_seconds` 没被见到才判定下线。
        24h 周期 + 默认 48h 宽限 = 容得下一次漏扫,拦得住真下线。

        ## 这个方法解决不了什么(重要)

        **时间宽限区分不了「资产真没了」和「负责发现它的模块这轮挂了」。**
        一次部分失败的扫描跑满两轮之后,宽限期照样会被耗尽,误报照样发生。
        真正的解法需要按数据源的成功率来算(模块失败 → 该模块的数据不算"消失"),
        而 arl-lite 目前没有采集这个信息。

        所以这里选择:把机制做对(状态判定 + 状态转移去重 + 可配置宽限),
        **把局限写明白**,而不是假装解决了。误报率的实测是队列里的
        `item-21-c66b81`,它依赖真实数据源才能给结论。

        Args:
            asset_type: domain/host/port/site/finding
            since_iso: 本次运行的起始时间(ISO),对应 detect_changes 的同一参数
            grace_seconds: 宽限期秒数。0 = 立刻判定(慎用,会抖)

        Returns:
            消失资产行列表(与 detect_changes 同形状)
        """
        table = self._ASSET_TABLES.get(asset_type)
        if table is None:
            raise ValueError(f"unknown asset_type: {asset_type!r} "
                             f"(choose from {sorted(self._ASSET_TABLES)})")
        if grace_seconds < 0:
            raise ValueError(f"grace_seconds must be >= 0, got {grace_seconds}")

        # 宽限期换算成时间戳上界:比 since_iso 再早这么多仍没被见到 → 算消失。
        # 用 Python 算而不是 SQL 的 datetime(),因为 since_iso 是 ISO 字符串,
        # 交给 SQLite 解析会踩时区/格式的坑。
        cutoff = _iso_minus_seconds(since_iso, grace_seconds)

        # 表名来自上面的白名单映射,不接受外部拼接
        sql = f"""SELECT * FROM {table}
                  WHERE workspace_id = ?
                    AND first_seen < ?
                    AND (last_seen IS NULL OR last_seen < ?)"""
        params = [self.s.workspace_id, since_iso, cutoff]
        with self.s._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def filter_newly_disappeared(self, rows: list[dict]) -> list[dict]:
        """从消失行里滤掉"已经报过 DISAPPEARED 且之后没复活"的

        不做去重的话,一次下线会被每轮扫描重复上报一遍,`monitor changes`
        里全是同一条hash 的刷屏。

        判据:asset_changes 里已存在一条同 hash 的 DISAPPEARED 记录,且它的
        detected_at **晚于**该资产的最后一次 last_seen —— 说明"消失"这个
        状态转移已经报过了,还没恢复过。资产复活后 last_seen 会前移,
        于是下次再消失时又能正常上报。

        这是**状态转移**检测而不是水位检测:比"有没有报过"准,因为它能
        区分"一直没复活"和"复活后又掉了"。
        """
        if not rows:
            return []
        out = []
        with self.s._conn() as conn:
            for row in rows:
                asset_hash = row.get("hash")
                if not asset_hash:
                    continue
                prior = conn.execute(
                    """SELECT detected_at FROM asset_changes
                       WHERE workspace_id = ? AND asset_hash = ?
                         AND change_type = 'DISAPPEARED'
                       ORDER BY id DESC LIMIT 1""",
                    (self.s.workspace_id, asset_hash),
                ).fetchone()
                if not prior:
                    out.append(row)
                    continue
                # 必须解析成 datetime 再比。直接比字符串的话,
                # SQLite CURRENT_TIMESTAMP 的空格分隔格式永远小于
                # Python isoformat 的 T 分隔格式,去重会完全失效。
                reported_at = parse_ts(prior["detected_at"])
                last_seen = parse_ts(row.get("last_seen"))
                if reported_at is None or last_seen is None:
                    # 时间戳认不出来就不去重:宁可重复报一次,也别漏报下线
                    log.debug(
                        "filter_newly_disappeared: hash=%s 时间戳无法比较,"
                        "detected_at=%r last_seen=%r,按未上报处理",
                        asset_hash, prior["detected_at"], row.get("last_seen"),
                    )
                    out.append(row)
                    continue
                if reported_at >= last_seen:
                    # 已有上报记录,且记录时间不早于最后存活时间 → 已报过
                    continue
                out.append(row)
        return out


DEFAULT_BASELINE_LEARN = 3


def _val_identity(value):
    """给一个从 JSON 里读出来的值一个可比较的身份

    带类型名:JSON 的 `true` 和 `1` 被 Python 读成 `True` 和 `1`,
    而 `True == 1` 为真 —— 布尔字段改成数字会被误当成"变回过"。
    """
    return (type(value).__name__, value)


def _field_transitions(rows, field: str) -> tuple[int, bool]:
    """从历史行里读出这个字段"变过几次"和"有没有变回过"

    返回 `(证据条数, 是否变回过)`。

    ## 「变回过」是把取值排成一条序列,看有没有重复

    序列 = 第一次的 before,后面每次的 after。所以

    - `A→B, B→A` 的序列是 `A, B, A` —— A 出现两次,它回来过,是抖动;
    - `A→B1, B1→B2` 的序列是 `A, B1, B2` —— 三个值各一次,它一路往前,
      是演进。

    **不能**在遍历时看"当前 before 在不在见过的值里"—— 连续链的接缝
    必然命中(A→B 之后见过 B,下一条 B→C 的 before 就是 B),那样两段以上
    的链一律被判成"变回过",等于这个判据根本不干活。接缝在序列里只占
    **一个**位置,所以必须先把序列拼出来再看重复。

    ## 为什么 LIKE 不能当判据,一定要 parse

    `diff LIKE '%"ip"%'` 会把 `{"geo": {"before": {"ip": ...}}}` 这种**嵌套**
    同名字段也算进去 —— 那只是别的字段的取值内容。所以 LIKE 现在只当
    **预筛**:宁可多捞几行(漏掉才是致命的),捞回来的一律 parse,
    判据由 parse 后的**顶层 key** 决定。预筛里 `%`/`_` 仍是通配符,
    只会多捞不会少捞,这个方向的宽松是安全的。

    ## 认不出来的行不算证据

    diff 缺失 / 坏 JSON / 结构不对 → 跳过,**不**当成"变回过"。
    少一条证据 = 少一次抑制 = 多报一次。反过来会把真变化吃掉。
    """
    n = 0
    values: list = []  # 用 list 不用 set:JSON 值可能是 dict/list,不可哈希
    for row in rows:
        try:
            parsed = json.loads(row["diff"]) if row["diff"] else None
        except (TypeError, ValueError):
            parsed = None
        entry = parsed.get(field) if isinstance(parsed, dict) else None
        if not isinstance(entry, dict) or "before" not in entry or "after" not in entry:
            log.debug("基线判据:第 %s 行的 diff 里认不出字段 %s,跳过",
                      row["id"], field)
            continue
        n += 1
        b = _val_identity(entry["before"])
        # 接缝上的 before 就是上一条的 after,重复记一次会把"继续往前"
        # 误读成"回来过"(第一版就这么写错了)。但断链时(中间有没记到的
        # 变更)它是**新出现**的取值,必须补进去,否则后续的回访会漏判。
        if not values or values[-1] != b:
            values.append(b)
        values.append(_val_identity(entry["after"]))
    # 有过重复取值 = 回来过。用切片而不是 set:值可能不可哈希。
    came_back = any(v in values[:i] for i, v in enumerate(values))
    return n, came_back


def is_baseline_noise(storage, asset_hash: str, change_type: str,
                      field: str | None = None,
                      threshold: int = DEFAULT_BASELINE_LEARN) -> bool:
    """这个变更是不是"这个资产本来就这样"的基线噪声

    ## 判据:两条都要满足

    1. 同一 `asset_hash` + `change_type` + `field` 已经变过 **至少
       `threshold` 次**;
    2. 历史里这个字段**变回过** —— 某个取值出现过不止一次。

    只看第 1 条会把单向演进一起吃掉(r41 实测修掉的):证书到期日一路
    往后推、IP 段迁移、DNS 切到新机房,全都是"变了 N 次"却从不停在
    某个值上,默认阈值 3 意味着第 4 次起就被静默 —— 而那恰恰是最该
    被看见的东西。加上第 2 条之后:抖动(A→B→A)照常压,演进
    (A→B1→B2→B3)继续报。

    前 `threshold - 1` 次照常上报:刚开始抖的时候没人知道它是抖动,
    这正是"学习"的含义 —— 连着抖够多次才敢下结论。

    ## 第 2 条为什么问"回来过没有"而不是"取值不超过 2 个"

    三值轮转(A→B→C→A)抖得比两值还厉害,但取值有 3 个 —— 按"不超过
    2 个"会把它误判成演进,于是永远刷屏。"某个取值出现过不止一次"
    直接问的就是它回来没有,3 值轮转照样判成抖动。

    ## `field=None` 一律 False

    没有字段就无从判断变没变回过,不构成任何噪声结论。

    ## 为什么不新增存储字段

    `asset_changes` 表自己就是历史:它记了每一次变更的 asset_hash /
    change_type / diff / detected_at。用它当基线库,schema 一个字不用动,
    而基线随历史自然演化 —— 资产稳定久了,下次再变就会重新计次。

    ## 局限(说在前面)

    - **只看历史,不看时间。** 一年前抖过 3 次的资产,今天再抖会被当基线;
      反过来一年前演进过的资产,今天开始真抖也要先攒够次数才学得出来。
    - **阈值是全局的,不 per-asset 学习。** 按资产各自的历史长度调阈值
      要一张新表,那超出这一步的范围。
    - **判据要靠 before/after 快照,只有单边快照的变更学不出来。**
      `record_change` 允许只传 `after`(NEW_ASSET)或只传 `before`
      (DISAPPEARED),那类记录没有字段级 diff,一律照报。
    """
    if threshold <= 0 or field is None:
        return False
    sql = ("SELECT id, diff FROM asset_changes "
           "WHERE workspace_id = ? AND asset_hash = ? AND change_type = ? "
           "AND diff IS NOT NULL AND diff LIKE ? ORDER BY id")
    params: list = [storage.workspace_id, asset_hash, change_type,
                    f'%"{field}"%']
    with storage._conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    n, came_back = _field_transitions(rows, field)
    return int(n) >= int(threshold) and came_back


def record_change(storage, asset_type: str, change_type: str,
                  asset_hash: str, before: dict | None = None,
                  after: dict | None = None, task_id: int | None = None,
                  baseline_threshold: int = DEFAULT_BASELINE_LEARN) -> bool:
    """记录一个变更事件到 asset_changes 表。返回这条是否**被记下来**。

    ## 返回 False = 被基线判为噪声,没记

    判据见 `is_baseline_noise`。返回 bool 而不是 None,是为了让调用方
    知道"这条没报出去"—— 而不返回的话,调用方无法区分"记了"和"被挡了",
    报表上就会出现"报了 0 条",看起来像没检测到,其实是被去噪了。

    ## `change_type` 必须在 `CHANGE_TYPES` 里,否则 raise

    和 `Monitor.detect_changes` 对 `asset_type` 用 `_ASSET_TABLES` 白名单
    是同一套纪律。**报错,不静默改写** —— 静默改写等于把一个拼错换成
    另一个拼错,错得一模一样但更难查。

    ## 为什么按「能力」而不是按 schema 注释里的「词表」

    schema 的注释列了 6 种可能的取值(NEW_ASSET / DISAPPEARED /
    TITLE_CHANGED / TECH_CHANGED / FINGERPRINT_CHANGED / STATUS_CHANGED),
    那是**这张表能存什么**。`CHANGE_TYPES` 只有 2 种,是**本模块产得出
    什么**。按词表校验的话,"注释里写过"就等于"实现了" —— 而事实是
    `TITLE_CHANGED` 这类字段级变更**根本没有实现**:它们需要一个
    「上一轮的字段快照」来对比,而资产表是原地 upsert 的,不留历史,
    `asset_changes` 里也只有变更本身。r42 实测(白名单收紧前):
    传 `'随便编的'` 或 `''` 都能入库并返回 True,拼错一个字母就是一条
    永久静默的记录 —— 入库了,却没有任何代码路径会生成它。

    ## r42 收紧白名单的直接后果,说在前面

    **基线机制在生产路径上够不着了。** `record_change` 要判基线必须有
    双边快照,而生产里唯一的两处调用(watcher 的 NEW_ASSET 只传 `after`、
    DISAPPEARED 只传 `before`)都是单边的。所以 r40/r41 那套判据目前
    只能靠直接构造历史行来验证,真实的 watcher 跑一次也不会走到它。

    这是**能力缺失(做不了)**,不是这次修掉的 bug:资产自身的属性变了
    (换 IP、换标题、换证书)系统本来就看不见。r42 只做的是把「看不见」
    从静默变成报错。要真正用上基线,先得实现一种带字段快照的变更类型,
    那是另一件事,已单独立项。
    """
    if change_type not in CHANGE_TYPES:
        raise ValueError(
            f"unknown change_type: {change_type!r} "
            f"(choose from {list(CHANGE_TYPES)})")

    # 逐字段判基线:只要**有一个**变动字段已达阈值,整条就是噪声。
    # 用任一而不是全部 —— 一个资产天天变的往往就那一项(比如 geo 漂移),
    # 拿它当基线不代表整条记录都不值得看,但足以说明这一条会持续刷屏。
    if before and after and baseline_threshold > 0:
        for k in set(before) | set(after):
            if _val_identity(before.get(k)) == _val_identity(after.get(k)):
                continue
            if is_baseline_noise(storage, asset_hash, change_type, k,
                                 baseline_threshold):
                log.info(
                    "record_change: %s/%s 的 %s 变过 %d 次且变回过,"
                    "判为基线抖动,不记",
                    asset_hash[:12], change_type, k, baseline_threshold)
                return False

    diff = None
    if before and after:
        # 字段级 diff
        diff = {}
        keys = set(before.keys()) | set(after.keys())
        for k in keys:
            bv = before.get(k)
            av = after.get(k)
            # 比身份不比相等:`True == 1`、`False == 0` 在 Python 里为真,
            # 用 `!=` 判会把「布尔字段改成数字」整条吞掉 —— 变更记了,
            # diff 却是空的(存成 NULL),基线系统永远看不见它。
            if _val_identity(bv) != _val_identity(av):
                diff[k] = {"before": bv, "after": av}
    try:
        with storage._conn() as conn:
            conn.execute(
                """INSERT INTO asset_changes
                   (workspace_id, asset_hash, asset_type, change_type,
                    before_value, after_value, diff, detected_at, detected_by_task_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (storage.workspace_id, asset_hash, asset_type, change_type,
                 json.dumps(before, ensure_ascii=False, default=str) if before else None,
                 json.dumps(after, ensure_ascii=False, default=str) if after else None,
                 json.dumps(diff, ensure_ascii=False, default=str) if diff else None,
                 datetime.utcnow().isoformat(), task_id)
            )
    except Exception as e:
        log.warning(f"record_change failed: {e}")
        return False
    return True


def list_changes(storage, asset_type: str | None = None,
                 change_type: str | None = None, limit: int = 50) -> list[dict]:
    """列变更事件"""
    sql = "SELECT * FROM asset_changes WHERE workspace_id = ?"
    params: list = [storage.workspace_id]
    if asset_type:
        sql += " AND asset_type = ?"
        params.append(asset_type)
    if change_type:
        sql += " AND change_type = ?"
        params.append(change_type)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with storage._conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
