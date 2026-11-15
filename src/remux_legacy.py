# -*- coding: utf-8 -*-
"""补转历史录像：把输出目录里残留的 .flv/.ts 转成 .mp4。

用途：旧版本异常退出/强杀后可能留下未转换的源文件；升级后可手动一键补转。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import ffmpeg_util

__all__ = ["find_sources", "convert_sources"]

SOURCE_EXTS = (".flv", ".ts", ".mkv")


def find_sources(root: Path, min_age_seconds: float = 120.0,
                 limit: int = 0) -> List[Path]:
    """扫描输出根目录下需要补转的源文件（同目录已有非空 .mp4 的跳过）。

    min_age_seconds：只处理修改时间早于 N 秒的文件，避免动到正在录制/刚结束的文件。
    """
    root = Path(root)
    if not root.exists():
        return []
    now = time.time()
    out: List[Path] = []
    for p in sorted(root.rglob("*")):
        try:
            if not p.is_file():
                continue
            if p.suffix.lower() not in SOURCE_EXTS:
                continue
            mp4 = p.with_suffix(".mp4")
            if mp4.exists() and mp4.stat().st_size > 0:
                continue
            age = now - p.stat().st_mtime
            if age < float(min_age_seconds):
                continue
            out.append(p)
            if limit and len(out) >= limit:
                break
        except OSError:
            continue
    return out


def convert_sources(files: List[Path], ffmpeg_path: str, keep_source: bool = False,
                    timeout: float = 1800.0,
                    on_progress: Optional[Callable[[int, int, Path, bool, str], None]] = None
                    ) -> Dict[str, object]:
    """逐个把源文件 remux 成 MP4；keep_source=False 时成功后删除源文件。"""
    total = len(files)
    result: Dict[str, object] = {"total": total, "converted": 0, "failed": 0,
                                 "deleted": 0, "errors": []}
    errors: List[str] = result["errors"]  # type: ignore[assignment]
    for index, src in enumerate(files, start=1):
        src = Path(src)
        ok = False
        msg = ""
        try:
            ok, msg = ffmpeg_util.remux_to_mp4(ffmpeg_path, src, timeout=timeout)
            if ok:
                result["converted"] = int(result["converted"]) + 1
                if not keep_source:
                    try:
                        src.unlink()
                        result["deleted"] = int(result["deleted"]) + 1
                    except OSError as exc:
                        msg = "删除源文件失败: %s" % exc
            else:
                result["failed"] = int(result["failed"]) + 1
                errors.append("%s: %s" % (src.name, msg))
        except Exception as exc:  # pragma: no cover - 兜底
            result["failed"] = int(result["failed"]) + 1
            errors.append("%s: %s" % (src.name, exc))
        if on_progress:
            try:
                on_progress(index, total, src, ok, msg)
            except Exception:
                pass
    return result
