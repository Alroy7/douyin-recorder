# -*- coding: utf-8 -*-
"""运行记录（SQLite）：录制历史 / 运行历史 / 错误记录。

设计目标：
1. 零第三方依赖（标准库 sqlite3）；
2. 所有写入失败只记 warning，绝不影响录制/监控主流程；
3. 线程安全：单连接 + 全局可重入锁（录制线程/监控线程/GUI 线程都会写）；
4. 数据文件默认 <项目根>/data/history.db，可用环境变量 DYREC_HISTORY_DB 覆盖（测试用）。

表结构：
    recordings(id, anchor, room_id, quality, started_at, ended_at, duration_sec,
               size_bytes, path, status, error, deleted)
    runs(id, started_at, ended_at, version, note)
    errors(id, ts, source, message)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("dyrec.history")

#: 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: 测试/多实例时用于覆盖数据库路径的环境变量
ENV_DB_PATH = "DYREC_HISTORY_DB"
#: 默认数据库：<项目根>/data/history.db
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "history.db"

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS recordings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anchor TEXT DEFAULT '',
        room_id TEXT DEFAULT '',
        quality TEXT DEFAULT '',
        started_at REAL DEFAULT 0,
        ended_at REAL DEFAULT 0,
        duration_sec REAL DEFAULT 0,
        size_bytes INTEGER DEFAULT 0,
        path TEXT DEFAULT '',
        status TEXT DEFAULT '',
        error TEXT DEFAULT '',
        deleted INTEGER DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_recordings_started ON recordings(started_at)",
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at REAL DEFAULT 0,
        ended_at REAL DEFAULT 0,
        version TEXT DEFAULT '',
        note TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at)",
    """CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL DEFAULT 0,
        source TEXT DEFAULT '',
        message TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_errors_ts ON errors(ts)",
)

_conns: Dict[str, sqlite3.Connection] = {}
_lock = threading.RLock()


def default_db_path() -> Path:
    """返回当前生效的数据库路径（环境变量优先）。"""
    override = os.environ.get(ENV_DB_PATH, "").strip()
    return Path(override) if override else DEFAULT_DB_PATH


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for sql in _SCHEMA:
        conn.execute(sql)
    conn.commit()


def _get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """获取（并缓存）数据库连接；首次使用时创建目录与表。"""
    path = Path(db_path) if db_path else default_db_path()
    key = str(path.resolve()).lower()
    with _lock:
        conn = _conns.get(key)
        if conn is not None:
            return conn
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:  # pragma: no cover - 某些文件系统不支持 WAL
            pass
        _ensure_schema(conn)
        _conns[key] = conn
        return conn


def init_db(db_path: Optional[Path] = None) -> Path:
    """初始化数据库（建目录/建表），返回数据库路径。"""
    path = Path(db_path) if db_path else default_db_path()
    try:
        _get_conn(path)
    except Exception as exc:
        LOG.warning("初始化历史数据库失败（已忽略）: %s", exc)
    return path


def close_all() -> None:
    """关闭所有缓存的数据库连接（测试清理用）。"""
    with _lock:
        for key, conn in list(_conns.items()):
            try:
                conn.close()
            except Exception:
                pass
            _conns.pop(key, None)


def _safe_write(sql: str, params: tuple, db_path: Optional[Path] = None) -> int:
    """执行写语句，返回 lastrowid / rowcount；失败返回 0（只记 warning）。"""
    try:
        conn = _get_conn(db_path)
        with _lock:
            cursor = conn.execute(sql, params)
            conn.commit()
            return int(cursor.lastrowid or cursor.rowcount or 0)
    except Exception as exc:
        LOG.warning("写入历史记录失败（已忽略）: %s", exc)
        return 0


def _safe_query(sql: str, params: tuple, db_path: Optional[Path] = None) -> List[sqlite3.Row]:
    """执行查询；失败返回空列表（只记 warning）。"""
    try:
        conn = _get_conn(db_path)
        with _lock:
            return list(conn.execute(sql, params).fetchall())
    except Exception as exc:
        LOG.warning("读取历史记录失败（已忽略）: %s", exc)
        return []


def _safe_exec(sql: str, params: tuple, db_path: Optional[Path] = None) -> int:
    """执行 UPDATE/DELETE，返回受影响行数；失败返回 0（只记 warning）。"""
    try:
        conn = _get_conn(db_path)
        with _lock:
            cursor = conn.execute(sql, params)
            conn.commit()
            rowcount = cursor.rowcount
        return int(rowcount) if rowcount is not None and rowcount > 0 else 0
    except Exception as exc:
        LOG.warning("更新历史记录失败（已忽略）: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# 录制记录
# ---------------------------------------------------------------------------
def record_recording_start(anchor: str = "", room_id: str = "", quality: str = "",
                           started_at: Optional[float] = None, path: str = "",
                           db_path: Optional[Path] = None) -> int:
    """记录一次录制开始，返回记录 id（失败返回 0）。"""
    return _safe_write(
        "INSERT INTO recordings (anchor, room_id, quality, started_at, path, status)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (str(anchor or ""), str(room_id or ""), str(quality or ""),
         float(started_at if started_at is not None else time.time()), str(path or ""), "recording"),
        db_path,
    )


def record_recording_end(recording_id: int, ended_at: Optional[float] = None,
                         duration_sec: float = 0, size_bytes: int = 0, path: str = "",
                         status: str = "stopped", error: str = "", deleted: bool = False,
                         db_path: Optional[Path] = None) -> bool:
    """更新一条录制记录为已结束；返回是否更新了记录。"""
    if not recording_id:
        return False
    updated = _safe_exec(
        "UPDATE recordings SET ended_at=?, duration_sec=?, size_bytes=?, path=?,"
        " status=?, error=?, deleted=? WHERE id=?",
        (float(ended_at if ended_at is not None else time.time()),
         float(duration_sec or 0), int(size_bytes or 0), str(path or ""),
         str(status or "stopped"), str(error or ""), 1 if deleted else 0,
         int(recording_id)),
        db_path,
    )
    return updated > 0


# ---------------------------------------------------------------------------
# 运行记录
# ---------------------------------------------------------------------------
def record_run_start(version: str = "", note: str = "",
                     started_at: Optional[float] = None,
                     db_path: Optional[Path] = None) -> int:
    """记录一次程序运行开始，返回记录 id（失败返回 0）。"""
    return _safe_write(
        "INSERT INTO runs (started_at, version, note) VALUES (?, ?, ?)",
        (float(started_at if started_at is not None else time.time()),
         str(version or ""), str(note or "")[:500]),
        db_path,
    )


def record_run_end(run_id: int, ended_at: Optional[float] = None,
                   note: Optional[str] = None, db_path: Optional[Path] = None) -> bool:
    """更新一条运行记录为已结束（note 为 None 时保留原值）。"""
    if not run_id:
        return False
    updated = _safe_exec(
        "UPDATE runs SET ended_at=?, note=COALESCE(?, note) WHERE id=?",
        (float(ended_at if ended_at is not None else time.time()),
         None if note is None else str(note)[:500], int(run_id)),
        db_path,
    )
    return updated > 0


# ---------------------------------------------------------------------------
# 错误记录
# ---------------------------------------------------------------------------
def record_error(source: str, message: str, ts: Optional[float] = None,
                 db_path: Optional[Path] = None) -> int:
    """记录一条错误/异常（source 标注来源模块），返回记录 id（失败返回 0）。"""
    text = str(message or "").strip()
    if not text:
        return 0
    return _safe_write(
        "INSERT INTO errors (ts, source, message) VALUES (?, ?, ?)",
        (float(ts if ts is not None else time.time()), str(source or ""), text[:2000]),
        db_path,
    )


# ---------------------------------------------------------------------------
# 查询 / 统计 / 清理
# ---------------------------------------------------------------------------
def _rows_to_dicts(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if "deleted" in item:
            item["deleted"] = bool(item["deleted"])
        result.append(item)
    return result


def list_recent_recordings(days: int = 7, limit: int = 500,
                           db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """返回最近 days 天（按开始时间倒序）的录制记录；days<=0 表示不限时间。"""
    limit = max(1, int(limit or 500))
    if days and days > 0:
        cutoff = time.time() - float(days) * 86400
        rows = _safe_query(
            "SELECT * FROM recordings WHERE started_at >= ?"
            " ORDER BY started_at DESC, id DESC LIMIT ?",
            (cutoff, limit), db_path,
        )
    else:
        rows = _safe_query(
            "SELECT * FROM recordings ORDER BY started_at DESC, id DESC LIMIT ?",
            (limit,), db_path,
        )
    return _rows_to_dicts(rows)


def list_recent_errors(days: int = 7, limit: int = 100,
                       db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """返回最近 days 天的错误记录（按时间倒序）。"""
    limit = max(1, int(limit or 100))
    cutoff = time.time() - float(max(1, days)) * 86400
    rows = _safe_query(
        "SELECT * FROM errors WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT ?",
        (cutoff, limit), db_path,
    )
    return _rows_to_dicts(rows)


def _aggregate(since: float, db_path: Optional[Path]) -> Dict[str, Any]:
    rows = _safe_query(
        "SELECT COUNT(*) AS count,"
        " COALESCE(SUM(duration_sec), 0) AS duration_sec,"
        " COALESCE(SUM(size_bytes), 0) AS size_bytes,"
        " COALESCE(SUM(CASE WHEN deleted = 1 THEN 1 ELSE 0 END), 0) AS deleted,"
        " COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0) AS failed"
        " FROM recordings WHERE started_at >= ?",
        (since,), db_path,
    )
    if not rows:
        return {"count": 0, "duration_sec": 0.0, "size_bytes": 0,
                "deleted": 0, "failed": 0}
    item = dict(rows[0])
    return {
        "count": int(item.get("count") or 0),
        "duration_sec": float(item.get("duration_sec") or 0),
        "size_bytes": int(item.get("size_bytes") or 0),
        "deleted": int(item.get("deleted") or 0),
        "failed": int(item.get("failed") or 0),
    }


def stats(days: int = 7, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """录制统计：今日 / 最近 days 天（默认 7 天）的录制数、总时长、总大小、删除数、失败数。"""
    days = max(1, int(days or 7))
    now = time.time()
    local_now = time.localtime(now)
    today_start = time.mktime((local_now.tm_year, local_now.tm_mon, local_now.tm_mday,
                               0, 0, 0, 0, 0, -1))
    week_start = now - float(days) * 86400
    return {
        "generated_at": now,
        "days": days,
        "today": _aggregate(today_start, db_path),
        "week": _aggregate(week_start, db_path),
    }


def cleanup(days: int = 90, db_path: Optional[Path] = None) -> Dict[str, int]:
    """删除超过 days 天的历史记录（recordings/runs/errors），返回各表删除数量。

    days<=0 时不删除任何数据。只清理数据库记录，不动磁盘上的录像/日志文件。
    """
    result = {"recordings": 0, "runs": 0, "errors": 0}
    if not days or days <= 0:
        return result
    cutoff = time.time() - float(days) * 86400
    try:
        conn = _get_conn(db_path)
        with _lock:
            for table, column in (("recordings", "started_at"), ("runs", "started_at"),
                                  ("errors", "ts")):
                cursor = conn.execute(
                    "DELETE FROM %s WHERE %s < ?" % (table, column), (cutoff,)
                )
                result[table] = int(cursor.rowcount or 0)
            conn.commit()
    except Exception as exc:
        LOG.warning("清理历史记录失败（已忽略）: %s", exc)
    return result
