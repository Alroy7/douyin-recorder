# -*- coding: utf-8 -*-
"""日志初始化：控制台 + 按天日志文件（logs/run-YYYY-MM-DD.log）+ 旧版 dyrec 日志。

- 按天日志：logs/run-YYYY-MM-DD.log，跨天自动切换新文件（便于「连续多天日志」分析）；
- 兼容旧行为：同时保留 logs/dyrec-YYYYMMDD.log（按大小轮转），老用户/脚本不受影响；
- 启动时按 log_keep_days 清理过期日志，只删 run-*.log 与 dyrec-*.log，不动用户其它文件；
- Windows 控制台默认 GBK，这里统一把 stdout/stderr 重配置为 UTF-8。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

LOGGER_NAME = "dyrec"
_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

#: 按天日志文件名：run-YYYY-MM-DD.log
_DAILY_LOG_RE = re.compile(r"^run-(\d{4}-\d{2}-\d{2})\.log$")
#: 旧版日志文件名：dyrec-YYYYMMDD.log（含轮转备份 .1）
_APP_LOG_RE = re.compile(r"^dyrec-(\d{8})\.log(?:\.\d+)?$")


def daily_log_path(log_dir: Path, date: Optional[str] = None) -> Path:
    """返回某天的按天日志路径（默认今天）。"""
    day = date or time.strftime("%Y-%m-%d")
    return Path(log_dir) / f"run-{day}.log"


class DailyFileHandler(logging.FileHandler):
    """按天切换文件的日志 handler：写入 logs/run-YYYY-MM-DD.log。

    跨天时（进程跨午夜运行）自动关闭旧文件、打开新一天的文件。
    """

    def __init__(self, log_dir: Path, prefix: str = "run", encoding: str = "utf-8"):
        self.log_dir = Path(log_dir)
        self.prefix = prefix
        self._date = time.strftime("%Y-%m-%d")
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        super().__init__(str(self._path_for(self._date)), mode="a",
                         encoding=encoding, delay=False)

    def _path_for(self, date: str) -> Path:
        return self.log_dir / f"{self.prefix}-{date}.log"

    def _rotate_if_needed(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today == self._date:
            return
        with self.lock:
            if today == self._date:
                return
            try:
                if self.stream is not None:
                    self.stream.close()
            except Exception:  # pragma: no cover - 关闭旧文件失败不致命
                pass
            self._date = today
            self.baseFilename = os.path.abspath(str(self._path_for(today)))
            self.stream = self._open()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._rotate_if_needed()
            super().emit(record)
        except Exception:  # pragma: no cover - 日志 handler 不允许抛异常
            self.handleError(record)


class _SafeStreamHandler(logging.StreamHandler):
    """控制台 handler：编码失败时用 replace，避免日志把程序打挂。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except UnicodeEncodeError:
            try:
                msg = self.format(record)
                stream = self.stream
                stream.write(msg.encode("utf-8", "replace").decode("utf-8", "replace") + self.terminator)
                self.flush()
            except Exception:  # pragma: no cover - 日志不能再抛异常
                pass
        except Exception:  # pragma: no cover
            pass


def setup_console() -> None:
    """把标准输出/错误切成 UTF-8，避免 Windows 控制台中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover
            pass


def _parse_log_date(name: str) -> str:
    """从日志文件名解析日期（YYYY-MM-DD），无法识别返回空字符串。"""
    match = _DAILY_LOG_RE.match(name)
    if match:
        return match.group(1)
    match = _APP_LOG_RE.match(name)
    if match:
        digits = match.group(1)
        return "%s-%s-%s" % (digits[0:4], digits[4:6], digits[6:8])
    return ""


def cleanup_logs(log_dir: Path, keep_days: int,
                 logger: Optional[logging.Logger] = None) -> int:
    """删除超过 keep_days 天的日志文件，返回删除数量。

    只清理本工具自己的日志（run-*.log、dyrec-*.log），不会碰用户的其它文件。
    keep_days <= 0 表示不清理。
    """
    if keep_days <= 0:
        return 0
    directory = Path(log_dir)
    if not directory.is_dir():
        return 0
    cutoff = time.time() - float(keep_days) * 86400
    deleted = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        date_text = _parse_log_date(entry.name)
        if not date_text:
            continue
        try:
            stamp = time.mktime(time.strptime(date_text, "%Y-%m-%d"))
        except ValueError:
            continue
        if stamp >= cutoff:
            continue
        try:
            entry.unlink()
            deleted += 1
        except OSError:
            continue
    if deleted and logger is not None:
        logger.info("清理过期日志 %d 个（保留 %d 天）", deleted, keep_days)
    return deleted


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True,
                  filename: Optional[str] = None, daily: bool = True,
                  keep_days: int = 0) -> logging.Logger:
    """初始化根 logger（幂等，可重复调用）。返回 'dyrec' logger。

    - daily=True 时额外写 logs/run-YYYY-MM-DD.log（按天，便于多天日志分析）；
    - filename 非空时仍写旧版命名日志（默认 dyrec-YYYYMMDD.log，按大小轮转）；
    - keep_days>0 时启动清理过期日志。
    """
    setup_console()
    log_dir = Path(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # 先清理过期日志，再打开新文件（不会误删当前正在写的日志）
    if keep_days > 0:
        try:
            cleanup_logs(log_dir, keep_days)
        except Exception:  # pragma: no cover - 清理失败不影响启动
            pass

    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False

    # 重复调用时先移除旧 handler，避免日志重复
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(_DEFAULT_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    if console:
        ch = _SafeStreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        root.addHandler(ch)

    name = filename or ("dyrec-" + time.strftime("%Y%m%d") + ".log")
    try:
        fh = logging.handlers.RotatingFileHandler(
            str(log_dir / name), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except OSError as exc:  # pragma: no cover
        if console:
            root.warning("无法创建日志文件: %s", exc)

    if daily:
        try:
            dh = DailyFileHandler(log_dir, prefix="run")
            dh.setFormatter(formatter)
            root.addHandler(dh)
        except OSError as exc:  # pragma: no cover
            if console:
                root.warning("无法创建按天日志文件: %s", exc)

    return root


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
