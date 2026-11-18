# -*- coding: utf-8 -*-
"""磁盘空间检查与按保留策略清理旧录像。

保留策略：
- keep_days > 0：删除最后修改时间早于 N 天的录像文件；
- keep_max_files > 0：同一输出目录下最多保留 N 个录像文件（按修改时间从旧到新删）。

只清理「录像扩展名」的文件（flv/mp4/ts/mkv），不会误删其他文件。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

LOG = logging.getLogger("dyrec.disk")

RECORD_EXTS = (".flv", ".mp4", ".ts", ".mkv", ".m4a", ".aac")


def free_bytes(path: Path) -> int:
    """返回 path 所在磁盘可用字节数；路径不存在时向上找最近存在的父目录。"""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).free
    except OSError:
        return -1


def free_gb(path: Path) -> float:
    b = free_bytes(path)
    return round(b / (1024 ** 3), 2) if b >= 0 else -1.0


def total_bytes(path: Path) -> int:
    """返回 path 所在磁盘总容量字节数；失败返回 -1。"""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).total
    except OSError:
        return -1


def total_gb(path: Path) -> float:
    b = total_bytes(path)
    return round(b / (1024 ** 3), 2) if b >= 0 else -1.0


def has_enough_space(path: Path, min_free_gb: float) -> bool:
    if min_free_gb <= 0:
        return True
    gb = free_gb(path)
    return gb < 0 or gb >= min_free_gb


def _iter_record_files(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    result: List[Path] = []
    try:
        for entry in directory.iterdir():
            try:
                if entry.is_file() and entry.suffix.lower() in RECORD_EXTS:
                    result.append(entry)
            except OSError:
                continue
    except OSError:
        return []
    return result


def cleanup_policy(root: Path, keep_days: int = 0, keep_max_files: int = 0,
                   protect: Optional[Iterable[Path]] = None,
                   logger: logging.Logger = LOG) -> Tuple[int, int]:
    """按保留策略清理 root 目录（递归）。返回 (删除文件数, 释放字节数)。

    protect：正在录制的文件，绝不删除。
    """
    root = Path(root)
    if not root.exists():
        return 0, 0
    protected = {str(Path(p).resolve()).lower() for p in (protect or [])}
    now = time.time()
    deleted = 0
    freed = 0
    remove_set: List[Path] = []

    for f in _iter_record_files(root):
        try:
            if str(f.resolve()).lower() in protected:
                continue
            mtime = f.stat().st_mtime
            size = f.stat().st_size
        except OSError:
            continue
        if keep_days > 0 and (now - mtime) > keep_days * 86400:
            remove_set.append(f)
            freed += size

    # 按目录分组做 max_files 裁剪
    if keep_max_files > 0:
        by_dir = {}
        for f in _iter_record_files(root):
            try:
                if str(f.resolve()).lower() in protected:
                    continue
                by_dir.setdefault(f.parent, []).append(f)
            except OSError:
                continue
        for _d, files in by_dir.items():
            files.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
            for old in files[keep_max_files:]:
                if old not in remove_set:
                    remove_set.append(old)
                    try:
                        freed += old.stat().st_size
                    except OSError:
                        pass

    for f in remove_set:
        try:
            f.unlink()
            deleted += 1
            logger.info("清理旧录像: %s", f)
        except OSError as exc:
            logger.warning("清理失败 %s: %s", f, exc)
    if deleted:
        # 清理空目录（仅删空目录，不动根）
        for d in sorted({f.parent for f in remove_set}, key=lambda p: len(p.parts), reverse=True):
            try:
                if d != root and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
    return deleted, freed


def human_size(num: int) -> str:
    """字节数转易读文本。"""
    try:
        n = float(num)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TB"


def human_duration(seconds: int) -> str:
    """秒数转 HH:MM:SS。"""
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"
