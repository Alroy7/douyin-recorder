# -*- coding: utf-8 -*-
"""主播活跃度统计：最近开播时间、多久没开播、从未开播。"""
from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model import WatchItem, render_output_dir

__all__ = ["format_ago", "collect_activity", "write_csv"]


def format_ago(ts: int, now: Optional[float] = None) -> str:
    """时间戳 -> 「刚刚 / 5 分钟前 / 3 小时前 / 2 天前 / 从未」."""
    if not ts:
        return "从未"
    now = time.time() if now is None else float(now)
    delta = max(0.0, now - float(ts))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return "%d 分钟前" % int(delta // 60)
    if delta < 86400:
        return "%d 小时前" % int(delta // 3600)
    days = int(delta // 86400)
    if days < 30:
        return "%d 天前" % days
    if days < 365:
        return "%d 个月前" % int(days // 30)
    return "%d 年前" % int(days // 365)


def _latest_mtime(path: Path, max_depth: int = 3) -> float:
    """目录内文件的最新修改时间（限制深度，避免扫爆大目录）。"""
    best = 0.0
    try:
        root_depth = len(path.parts)
        for dirpath, dirnames, filenames in os.walk(path):
            depth = len(Path(dirpath).parts) - root_depth
            if depth >= max_depth:
                dirnames[:] = []
            for name in filenames:
                try:
                    mtime = os.path.getmtime(os.path.join(dirpath, name))
                    if mtime > best:
                        best = mtime
                except OSError:
                    continue
    except OSError:
        pass
    return best


def _existing_dir_for(item, output_root: Path, template: str) -> Optional[Path]:
    """找到该主播对应的录像目录（兼容 主播/日期、主播、日期/主播 等布局）。"""
    cands = []
    for tmpl in (template or "{anchor}/{date}", "{anchor}"):
        try:
            cands.append(render_output_dir(item, tmpl, Path(output_root)))
        except Exception:
            pass
    for d in cands:
        try:
            if d.exists():
                return d
        except OSError:
            continue
    root = Path(output_root).resolve()
    for d in cands:
        try:
            parent = d.parent
            if parent.exists() and parent.resolve() != root:
                return parent
        except OSError:
            continue
    return None


def collect_activity(watchlist, output_root: Path, template: str = "",
                     backfill: bool = True) -> List[Dict[str, Any]]:
    """收集每个主播的最近开播信息。

    last_live_at 优先用列表里记录的值；为 0 时可从录像目录最新文件时间回填（近似）。
    返回按「最久没开播」排序的列表（从未开播在前）。
    """
    now = time.time()
    rows: List[Dict[str, Any]] = []
    for item in watchlist.all():
        last = int(getattr(item, "last_live_at", 0) or 0)
        source = "recorded" if last else "unknown"
        if not last and backfill:
            try:
                d = _existing_dir_for(item, Path(output_root), template)
                if d is not None and d.exists():
                    mt = _latest_mtime(d)
                    if mt:
                        last = int(mt)
                        source = "files"
            except Exception:
                pass
        days = ((now - last) / 86400.0) if last else None
        rows.append({
            "id": item.id,
            "name": item.display_name,
            "room": item.web_rid or "",
            "group": getattr(item, "group", "") or "",
            "enabled": bool(item.enabled),
            "last_live_at": last,
            "ago": format_ago(last, now),
            "days": days,
            "source": source,
        })
    rows.sort(key=lambda r: (0 if not r["last_live_at"] else 1,
                             r["last_live_at"]))
    return rows


def write_csv(rows: List[Dict[str, Any]], path: str) -> int:
    """导出活跃度 CSV；返回条数。"""
    out = str(path)
    d = os.path.dirname(os.path.abspath(out))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["主播", "房间号", "分组", "最近开播时间", "距今天数", "数据来源"])
        for r in rows:
            ts = r.get("last_live_at") or 0
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
            days = "" if r.get("days") is None else "%.1f" % r["days"]
            source = {"recorded": "列表记录", "files": "录像文件", "unknown": "从未"}.get(
                str(r.get("source")), "")
            writer.writerow([r.get("name", ""), r.get("room", ""), r.get("group", ""),
                             when, days, source])
    return len(rows)
