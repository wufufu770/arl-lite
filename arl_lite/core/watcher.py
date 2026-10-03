"""arl_lite.core.watcher

Watch 模式 — 持续监控目标,新发现/变化时告警。

设计:
- 定期重跑 target(默认 24h,可配)
- 跟踪 last_run + new_assets count
- 新发现 critical / 资产变化触发 webhook
- 纯 stdlib(threading.Event + time)
- SIGTERM 优雅退出
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("arl_lite.core.watcher")

# `record_change_capped` 是模块级的,所以要能看见 `record_change`。
# monitor.py 模块层只 import 标准库,不 import 本包的任何东西,提上来不成环
# (`no_import_cycle` 门禁会验)。原来 `_run_target` 里那句延迟 import 是
# 给 TaskRunner 躲环用的,monitor 不需要。
from .monitor import record_change


def _as_int(v, default: int = 0) -> int:
    """状态文件里的计数器一律走这儿 —— 宁可当成 0,不要让 watcher 起不来"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return int(v)


# 单轮每种资产类型最多往 `asset_changes` 写多少条变更。
#
# 上限本身是对的:一轮扫出几万个新资产时,全写进去会把表和后面的查询一起拖垮。
# 问题从来不是「有没有上限」,而是**上限是静默的**。
#
# 为什么丢掉的是**永久**的(实测,不是推测):`detect_changes` 按
# `first_seen >= since_iso` 取,而下一轮的 `since_iso` 是本轮开始之后 ——
# 所以这一轮没记上的资产,下一轮 `first_seen` 已经早于窗口起点,再也不会被检出。
# 实测 250 个新 host、第 1 轮只记 200、第 2 轮检出 0 条、累计永远停在 200。
#
# 为什么「均匀抽样」不是改进(所以不做):上面那条查询**没有 `ORDER BY`**,
# SQLite 按什么顺序返回不保证,所以「留前 200 条」本来就已经是随机留。
# 抽样只是换一批丢,永久丢失率一模一样。
#
# 所以这个上限必须**自报**:丢了多少条、累计丢了多少,都要能被人查���
# (`record_change_capped` 返回被丢的条数,watcher 累加并写进状态)。
CHANGE_RECORD_CAP = 200


def record_change_capped(storage, asset_type: str, change_type: str,
                         rows: list[dict], snapshot: str = "after",
                         cap: int | None = None) -> int:
    """把一批变更写进 `asset_changes`,最多写 `cap` 条,**返回被丢掉的条数**

    `snapshot` 是单边快照的键名:`NEW_ASSET` 传 `"after"`(默认),
    `DISAPPEARED` 传 `"before"`。

    ## 为什么返回值是「丢了多少」而不是「写了多少」

    调用方真正要回答的问题是「这批东西**没记全**,对不对」——所以返回值
    直接给缺口,免得调用方自己写 `len(rows) - cap`,那个减法在
    `cap > len(rows)` 时是负数,而负数在日志里看着像「多记了几条」。

    ## 为什么不静默

    静默截断比截断本身更坏:用户看 `monitor changes` 拿到 200 条,看
    watcher 日志拿到 `new=250`,两个数字对不上,而**没有任何地方**提示过
    上限的存在。这不是「显示不全」,是变更记录本身缺了 —— 丢掉的资产
    下一轮就不再算新增,而基线判据正好读这张表。所以缺口要往回传。
    """
    if snapshot not in ("after", "before"):
        raise ValueError(f"snapshot must be 'after' or 'before', got {snapshot!r}")
    cap = CHANGE_RECORD_CAP if cap is None else int(cap)
    if cap < 0:
        raise ValueError(f"cap must be >= 0, got {cap}")
    keep = list(rows[:cap]) if cap < len(rows) else list(rows)
    for row in keep:
        record_change(storage, asset_type, change_type,
                      asset_hash=row.get("hash") or "",
                      task_id=row.get("task_id"),
                      **{snapshot: row})
    return len(rows) - len(keep)


def account_detected_and_recorded(
    detected: int, recorded: int, found: int, dropped: int,
) -> tuple[int, int]:
    """把一次 `record_change_capped` 的结果并进本轮的两个计数口径

    返回 `(detected, recorded)`,两个数都加同一次变更的份。

    ## 为什么必须走这一个函数(r53)

    两个口径曾经**不对称**:`detected` 只在新增分支加,`recorded`
    新增和消失都加。于是只要那一轮检出了消失资产,`detected < recorded`
    就恒成立,`account_change_recording` 的 `max(0, ...)` 把丢弃数夹成 0。
    实测(210 新增,`CHANGE_RECORD_CAP`=200,新增侧丢 10 条):

        无消失资产  -> detected=210 recorded=200 -> 报丢弃 10 条 ✓
        另有 20 消失 -> detected=210 recorded=220 -> 报丢弃  0 条 ✗

    **同样丢 10 条,只因多了一批消失就归零。** r50 建的「截断必须可见」
    在有消失资产的轮次里静默失效 —— 而消失检测是 watcher 的常规功能,
    几乎每轮都有东西在消失。

    口径对称这件事没法靠「两处都记得加」来保证:两处各写一行,迟早有一处
    忘了。所以收成一个函数,新增和消失都调它 —— 少加一处就少调一次,
    调用点数能数,也能测。

    ## 为什么参数是 `found` 和 `dropped` 而不是 `recorded_this_call`

    `dropped` 是 `record_change_capped` 的返回值,`found` 是它那一批的行数,
    两个都已经在手上了。让调用方自己算 `found - dropped` 再传进来,
    就等于把「记入数」这个减法复制一份到两处调用点 —— 同一个坑。
    """
    return detected + int(found), recorded + (int(found) - int(dropped))


def account_change_recording(wt, detected: int, recorded: int) -> int:
    """把本轮的截断情况记到 watch target 上,**返回本轮丢了几条**

    ## 为什么要抽出来而不是内联在 `_run_target` 里

    因为内联的那两行是本轮唯一的「丢多少条」接线,而它**完全测不到** ——
    `_run_target` 要真 TaskRunner 才跑得起来。r50 第一版的变异测试实测:
    把 `wt.dropped_change_count += dropped_this_run` 改成 `pass`,
    19 条测试**全绿** —— helper 层测得挺好,接线那一段没人验。
    抽成函数就能直接测,接线还在同一个地方。

    ## 为什么用 `max(0, ...)`

    `detected < recorded` 说明两个数被算错了(不是真的多记了),那时候
    报一个负的「丢弃数」会让人以为上界算出了负条数,更难查。夹到 0
    至少不会把一个 bug 伪装成另一个 bug 的证据。
    """
    dropped = max(0, int(detected) - int(recorded))
    wt.last_dropped_change_count = dropped
    wt.dropped_change_count += dropped
    return dropped


class WatchTarget:
    """单个 watch 目标

    Attributes:
        target: 域名/IP/URL
        modules: 跑哪些 module
        interval_seconds: 重跑间隔秒
        last_run: 上次跑时间戳
        next_run: 下次跑时间戳
        last_count: 上次跑完后的**全部资产**数 —— `Monitor._ASSET_TABLES`
            里每张资产表的行数之和。r52 之前它只加 `hosts` + `domains`
            两张表,而字段名承诺的是「资产数」,承载的却是「其中两种」——
            字段名和语义对不上就是 bug(决策 #5)。清单从契约表派生,
            不手写。
        run_count: 总跑次数
        new_count: 新资产数(累计)。同样覆盖全部 5 种资产表。
        dropped_change_count: 因为触到写入上限而**没能记进**
            `asset_changes` 的变更条数(累计)。和 `new_count` 分开是因为
            两者不是一回事:`new_count` 是资产表里真实多出来的行数,
            而这里是「检测到了但没落库」的条数 —— 见 `record_change_capped`。
    """
    def __init__(
        self,
        target: str,
        modules: list[str] | None = None,
        interval_seconds: int = 86400,  # 24h
    ):
        if not isinstance(target, str) or not target.strip():
            raise ValueError("watch target must be non-empty str")
        if len(target) > 1000:
            raise ValueError("watch target too long")
        self.target = target.strip()
        self.modules = modules or ["dns", "whois", "subfinder", "crtsh"]
        if not isinstance(self.modules, list):
            raise ValueError("modules must be list")
        if not isinstance(interval_seconds, int) or interval_seconds < 60:
            raise ValueError(f"interval_seconds must be int >= 60, got {interval_seconds}")
        self.interval_seconds = interval_seconds
        self.last_run: str | None = None
        self.next_run: str | None = None
        self.last_count: int = 0
        self.run_count: int = 0
        self.new_count: int = 0
        self.dropped_change_count: int = 0
        self.last_dropped_change_count: int = 0

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "modules": self.modules,
            "interval_seconds": self.interval_seconds,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "last_count": self.last_count,
            "run_count": self.run_count,
            "new_count": self.new_count,
            "dropped_change_count": self.dropped_change_count,
            "last_dropped_change_count": self.last_dropped_change_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WatchTarget":
        wt = cls(
            target=d["target"],
            modules=d.get("modules"),
            interval_seconds=d.get("interval_seconds", 86400),
        )
        wt.last_run = d.get("last_run")
        wt.next_run = d.get("next_run")
        wt.last_count = d.get("last_count", 0)
        wt.run_count = d.get("run_count", 0)
        wt.new_count = d.get("new_count", 0)
        # 老状态文件里没有这两个字段(它们是 r50 加的)。`int(...)` 而不是直接
        # 取值:JSON 里存成字符串或 null 时,直接用会让 `+= 1` 抛 TypeError,
        # 而那会让整个 watcher 起不来 —— 升级不该因为一个计数器字段挂掉。
        wt.dropped_change_count = _as_int(d.get("dropped_change_count"))
        wt.last_dropped_change_count = _as_int(d.get("last_dropped_change_count"))
        return wt


class Watcher:
    """Watch 调度器

    Usage:
        w = Watcher(storage, webhook_config=...)
        w.add("example.com", modules=["dns", "whois"], interval=3600)
        w.add("foo.com", interval=7200)
        w.start()  # 后台线程循环
        # ...
        w.stop()

    工作流:
    - 每个 target 独立调度(独立 interval)
    - 跑完调 WebhookConfig 通知
    - SIGTERM / stop() 优雅退出
    """

    def __init__(self, storage, webhook_config=None, on_run=None, state_path=None):
        self.storage = storage
        self.webhook = webhook_config
        self.on_run = on_run  # 可选 callback(target, found, duration, new_count)
        # 状态落盘路径:CLI 传入 ~/.arl-lite/watch/watch.json。
        # 为 None 时 watcher 不落盘(库/测试场景,纯内存调度)。
        self.state_path = state_path
        self._targets: dict[str, WatchTarget] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _persist_state(self) -> None:
        """把运行态写回 state_path(原子替换,避免半截文件)

        last_run/next_run 之前只活在内存,进程一重启就丢,watcher 会认为
        "从没跑过"从而立刻重复扫描。周期配得短的话等于自己打爆数据源。
        """
        if not self.state_path:
            return
        try:
            with self._lock:
                payload = [wt.to_dict() for wt in self._targets.values()]
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)  # 原子替换
        except Exception as e:
            # 落盘失败不该拖垮调度循环——watcher 主功能不依赖它
            log.warning(f"watch: failed to persist state: {e}")

    def add(self, target: str, modules=None, interval_seconds: int = 86400) -> WatchTarget:
        """添加 watch target"""
        wt = WatchTarget(target, modules, interval_seconds)
        with self._lock:
            self._targets[target] = wt
        self._persist_state()
        log.info(f"watch: added target={target} modules={wt.modules} interval={wt.interval_seconds}s")
        return wt

    def remove(self, target: str) -> bool:
        with self._lock:
            if target in self._targets:
                del self._targets[target]
                self._persist_state()
                log.info(f"watch: removed target={target}")
                return True
        return False

    def list(self) -> list[WatchTarget]:
        with self._lock:
            return list(self._targets.values())

    def start(self) -> None:
        """启动后台 watch 线程"""
        if self._thread and self._thread.is_alive():
            log.warning("watch: already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="watcher", daemon=True)
        self._thread.start()
        log.info(f"watch: started with {len(self._targets)} targets")

    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止

        join 前先向正在跑的扫描传播停止(光 set _stop 要等当前目标整个
        扫完);join 超时说明扫描还没退出,如实告警而不是谎报已停止
        (daemon 线程随后被强杀会留下 RUNNING 任务孤儿)。
        """
        self.request_stop_all()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning(f"watch: thread still running after {timeout}s stop (scan will be killed on exit)")
            else:
                log.info("watch: stopped")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- GracefulShutdown duck-typing 接口 ----
    # cli.cmd_watch_start 把 Watcher 传给 GracefulShutdown,信号处理器会依次调用
    # mark_stopped / request_stop_all / cleanup。缺了任何一个,第一次 SIGTERM/Ctrl+C
    # 就会被吞掉(AttributeError 被 except 吃掉),要按第二次才强制退出。

    def mark_stopped(self) -> None:
        """信号处理:watch 无任务状态可回写,仅留痕"""
        log.info("watch: shutdown requested (mark_stopped)")

    def request_stop_all(self) -> None:
        """信号处理:置停止事件,并通知当前正在跑的 TaskRunner 停止

        只 set _stop 的话,循环要等当前目标整个扫描完才会检查,一次
        Ctrl+C 就"按了没反应"。把停止传播到 runner 内的 modules,
        长扫描也能协作式退出。
        """
        self._stop.set()
        runner = getattr(self, "_current_runner", None)
        if runner is not None:
            try:
                runner.request_stop_all()
                runner.mark_stopped()
            except Exception as e:
                log.debug(f"watch: stop runner failed: {e}")

    def cleanup(self) -> None:
        """atexit 钩子:watch 无额外资源需要释放"""
        pass

    def _loop(self) -> None:
        """主循环:等 target 到期,跑

        lock 内只做到期判定和快照,真正扫描在 lock 外执行——否则一次
        几分钟的网络扫描会让 add/remove/list/status 全部卡死。
        """
        while not self._stop.is_set():
            now = time.time()
            next_wake = None
            due: list[WatchTarget] = []
            with self._lock:
                for wt in self._targets.values():
                    next_at = self._next_run_at(wt)
                    if next_at is None:
                        continue
                    if next_at <= now:
                        due.append(wt)
                    if next_wake is None or next_at < next_wake:
                        next_wake = next_at
            for wt in due:
                if self._stop.is_set():
                    break
                try:
                    self._run_target(wt)
                except Exception as e:
                    log.exception(f"watch: run failed for {wt.target}: {e}")
                wt.next_run = datetime.fromtimestamp(
                    time.time() + wt.interval_seconds
                ).isoformat()
                # 运行态回写落盘:之前 last_run/next_run 只活在内存里,
                # 进程重启后 watcher 不知道上次跑过,会立刻重复扫一遍。
                self._persist_state()
            # sleep 到下次(或 stop)
            if next_wake is None:
                wait = 60.0  # 没目标时 60s 醒一次
            else:
                wait = max(1.0, min(60.0, next_wake - time.time()))
            self._stop.wait(timeout=wait)

    def _next_run_at(self, wt: WatchTarget) -> float | None:
        if wt.next_run:
            try:
                return datetime.fromisoformat(wt.next_run).timestamp()
            except Exception:
                pass
        # 没 next_run:立即跑一次
        return time.time() - 1

    def _run_target(self, wt: WatchTarget) -> None:
        """跑一个 target(同步)— 包含变化检测 + monitor 变更接线"""
        from ..core.task_runner import TaskRunner  # 延迟 import 避免循环
        from ..core.monitor import Monitor, record_change
        from ..notify import notify_task_done, notify_critical_finding

        start = time.time()
        start_iso = datetime.utcnow().isoformat()
        log.info(f"watch: running target={wt.target} modules={wt.modules}")

        # 记录本次开始前资产数(走 COUNT(*),别全表载入再 len)
        before = self._count_assets()

        runner = TaskRunner(
            storage=self.storage,
            workspace_id=self.storage.workspace_id,
        )
        self._current_runner = runner  # request_stop_all 时传播停止
        try:
            # TaskRunner.run 是 async — 在新 event loop 跑
            import asyncio
            result = asyncio.run(
                runner.run(
                    target=wt.target,
                    modules=wt.modules,
                )
            )
        except Exception as e:
            log.exception(f"watch: TaskRunner failed for {wt.target}: {e}")
            return
        finally:
            self._current_runner = None

        after = self._count_assets()
        # 资产表清单**从 `Monitor._ASSET_TABLES` 派生**,不另抄一份(决策 #9)。
        #
        # 不能从 `get_stats()` 的键派生:它的键里还有 `tasks` 和
        # `correlations`(storage.py:get_stats),那两张表不是资产 ——
        # 把它们的行数算进「新增资产」是同一个错的镜像(该加的没加、
        # 不该加的加了),一样让报告总数失真。判据是「什么是资产」,
        # 而那个答案只有契约表里有。
        asset_tables = tuple(Monitor._ASSET_TABLES.values())
        # max(0):资产被删/并发清库时差值可为负,通知语义只关心"新增"
        new_by_type = {t: max(0, after.get(t, 0) - before.get(t, 0))
                       for t in asset_tables}
        new_total = sum(new_by_type.values())

        # 更新 target 状态
        wt.last_run = datetime.utcnow().isoformat()
        wt.next_run = datetime.fromtimestamp(
            time.time() + wt.interval_seconds
        ).isoformat()
        # 和 `new_total` 用同一份清单 —— 两处各自数一遍,迟早有一处漏
        wt.last_count = sum(after.get(t, 0) for t in asset_tables)
        wt.run_count += 1
        wt.new_count += new_total

        duration = time.time() - start
        # 逐类型打,不手写三个:手写的清单和上面的差值不是同一份,
        # 加了新资产种类就会只在这里漏(那正是 r52 的形状)。
        log.info(
            f"watch: target={wt.target} done in {duration:.1f}s, "
            f"new={new_total} "
            f"({' '.join(f'{t}={new_by_type[t]}' for t in asset_tables)})"
        )

        # 变更检测接线:本次新增的资产逐条写 asset_changes(之前 detect_changes
        # 没有任何调用者,`monitor changes` 永远是空的)
        #
        # 写入有上限(见 `CHANGE_RECORD_CAP`),而**丢掉的是永久的**:这一轮
        # 没记上的资产,下一轮 `first_seen` 已经早于窗口起点,再也不会被检出。
        # 所以每一处截断都要留痕 —— 静默截断比截断本身更坏。
        critical_findings: list[dict] = []
        disappeared_total = 0
        recorded_total = 0
        detected_total = 0
        # 同样从契约表派生:上面数「新增」用的是它,这里逐类做检测也得是
        # 同一份 —— 两份手抄的清单(r52 之前的三元组和五元组)必然漂移,
        # 而漂移的方向正是「少算」。
        for asset_type, table in Monitor._ASSET_TABLES.items():
            mon = Monitor(self.storage)
            try:
                new_rows = mon.detect_changes(asset_type, start_iso)
            except Exception as e:
                log.warning(f"watch: detect_changes({asset_type}) failed: {e}")
                new_rows = []
            dropped_new = record_change_capped(
                self.storage, asset_type, "NEW_ASSET", new_rows,
                snapshot="after")
            # 检出和记入**成对**并进本轮口径 —— 见 account_detected_and_recorded
            detected_total, recorded_total = account_detected_and_recorded(
                detected_total, recorded_total, len(new_rows), dropped_new)
            if dropped_new:
                log.warning(
                    f"watch: {asset_type} 本轮检出 {len(new_rows)} 个新资产,"
                    f"只记了 {len(new_rows) - dropped_new} 条 —— 上限 "
                    f"CHANGE_RECORD_CAP={CHANGE_RECORD_CAP} 截掉了 {dropped_new} 条。"
                    f"这 {dropped_new} 条**不会在后续轮次补上**(它们 first_seen "
                    f"已早于下一轮的检测窗口),所以这是永久缺失。"
                    f"要让它们进库就把上限调大或分批扫。")
            if table == "findings":
                critical_findings = [r for r in new_rows if r.get("severity") == "critical"]

            # 消失检测:本次运行没再出现的资产。宽限期默认 48h,
            # 理由和已知局限见 Monitor.detect_disappeared 的文档。
            try:
                gone = mon.detect_disappeared(asset_type, start_iso)
                # 状态转移去重:一直没人管的资产不会被每轮重复上报
                gone = mon.filter_newly_disappeared(gone)
            except Exception as e:
                log.warning(f"watch: detect_disappeared({asset_type}) failed: {e}")
                continue
            dropped_gone = record_change_capped(
                self.storage, asset_type, "DISAPPEARED", gone,
                snapshot="before")
            disappeared_total += len(gone)
            # 消失**也要**并进 detected —— r53:以前只有 recorded 加它,
            # 于是「检出」和「记入」成了两个口径,只要这轮有消失,
            # `max(0, detected - recorded)` 就恒为 0,截断丢失被吞掉。
            detected_total, recorded_total = account_detected_and_recorded(
                detected_total, recorded_total, len(gone), dropped_gone)
            if dropped_gone:
                log.warning(
                    f"watch: {asset_type} 本轮检出 {len(gone)} 个消失资产,"
                    f"只记了 {len(gone) - dropped_gone} 条 —— 截掉 {dropped_gone} 条。"
                    f"消失检测是状态转移驱动的,下一轮不会重报同一批,"
                    f"所以这 {dropped_gone} 条也是永久缺失。")

        if disappeared_total:
            log.info(f"watch: target={wt.target} disappeared={disappeared_total}")

        # 同步 monitors 表状态(如果有对该 target 的监控)
        try:
            for m in Monitor(self.storage).list():
                if m.get("target") == wt.target:
                    # 传 `detected_total`,不是 `new_total`:那一列叫
                    # `last_change_count`,承诺的是**变更**数。而 `new_total`
                    # 数的是「资产表里多出来的行」,只认新增 —— 实测 7 个
                    # 资产消失、0 个新增时,`asset_changes` 里记了 7 条
                    # DISAPPEARED,而那一列写的是 0。字段名承诺的语义和
                    # 承载的对不上就是 bug(决策 #5),r53 之后
                    # `detected_total` 才是真的「本轮检出几条变更」。
                    Monitor(self.storage).record_run(m["id"], detected_total)
        except Exception as e:
            log.debug(f"watch: monitor record_run failed: {e}")

        # 截断留痕要落在**能查的地方**,不只在日志里 —— 日志会被翻过去,
        # 而 `monitor list` 才是用户下次还会看到的地方。
        dropped_this_run = account_change_recording(
            wt, detected_total, recorded_total)
        if dropped_this_run:
            log.warning(
                f"watch: target={wt.target} 本轮共检出 {detected_total} 条变更,"
                f"写进 asset_changes 的只有 {recorded_total} 条,"
                f"**截掉 {dropped_this_run} 条且不会补上**"
                f"(累计已丢 {wt.dropped_change_count} 条)。"
                f"注意上面的 new={new_total} 数的是资产表里多出来的行,"
                f"和这里记了多少条变更**不是一回事**。")

        # 通知
        if self.webhook:
            notify_task_done(
                self.webhook,
                task_id=0,  # watch 不绑定 task
                target=wt.target,
                found=new_total,
                duration_seconds=duration,
            )
            # critical finding 单独通知(只看本次新增的,旧版会拿错行)
            for f in critical_findings:
                notify_critical_finding(self.webhook, f)

        # 外部 callback
        if self.on_run:
            try:
                self.on_run(wt.target, new_total, duration, new_total)
            except Exception as e:
                log.exception(f"watch: on_run callback failed: {e}")

    def _count_assets(self) -> dict[str, int]:
        try:
            return self.storage.get_stats()
        except Exception:
            return {}

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "targets": [wt.to_dict() for wt in self.list()],
        }
