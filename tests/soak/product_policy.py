# -*- coding: utf-8 -*-
"""产物策略（浸泡测试专用，对应 user 要求）：

每场录制结束 -> remux 成 mp4 -> 用 ffmpeg 校验可读（Duration>0 且有视频流）
-> 校验通过 **立即删除该场所有文件（mp4 + 源 ts/flv）** 节省磁盘；
校验失败则保留到 keep 目录并记录，便于排查。

设计为后台线程池：录制线程的事件回调只做入队，不阻塞录制。
"""
from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from src import ffmpeg_util
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from src import ffmpeg_util

LOG = logging.getLogger("soak.product")

MEDIA_EXTS = (".ts", ".flv", ".mp4", ".mkv")
_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class ProductPolicy:
    """录制产物 remux -> 校验 -> 删除（或保留失败样本）。"""

    def __init__(self, ffmpeg: str, keep_dir: Path, log_path: Path,
                 workers: int = 2, logger: Optional[logging.Logger] = None) -> None:
        self.ffmpeg = ffmpeg
        self.keep_dir = Path(keep_dir)
        self.log_path = Path(log_path)
        self.log = logger or LOG
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._worker, name="product-%d" % i, daemon=True)
            for i in range(max(1, workers))
        ]
        self._stats = {
            "processed": 0, "ok": 0, "fail": 0, "skipped": 0,
            "deleted_files": 0, "deleted_bytes": 0,
            "remux_ok": 0, "remux_fail": 0, "verify_fail": 0,
        }
        self._lock = threading.Lock()
        self._seen = set()
        self.keep_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(self.log_path, "a", encoding="utf-8")
        for t in self._threads:
            t.start()

    # ---------------- 对外接口 ----------------
    def submit(self, path: str) -> None:
        """录制结束后调用（把最后一个文件路径交给后台处理）。"""
        if not path:
            return
        self._queue.put(str(path))

    def queue_size(self) -> int:
        return self._queue.qsize()

    def stats_snapshot(self) -> Dict[str, int]:
        with self._lock:
            data = dict(self._stats)
        data["queue"] = self._queue.qsize()
        return data

    def stop(self, drain_timeout: float = 300.0) -> None:
        """停止工作线程；先尽量把队列里的任务处理完。"""
        self._stop.set()
        deadline = time.time() + max(1.0, drain_timeout)
        for t in self._threads:
            t.join(max(0.0, deadline - time.time()))
        try:
            self._log_file.flush()
            self._log_file.close()
        except Exception:
            pass

    def sweep(self, root: Path, older_than: float = 600.0) -> int:
        """兜底清扫：目录里超过 older_than 秒没变动的媒体文件也走处理流程。"""
        now = time.time()
        count = 0
        root = Path(root)
        if not root.exists():
            return 0
        for path in root.rglob("*"):
            try:
                if not path.is_file() or path.suffix.lower() not in MEDIA_EXTS:
                    continue
                st = path.stat()
            except OSError:
                continue
            if now - st.st_mtime < older_than:
                continue
            key = str(path)
            if key in self._seen:
                continue
            self._seen.add(key)
            self._queue.put(key)
            count += 1
        return count

    # ---------------- 处理流程 ----------------
    def process_sync(self, path: str) -> Dict:
        """同步处理一个录制文件（real 模式直接用）。"""
        return self._process(str(path))

    def _worker(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                path = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._process(path)
            except Exception as exc:
                self.log.exception("处理后异常 %s: %s", path, exc)
            finally:
                self._queue.task_done()

    def _collect_files(self, path: Path) -> List[Path]:
        parent = path.parent
        base = path.stem
        files = []
        if not parent.exists():
            return files
        for cand in parent.iterdir():
            try:
                if not cand.is_file():
                    continue
            except OSError:
                continue
            if cand.suffix.lower() not in MEDIA_EXTS:
                continue
            if cand.stem == base or cand.stem.startswith(base + "_"):
                files.append(cand)
        files.sort()
        return files

    def _process(self, path_str: str) -> Dict:
        path = Path(path_str)
        if not path.exists():
            with self._lock:
                self._stats["skipped"] = self._stats.get("skipped", 0) + 1
            return {"ok": True, "skipped": True,
                    "reason": "文件不存在（可能已被录制器按最短保留时长删除）"}
        files = self._collect_files(path)
        if not files:
            files = [path]
        sources = [f for f in files if f.suffix.lower() in (".ts", ".flv")]
        try:
            total_bytes = sum(f.stat().st_size for f in files)
        except OSError:
            total_bytes = 0
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": [str(f) for f in files],
            "size_mb": round(total_bytes / 1048576.0, 2),
        }
        if not sources:
            mp4s = [f for f in files if f.suffix.lower() == ".mp4"]
            if not mp4s:
                return self._finalize_fail(record, "没有可处理的媒体文件")
            ok, detail = self._verify(mp4s[0])
            record["verify"] = detail
            if ok:
                return self._finalize_ok(record, files)
            return self._finalize_fail(record, "校验失败: " + detail)

        all_ok = True
        details = []
        for src in sources:
            mp4 = src.with_suffix(".mp4")
            if not mp4.exists() or mp4.stat().st_size == 0:
                ok, info = ffmpeg_util.remux_to_mp4(self.ffmpeg, src)
                if not ok:
                    with self._lock:
                        self._stats["remux_fail"] += 1
                    all_ok = False
                    details.append("remux失败: " + info)
                    continue
                with self._lock:
                    self._stats["remux_ok"] += 1
                mp4 = Path(info)
            ok, detail = self._verify(mp4)
            details.append(detail)
            if not ok:
                all_ok = False
        record["verify"] = " | ".join(details)
        if all_ok:
            # 重新收集（remux 后多出 mp4），把 mp4 和源文件一起删掉
            final_files = self._collect_files(path) or files
            return self._finalize_ok(record, final_files)
        return self._finalize_fail(record, "部分/全部校验失败: " + record["verify"])

    def _verify(self, mp4: Path) -> Tuple[bool, str]:
        """用 ffmpeg -i 校验：Duration > 0 且存在视频流。"""
        cmd = [self.ffmpeg, "-hide_banner", "-i", str(mp4)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, "ffmpeg 校验启动失败: %s" % exc
        text = (proc.stderr or b"").decode("utf-8", "replace")
        match = _RE_DURATION.search(text)
        if not match:
            return False, "无法解析时长"
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        has_video = "Stream #" in text and "Video:" in text
        if duration <= 0:
            return False, "Duration=0"
        if not has_video:
            return False, "无视频流"
        return True, "Duration=%.1fs video=yes" % duration

    def _finalize_ok(self, record: Dict, files: List[Path]) -> Dict:
        deleted = 0
        freed = 0
        for f in files:
            try:
                size = f.stat().st_size
                f.unlink()
                deleted += 1
                freed += size
            except OSError as exc:
                self.log.warning("删除失败 %s: %s", f, exc)
        with self._lock:
            self._stats["processed"] += 1
            self._stats["ok"] += 1
            self._stats["deleted_files"] += deleted
            self._stats["deleted_bytes"] += freed
        record.update({"ok": True, "deleted_files": deleted,
                       "deleted_mb": round(freed / 1048576.0, 2)})
        self._write_log(record)
        return record

    def _finalize_fail(self, record: Dict, reason: str) -> Dict:
        record.update({"ok": False, "reason": reason})
        target_dir = self.keep_dir / time.strftime("%Y%m%d-%H%M%S")
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            for f in record.get("files") or []:
                src = Path(f)
                if src.exists():
                    try:
                        shutil.move(str(src), str(target_dir / src.name))
                    except OSError as exc:
                        self.log.warning("保留失败样本失败 %s: %s", src, exc)
        except OSError as exc:
            self.log.warning("创建保留目录失败: %s", exc)
        with self._lock:
            self._stats["processed"] += 1
            self._stats["fail"] += 1
            if "校验" in reason:
                self._stats["verify_fail"] += 1
        record["keep_dir"] = str(target_dir)
        self._write_log(record)
        self.log.warning("产物校验失败已保留: %s %s", target_dir, reason)
        return record

    def _write_log(self, record: Dict) -> None:
        try:
            self._log_file.write(json.dumps(record, ensure_ascii=False) + chr(10))
            self._log_file.flush()
        except Exception:
            pass
