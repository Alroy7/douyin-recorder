# -*- coding: utf-8 -*-
"""单实例锁：防止重复启动导致同一批主播被两个进程同时录制。

Windows 用 msvcrt.locking 对锁文件首字节加独占锁；进程退出时操作系统自动释放，
因此崩溃/被杀不会留下死锁（文件里残留的 PID 只用于提示）。
"""
from __future__ import annotations

import atexit
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

__all__ = ["InstanceGuard", "acquire_long_running_guard"]


class InstanceGuard:
    """基于文件锁的单实例守卫。"""

    def __init__(self, path: Path, label: str = "抖音直播录制工具"):
        self.path = Path(path)
        # 锁文件的被锁区域（首字节）对其他进程不可读，PID 单独写到旁路文件用于提示
        self.pid_path = self.path.with_name(self.path.name + ".pid")
        self.label = label
        self._fd: Optional[int] = None
        self._file = None

    # ---------------- 内部 ----------------
    def _read_pid(self) -> str:
        try:
            text = self.pid_path.read_text(encoding="utf-8", errors="ignore").strip()
            return text.splitlines()[0] if text else ""
        except OSError:
            return ""

    def _write_pid(self) -> None:
        try:
            self.pid_path.write_text(
                "%d\n%s\n" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")),
                encoding="utf-8")
        except OSError:
            pass

    # ---------------- 对外 ----------------
    def acquire(self) -> Tuple[bool, str]:
        """尝试获取锁。返回 (ok, 提示)。已持有则直接返回成功。"""
        if self._fd is not None:
            return True, "已持有实例锁"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
            self._fd = fd
            self._file = os.fdopen(fd, "r+", encoding="utf-8", errors="ignore")
        except OSError as exc:
            self._fd = None
            self._file = None
            return False, "无法创建实例锁文件：%s" % exc
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - 非 Windows 兜底
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pid = self._read_pid()
            self.release()
            return False, "%s 已在运行（PID %s）。请勿重复启动，否则同一主播会被重复录制。" % (
                self.label, pid or "?")
        self._write_pid()
        try:
            atexit.register(self.release)
        except Exception:
            pass
        return True, "实例锁获取成功"

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    self._file.seek(0)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:  # pragma: no cover
                try:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                if self._file is not None:
                    self._file.close()
            except OSError:
                pass
            self._fd = None
            self._file = None
            try:
                self.pid_path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "InstanceGuard":
        ok, msg = self.acquire()
        if not ok:
            raise RuntimeError(msg)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_long_running_guard(data_dir: Path, label: str = "抖音直播录制工具") -> Tuple[Optional[InstanceGuard], str]:
    """便捷函数：data_dir/app.lock。返回 (guard, 提示)；失败时 guard 为 None。"""
    guard = InstanceGuard(Path(data_dir) / "app.lock", label)
    ok, msg = guard.acquire()
    return (guard if ok else None), msg
