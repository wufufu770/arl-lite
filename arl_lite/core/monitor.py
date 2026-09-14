"""arl_lite.core.monitor

资产监控:
- Monitor CRUD(monitors 表)
- 跑一次 monitor:对比上次结果,产生 change events
- 列出某个 monitor 的 changes

设计:
- monitors 表存监控任务配置
- asset_changes 表存变更事件
- diff 算法:用 last_run_at + first_seen 判断 NEW_ASSET
- DISAPPEARED:这次没出现的 hash(需要 snapshot)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

log = logging.getLogger("arl_lite.core.monitor")


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


def record_change(storage, asset_type: str, change_type: str,
                  asset_hash: str, before: dict | None = None,
                  after: dict | None = None, task_id: int | None = None) -> None:
    """记录一个变更事件到 asset_changes 表"""
    diff = None
    if before and after:
        # 字段级 diff
        diff = {}
        keys = set(before.keys()) | set(after.keys())
        for k in keys:
            bv = before.get(k)
            av = after.get(k)
            if bv != av:
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
