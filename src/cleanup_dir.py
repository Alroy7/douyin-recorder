# -*- coding: utf-8 -*-
"""输出目录清理：空文件夹 / 残留或转换失败的 flv / 不完整 mp4 / 时长过短的 mp4。

设计要点：
1. 只处理「修改时间早于 min_age_seconds」的文件（默认 120 秒），避免动到正在录制/刚落盘的文件；
2. 支持传入 protect（当前正在录制的文件），绝不删除；
3. 源文件（flv/ts/mkv）：
   - 同目录已有有效 .mp4（>0 字节）→ 视为重复残留，直接删源；
   - 没有 .mp4 → 先尝试 remux 补转；转换失败则删除源（转换失败的 flv 不再保留）；
4. mp4：0 字节或 ffmpeg 解析不出时长 → 视为不完整，删除；时长 < min_keep_seconds → 删除；
5. 最后删除清理后变空的文件夹（从深到浅，不动输出根目录）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import ffmpeg_util

LOG = logging.getLogger("dyrec.cleanup")

SOURCE_EXTS = (".flv", ".ts", ".mkv")

#: 进度回调：on_progress(done, total, message)
ProgressCb = Optional[Callable[[int, int, str], None]]


def _emit(cb: ProgressCb, done: int, total: int, message: str) -> None:
    if cb is None:
        return
    try:
        cb(done, total, message)
    except Exception:  # pragma: no cover - 回调异常不致命
        pass


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _age_ok(path: Path, min_age_seconds: float, now: float) -> bool:
    try:
        return (now - path.stat().st_mtime) >= float(min_age_seconds or 0)
    except OSError:
        return False


def _resolve_set(paths: Iterable[Path]) -> Set[str]:
    out: Set[str] = set()
    for p in paths or ():
        try:
            out.add(str(Path(p).resolve()).lower())
        except Exception:
            pass
    return out


def _protected_set(protect: Iterable[Path]) -> Set[str]:
    return _resolve_set(protect)


def _is_protected(path: Path, protected: Set[str]) -> bool:
    if not protected:
        return False
    try:
        return str(path.resolve()).lower() in protected
    except Exception:
        return False


def build_plan(root: Path, ffmpeg_path: str, min_keep_seconds: float = 0,
               *, min_age_seconds: float = 120.0, protect: Iterable[Path] = (),
               on_progress: ProgressCb = None) -> Dict[str, object]:
    """扫描输出目录并生成清理计划（不删除任何文件）。

    返回：
      flv_convert    : [Path] 无有效 mp4 的源文件，需补转（失败删）
      flv_leftover   : [Path] 已有有效 mp4 的重复源文件，直接删
      mp4_incomplete : [Path] 0 字节或解析不出时长的 mp4
      mp4_short      : [Path] 时长 < min_keep_seconds 的 mp4
      scanned        : 扫描到的文件数
    """
    root = Path(root)
    plan: Dict[str, object] = {
        "root": str(root),
        "flv_convert": [],
        "flv_leftover": [],
        "mp4_incomplete": [],
        "mp4_short": [],
        "scanned": 0,
    }
    if not root.exists():
        return plan
    now = time.time()
    protected = _protected_set(protect)
    min_keep = float(min_keep_seconds or 0)

    files: List[Path] = []
    try:
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    files.append(p)
            except OSError:
                continue
    except OSError:
        return plan
    files.sort()
    plan["scanned"] = len(files)
    total = len(files)

    flv_convert: List[Path] = plan["flv_convert"]  # type: ignore[assignment]
    flv_leftover: List[Path] = plan["flv_leftover"]  # type: ignore[assignment]
    mp4_incomplete: List[Path] = plan["mp4_incomplete"]  # type: ignore[assignment]
    mp4_short: List[Path] = plan["mp4_short"]  # type: ignore[assignment]

    for done, p in enumerate(files, start=1):
        suffix = p.suffix.lower()
        _emit(on_progress, done, total, "扫描 %s" % p.name)
        if _is_protected(p, protected) or not _age_ok(p, min_age_seconds, now):
            continue
        if suffix in SOURCE_EXTS:
            mp4 = p.with_suffix(".mp4")
            if _size(mp4) > 0:
                flv_leftover.append(p)       # 已有有效 mp4：重复残留
            else:
                flv_convert.append(p)        # 无 mp4：待补转
        elif suffix == ".mp4":
            size = _size(p)
            if size <= 0:
                mp4_incomplete.append(p)
                continue
            duration = ffmpeg_util.probe_duration(ffmpeg_path, p)
            if duration < 0:
                mp4_incomplete.append(p)
            elif min_keep > 0 and duration < min_keep:
                mp4_short.append(p)
    return plan


def _delete(path: Path, errors: List[str]) -> bool:
    try:
        path.unlink()
        return True
    except OSError as exc:
        errors.append("%s: %s" % (path.name, exc))
        return False


def execute_plan(plan: Dict[str, object], ffmpeg_path: str, *,
                 keep_source: bool = False, delete_empty_dirs: bool = True,
                 on_progress: ProgressCb = None) -> Dict[str, object]:
    """执行 build_plan 生成的计划，返回统计结果。"""
    stats: Dict[str, object] = {
        "root": plan.get("root", ""),
        "flv_converted": 0,
        "flv_deleted": 0,
        "flv_leftover_deleted": 0,
        "mp4_incomplete_deleted": 0,
        "mp4_short_deleted": 0,
        "empty_dirs_deleted": 0,
        "errors": [],
    }
    errors: List[str] = stats["errors"]  # type: ignore[assignment]

    flv_leftover: List[Path] = list(plan.get("flv_leftover") or [])  # type: ignore[arg-type]
    flv_convert: List[Path] = list(plan.get("flv_convert") or [])  # type: ignore[arg-type]
    mp4_incomplete: List[Path] = list(plan.get("mp4_incomplete") or [])  # type: ignore[arg-type]
    mp4_short: List[Path] = list(plan.get("mp4_short") or [])  # type: ignore[arg-type]
    total = len(flv_leftover) + len(flv_convert) + len(mp4_incomplete) + len(mp4_short)
    done = 0

    # 1) 重复残留源文件：直接删
    for p in flv_leftover:
        done += 1
        _emit(on_progress, done, total, "删除重复源文件 %s" % p.name)
        if _delete(p, errors):
            stats["flv_leftover_deleted"] = int(stats["flv_leftover_deleted"]) + 1

    # 2) 无 mp4 的源文件：先补转，成功删源；失败也删源（转换失败的 flv 不保留）
    for p in flv_convert:
        done += 1
        _emit(on_progress, done, total, "补转 %s" % p.name)
        ok, msg = ffmpeg_util.remux_to_mp4(ffmpeg_path, p, timeout=1800)
        if ok:
            stats["flv_converted"] = int(stats["flv_converted"]) + 1
            if not keep_source:
                _delete(p, errors)
        else:
            LOG.warning("补转失败，删除源文件: %s（%s）", p, msg)
            if _delete(p, errors):
                stats["flv_deleted"] = int(stats["flv_deleted"]) + 1

    # 3) 不完整 mp4
    for p in mp4_incomplete:
        done += 1
        _emit(on_progress, done, total, "删除不完整 mp4 %s" % p.name)
        if _delete(p, errors):
            stats["mp4_incomplete_deleted"] = int(stats["mp4_incomplete_deleted"]) + 1

    # 4) 过短 mp4
    for p in mp4_short:
        done += 1
        _emit(on_progress, done, total, "删除过短 mp4 %s" % p.name)
        if _delete(p, errors):
            stats["mp4_short_deleted"] = int(stats["mp4_short_deleted"]) + 1

    # 5) 空文件夹（从深到浅，不动根目录）
    if delete_empty_dirs:
        root = Path(str(plan.get("root") or ""))
        try:
            dirs = [d for d in root.rglob("*") if d.is_dir()]
        except OSError:
            dirs = []
        dirs.sort(key=lambda d: len(d.parts), reverse=True)
        for d in dirs:
            try:
                if any(d.iterdir()):
                    continue
                d.rmdir()
                stats["empty_dirs_deleted"] = int(stats["empty_dirs_deleted"]) + 1
            except OSError:
                continue
    return stats


def cleanup_directory(root: Path, ffmpeg_path: str, min_keep_seconds: float = 0,
                      *, min_age_seconds: float = 120.0,
                      protect: Iterable[Path] = (), keep_source: bool = False,
                      dry_run: bool = False,
                      on_progress: ProgressCb = None) -> Dict[str, object]:
    """扫描并清理输出目录（dry_run=True 只生成计划，不删除）。

    返回统计 dict；dry_run 时返回计划中各分类的数量（键名 plan_*）。
    """
    plan = build_plan(root, ffmpeg_path, min_keep_seconds,
                      min_age_seconds=min_age_seconds, protect=protect,
                      on_progress=on_progress)
    if dry_run:
        return {
            "root": plan.get("root", ""),
            "dry_run": True,
            "plan_flv_convert": len(plan.get("flv_convert") or []),
            "plan_flv_leftover": len(plan.get("flv_leftover") or []),
            "plan_mp4_incomplete": len(plan.get("mp4_incomplete") or []),
            "plan_mp4_short": len(plan.get("mp4_short") or []),
            "plan_scanned": int(plan.get("scanned") or 0),
        }
    result = execute_plan(plan, ffmpeg_path, keep_source=keep_source,
                          on_progress=on_progress)
    result["dry_run"] = False
    return result


def summary_text(stats: Dict[str, object]) -> str:
    """把统计结果格式化成一行中文摘要。"""
    return ("清理完成：删除空文件夹 %d 个；源文件：补转 %d、删除重复 %d、删除失败源 %d；"
            "删除不完整 mp4 %d；删除过短 mp4 %d；错误 %d"
            % (int(stats.get("empty_dirs_deleted") or 0),
               int(stats.get("flv_converted") or 0),
               int(stats.get("flv_leftover_deleted") or 0),
               int(stats.get("flv_deleted") or 0),
               int(stats.get("mp4_incomplete_deleted") or 0),
               int(stats.get("mp4_short_deleted") or 0),
               len(stats.get("errors") or [])))
