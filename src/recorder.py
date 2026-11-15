# -*- coding: utf-8 -*-
"""单房间录制引擎：ffmpeg 子进程 + 断流重连 + 分段 + 磁盘守护 + 事件回调。

核心思路
--------
直播流不可靠（HTTP-FLV 随时断），所以录制不是一次 ffmpeg 跑到底，而是循环：

    while 未停止:
        spawn ffmpeg -i <url> -c copy <文件>
        监控子进程 / 磁盘 / 主播是否下播 / 录制时长上限
        ffmpeg 退出后判断是「正常下播」「用户停止」还是「断流」
        断流 -> 递增退避后重连（每次重连写新文件，避免覆盖已有录像）

- 直录 FLV：-c copy 不转码，CPU 占用极低；
- 分段：-f segment -segment_time N，文件名带递增序号；
- 收尾：可选 remux 成 MP4（-c copy），多段时用 concat demuxer 合并；
- 事件：开始/进行中/结束/错误/重连/分段，通过回调交给 GUI/日志。

流地址可以通过「字符串」或「回调函数」提供：回调模式在每次重连前重新调用，
正好适配抖音带签名、有时效的拉流地址（每次重连必须重新取地址）。

本模块不依赖 dy_api，因此可以用本地测试流独立验证（见 tests/test_recorder.py）。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

try:  # 支持包内导入（python run.py）与脚本直接运行（python src/recorder.py）
    from . import disk as diskmod
    from . import ffmpeg_util
    from . import history as historymod
    from .model import EventType, RecordEvent
except ImportError:  # pragma: no cover - 直接运行脚本时
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import disk as diskmod  # type: ignore
    import ffmpeg_util  # type: ignore
    import history as historymod  # type: ignore
    from model import EventType, RecordEvent  # type: ignore

LOG = logging.getLogger("dyrec.recorder")

#: 录制中间格式固定 FLV（http-flv 直录最稳）
RECORD_EXT = ".flv"
#: 判定「本次尝试确有产出」的最小字节数，低于此值视为快速失败
MIN_USEFUL_BYTES = 64 * 1024

#: 默认「最短保留时长」（秒），0 = 关闭。
#: 由 config.load_config() 按配置注入；创建 RoomRecorder 时也可用 min_keep_seconds 显式覆盖。
DEFAULT_MIN_KEEP_SECONDS = 0


def set_default_min_keep_seconds(seconds: int) -> None:
    """设置全局默认最短保留时长（0=关闭）。供 config.load_config() 注入配置值。"""
    global DEFAULT_MIN_KEEP_SECONDS
    try:
        DEFAULT_MIN_KEEP_SECONDS = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        DEFAULT_MIN_KEEP_SECONDS = 0


class RoomRecorder:
    """一个房间的一次录制会话（内部自带线程与 ffmpeg 子进程）。"""

    def __init__(
        self,
        ffmpeg_path: str,
        stream_url: Union[str, Callable[[], Optional[str]]],
        output_dir: Union[str, Path],
        filename_base: str,
        *,
        name: str = "",
        room_id: str = "",
        container: str = "flv",
        remux_mp4: bool = True,
        keep_source: bool = False,
        min_keep_seconds: Optional[int] = None,
        quality: str = "",
        segment_duration: int = 0,
        segment_size_mb: int = 0,
        retry_max: int = 5,
        retry_interval: int = 5,
        retry_interval_max: int = 120,
        stall_check_seconds: int = 30,
        stall_kill_after: int = 2,
        min_free_gb: float = 2.0,
        graceful_stop_timeout: float = 20,
        record_timeout_min: int = 0,
        extra_args: Optional[List[str]] = None,
        extra_input_args: Optional[List[str]] = None,
        http_input_args: Optional[List[str]] = None,
        is_live_fn: Optional[Callable[[], Optional[bool]]] = None,
        on_event: Optional[Callable[[RecordEvent], None]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.ffmpeg_path = str(ffmpeg_path or "")
        self._stream_url = stream_url
        self.output_dir = Path(output_dir)
        # 文件名里不允许 % 和单引号（segment 占位符 / concat 转义问题）
        self.filename_base = (filename_base or "record").replace("%", "_").replace("'", "_")
        self.name = name or self.filename_base
        self.room_id = room_id or self.filename_base
        self.container = (container or "flv").lower()
        if self.container not in ("flv", "ts"):
            self.container = "flv"
        self.ext = "." + self.container
        self.remux_mp4 = bool(remux_mp4)
        self.keep_source = bool(keep_source)
        # None = 跟随全局默认（config 注入）；显式传 0 = 本次关闭
        self.min_keep_seconds = (
            DEFAULT_MIN_KEEP_SECONDS if min_keep_seconds is None
            else max(0, int(min_keep_seconds))
        )
        self.quality = str(quality or "")
        self.segment_duration = max(0, int(segment_duration or 0))
        self.segment_size_mb = max(0, int(segment_size_mb or 0))
        self.retry_max = max(0, int(retry_max))
        self.retry_interval = max(1, int(retry_interval))
        self.retry_interval_max = max(self.retry_interval, int(retry_interval_max))
        self.stall_check_seconds = max(0, int(stall_check_seconds or 0))
        self.stall_kill_after = max(1, int(stall_kill_after or 2))
        self.min_free_gb = float(min_free_gb or 0)
        self.graceful_stop_timeout = float(graceful_stop_timeout or 20)
        self.record_timeout_min = max(0, int(record_timeout_min or 0))
        self.extra_args = list(extra_args or [])
        self.extra_input_args = list(extra_input_args or [])
        # None 表示使用内置 HTTP 参数模板；[] 表示完全不添加网络参数（测试用）
        self.http_input_args = http_input_args
        self.is_live_fn = is_live_fn
        self.on_event = on_event
        self.log = logger or LOG

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_tail = ""
        self._started_at = 0.0
        self._ended_at = 0.0
        self._state = "idle"          # idle/recording/reconnecting/stopping/stopped/error
        self._error = ""
        self._stop_reason = ""
        self._files: List[Path] = []
        self._current_file = ""
        self._reconnects = 0
        self._attempt = 0                 # 本次会话内 ffmpeg 启动次数
        self._consecutive_failures = 0    # 连续无产出失败次数（用于退避/收尾）
        self._produced_bytes = 0
        self._last_error_detail = ""
        self._offline_flag: Optional[bool] = None
        self._history_id = 0          # 历史库里的本次录制记录 id（0=未写入）
        self._deleted_short = False   # 是否因「时长过短」删除了本场产物

    # ==================================================================
    # 公共 API
    # ==================================================================
    def start(self) -> bool:
        """启动录制线程。已在运行则返回 False。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._started_at = time.time()
            self._ended_at = 0.0
            self._state = "recording"
            self._error = ""
            self._stop_reason = ""
            self._files = []
            self._current_file = ""
            self._reconnects = 0
            self._attempt = 0
            self._consecutive_failures = 0
            self._produced_bytes = 0
            self._deleted_short = False
            self._history_id = 0
            try:
                self.output_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._error = f"无法创建输出目录 {self.output_dir}: {exc}"
                self.log.error(self._error)
                self._emit(EventType.ERROR, self._error)
                return False
            self._thread = threading.Thread(
                target=self._run, name=f"recorder-{self.name}", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, reason: str = "手动停止") -> None:
        """请求停止：置停止标志并立即中断 ffmpeg，收尾由录制线程完成。"""
        with self._lock:
            if not self._stop_reason:
                self._stop_reason = reason or "手动停止"
            self._state = "stopping"
        self._stop_event.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            ffmpeg_util.terminate_process(proc, wait=min(5.0, self.graceful_stop_timeout))

    def join(self, timeout: Optional[float] = None) -> bool:
        """等待录制线程结束，返回线程是否已结束。"""
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()

    def is_alive(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def status(self) -> Dict:
        """运行时状态快照（供 GUI 显示）。"""
        with self._lock:
            files = list(self._files)
            state = self._state
            err = self._error
            reason = self._stop_reason
            reconnects = self._reconnects
            current = self._current_file
        size = self._total_size(files)
        now = time.time()
        start = self._started_at or now
        end = self._ended_at or now
        return {
            "state": state,
            "recording": state in ("recording", "reconnecting", "stopping"),
            "deleted": self._deleted_short,
            "error": err,
            "stop_reason": reason,
            "duration": int(max(0, end - start)),
            "file_size": size,
            "file_count": len(files),
            "current_file": current,
            "files": [str(f) for f in files],
            "reconnects": reconnects,
            "name": self.name,
            "room_id": self.room_id,
        }

    # ==================================================================
    # 内部：主循环
    # ==================================================================
    def _run(self) -> None:
        stop_reason = ""
        error = ""
        try:
            self._history_begin()
            while not self._stop_event.is_set():
                # --- 每次尝试前的守护检查 ---
                if not diskmod.has_enough_space(self.output_dir, self.min_free_gb):
                    gb = diskmod.free_gb(self.output_dir)
                    error = f"磁盘剩余空间不足（{gb} GB < {self.min_free_gb} GB），已停止录制"
                    self._emit(EventType.DISK, error)
                    stop_reason = "磁盘空间不足"
                    break
                if self.record_timeout_min > 0:
                    if (time.time() - self._started_at) >= self.record_timeout_min * 60:
                        stop_reason = f"达到单次录制时长上限（{self.record_timeout_min} 分钟）"
                        break

                url = self._get_stream_url()
                if not url:
                    error = "未获取到可录制的流地址"
                    self._emit(EventType.ERROR, error)
                    stop_reason = "无可用流"
                    break

                attempt_no = self._attempt
                target = self._output_target(attempt_no)
                with self._lock:
                    self._attempt += 1
                    self._current_file = target.replace("%03d", "000")
                cmd = ffmpeg_util.build_ffmpeg_cmd(
                    self.ffmpeg_path,
                    url,
                    target,
                    segment_duration=self.segment_duration,
                    container=self.container,
                    extra_args=self.extra_args,
                    extra_input_args=self.extra_input_args,
                    http_input_args=self.http_input_args,
                )
                self.log.info("启动 ffmpeg（第 %d 次）: %s", attempt_no + 1, _short_cmd(cmd))

                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        creationflags=ffmpeg_util.CREATE_NO_WINDOW,
                    )
                except (OSError, ValueError) as exc:
                    error = f"启动 ffmpeg 失败: {exc}"
                    self._emit(EventType.ERROR, error)
                    stop_reason = "ffmpeg 启动失败"
                    break

                with self._lock:
                    self._proc = proc
                    self._state = "recording"
                reader = threading.Thread(target=self._collect_stderr, args=(proc,), daemon=True)
                reader.start()

                display_path = target.replace("%03d", "000")
                if attempt_no == 0:
                    self._emit(EventType.START, "开始录制", path=display_path)
                else:
                    self._emit(EventType.RECONNECT, f"第 {attempt_no} 次重连", path=display_path)

                before_bytes = self._total_size()
                result = self._monitor(proc)
                self.log.debug("监控结束: %s", result)

                if result == "stop":
                    ffmpeg_util.terminate_process(proc, wait=5.0)
                    stop_reason = self._stop_reason or "手动停止"
                    break
                if result == "disk":
                    ffmpeg_util.terminate_process(proc, wait=5.0)
                    gb = diskmod.free_gb(self.output_dir)
                    error = f"磁盘剩余空间不足（{gb} GB < {self.min_free_gb} GB），已停止录制"
                    self._emit(EventType.DISK, error)
                    stop_reason = "磁盘空间不足"
                    break
                if result == "timeout":
                    ffmpeg_util.terminate_process(proc, wait=5.0)
                    stop_reason = f"达到单次录制时长上限（{self.record_timeout_min} 分钟）"
                    break
                if result == "offline":
                    # 直播已结束：让 ffmpeg 自然收尾（少量缓冲仍在写），但限时
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        ffmpeg_util.terminate_process(proc, wait=5.0)
                    stop_reason = "直播已结束（下播）"
                    break

                # 停止标志可能刚被置位
                if self._stop_event.is_set():
                    ffmpeg_util.terminate_process(proc, wait=5.0)
                    stop_reason = self._stop_reason or "手动停止"
                    break

                # ffmpeg 自行退出：区分「下播」与「断流」
                # _check_offline() 返回「是否正在直播」：True=直播中，False=已下播，None=未知。
                # 注意：这里曾误写为 is True（把「直播中」当成下播），导致断流被误判为下播。
                live = self._check_offline()
                grew = self._total_size() - before_bytes
                if live is False:
                    stop_reason = "直播已结束（下播）"
                    break
                if grew >= MIN_USEFUL_BYTES:
                    # 本次确有产出，视为正常断流，重置连续失败计数
                    self._produced_bytes += grew
                    with self._lock:
                        self._consecutive_failures = 0
                else:
                    with self._lock:
                        self._consecutive_failures += 1
                    if self._consecutive_failures > self.retry_max:
                        error = self._last_error_detail or "断流重连次数已达上限"
                        self._emit(EventType.ERROR, f"连续失败 {self._consecutive_failures} 次，停止重连")
                        stop_reason = "连续失败次数超限"
                        break
                # 递增退避
                delay = self._backoff_delay()
                with self._lock:
                    self._reconnects += 1
                self._emit(
                    EventType.RECONNECT,
                    f"断流，{delay} 秒后重连（连续失败 {self._consecutive_failures} 次）",
                )
                if self._stop_event.wait(delay):
                    stop_reason = self._stop_reason or "手动停止"
                    break

        except Exception as exc:  # pragma: no cover - 顶层兜底
            self.log.exception("录制线程异常")
            error = f"录制线程异常: {exc}"
            self._emit(EventType.ERROR, error)
            stop_reason = "内部错误"
        finally:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                ffmpeg_util.terminate_process(proc, wait=self.graceful_stop_timeout)
            with self._lock:
                self._proc = None
                self._ended_at = time.time()
                if error:
                    self._error = error
                if stop_reason:
                    self._stop_reason = stop_reason
                self._state = "error" if error else "stopped"
            # 收尾：remux / 过短自动删除 / 事件 / 运行记录
            final_files = self._finalize()
            final_files, deleted, media_duration = self._delete_if_too_short(final_files)
            if deleted:
                duration_text = _fmt_seconds(media_duration)
                # INFO 事件：GUI/日志面板能直接看到「过短录制已删除」
                self._emit(
                    EventType.INFO,
                    "过短录制已删除（%ss < %ss）" % (duration_text, self.min_keep_seconds),
                )
            with self._lock:
                # 最终文件列表以收尾后的为准（remux 后 FLV 已删除、MP4 已生成/被删）
                self._files = list(final_files)
                self._deleted_short = deleted
            st = self.status()
            if deleted:
                summary = (
                    f"录制结束：{stop_reason or '结束'}，时长 {diskmod.human_duration(int(media_duration))} "
                    f"低于最短保留时长 {self.min_keep_seconds} 秒，已自动删除本场产物"
                )
            else:
                summary = (
                    f"录制结束：{stop_reason or '结束'}，共 {len(final_files)} 个文件，"
                    f"{diskmod.human_size(st['file_size'])}，时长 {diskmod.human_duration(st['duration'])}"
                )
            if error:
                self.log.error(summary)
            else:
                self.log.info(summary)
            self._emit(
                EventType.STOPPED,
                summary,
                path=str(final_files[-1]) if final_files else "",
                size=st["file_size"],
                duration=st["duration"],
                deleted=deleted,
            )
            self._history_end(final_files, media_duration, deleted)

    def _monitor(self, proc: subprocess.Popen) -> str:

        last_progress = 0.0
        last_disk_check = time.time()
        last_live_check = time.time()
        last_file_refresh = 0.0
        # 卡死看门狗：按 stall_check_seconds 对比录制文件大小，
        # 连续 stall_kill_after 次不增长就判定 ffmpeg 卡死并重启录制（参考原版逻辑）
        spawn_start = time.time()
        spawn_base_size = self._total_size()
        last_stall_check = time.time()
        last_stall_size = spawn_base_size
        stall_count = 0

        while True:
            if self._stop_event.is_set():
                return "stop"
            rc = proc.poll()
            now = time.time()
            if now - last_file_refresh >= 1.0:
                last_file_refresh = now
                self._refresh_files()
            if now - last_progress >= 5.0:
                last_progress = now
                self._emit(
                    EventType.PROGRESS,
                    "",
                    size=self._total_size(),
                    duration=int(now - self._started_at),
                )
            if now - last_disk_check >= 10.0:
                last_disk_check = now
                if not diskmod.has_enough_space(self.output_dir, self.min_free_gb):
                    return "disk"
            if self.record_timeout_min > 0 and (now - self._started_at) >= self.record_timeout_min * 60:
                return "timeout"
            # 主播是否已下播（回调读缓存，必须很快返回；Manager 保证）
            if self.is_live_fn is not None and now - last_live_check >= 20.0:
                last_live_check = now
                live = self._check_offline()
                if live is False:
                    return "offline"
            # 卡死看门狗
            if self.stall_check_seconds > 0 and now - last_stall_check >= self.stall_check_seconds:
                last_stall_check = now
                cur_size = self._total_size()
                # 启动后整段没收到任何数据：直接判死，不等第二次检查
                if cur_size <= spawn_base_size and now - spawn_start >= self.stall_check_seconds:
                    self._emit(
                        EventType.RECONNECT,
                        "启动后 %.0f 秒未收到任何数据，重启 ffmpeg" % (now - spawn_start),
                    )
                    ffmpeg_util.terminate_process(proc, wait=5.0)
                    return "stalled"
                if cur_size - last_stall_size < 1024:
                    stall_count += 1
                    self.log.warning(
                        "录制文件无增长（%d/%d 次，%d 秒），可能已卡死",
                        stall_count, self.stall_kill_after, self.stall_check_seconds,
                    )
                    if stall_count >= self.stall_kill_after:
                        self._emit(
                            EventType.RECONNECT,
                            "检测到录制卡死（文件连续 %d 次无增长），重启 ffmpeg" % stall_count,
                        )
                        ffmpeg_util.terminate_process(proc, wait=5.0)
                        return "stalled"
                else:
                    stall_count = 0
                last_stall_size = cur_size
            # 单文件大小上限：超过后主动重启 ffmpeg，产生新文件
            if self.segment_size_mb > 0 and self.segment_duration <= 0:
                if self._total_size() >= self.segment_size_mb * 1024 * 1024:
                    self.log.info("单文件达到 %d MB，自动切换新文件", self.segment_size_mb)
                    ffmpeg_util.terminate_process(proc, wait=5.0)
                    return "size"
            if rc is not None:
                return "exited"
            self._stop_event.wait(1.0)

    def _check_offline(self) -> Optional[bool]:
        """调用 is_live_fn 判断是否下播（None 表示未知）。"""
        if self.is_live_fn is None:
            return None
        try:
            live = self.is_live_fn()
        except Exception as exc:  # pragma: no cover
            self.log.debug("is_live_fn 异常: %s", exc)
            live = None
        self._offline_flag = live
        return live

    def _backoff_delay(self) -> int:
        """断流重连间隔：首次 retry_interval，之后指数递增，封顶 retry_interval_max。"""
        n = max(1, self._consecutive_failures)
        delay = self.retry_interval * (2 ** min(n - 1, 6))
        return int(min(delay, self.retry_interval_max))

    def _get_stream_url(self) -> str:
        try:
            url = self._stream_url() if callable(self._stream_url) else self._stream_url
        except Exception as exc:
            self.log.warning("获取流地址异常: %s", exc)
            return ""
        return str(url or "")

    def _output_target(self, attempt: int) -> str:
        """本次 ffmpeg 的输出路径（分段时含 %03d 序号占位）。"""
        suffix = f"_r{attempt}" if attempt > 0 else ""
        base = f"{self.filename_base}{suffix}"
        if self.segment_duration > 0:
            return str(self.output_dir / f"{base}_%03d{self.ext}")
        return str(self.output_dir / f"{base}{self.ext}")

    def _collect_stderr(self, proc: subprocess.Popen) -> None:
        try:
            tail = ffmpeg_util.read_tail(proc.stderr, max_lines=40)
            if tail:
                self._stderr_tail = tail
                self._last_error_detail = tail.strip().splitlines()[-1][:300]
        except Exception:  # pragma: no cover
            pass

    def _refresh_files(self) -> None:
        """扫描输出目录，收集本次会话产生的文件。"""
        try:
            entries = list(self.output_dir.iterdir())
        except OSError:
            return
        added: List[Path] = []
        with self._lock:
            known = {str(f).lower() for f in self._files}
            for f in entries:
                try:
                    if not f.is_file():
                        continue
                except OSError:
                    continue
                if f.suffix.lower() not in (".flv", ".ts", ".mp4", ".mkv"):
                    continue
                if not f.name.startswith(self.filename_base):
                    continue
                if str(f).lower() not in known:
                    added.append(f)
            if added:
                added.sort(key=lambda p: p.name)
                self._files.extend(added)
                self._current_file = str(added[-1])
        for f in added:
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            if size > 0:
                self._emit(EventType.SEGMENT, f"新分段: {f.name}", path=str(f), size=size)

    def _total_size(self, files: Optional[List[Path]] = None) -> int:
        """统计文件总大小；不传 files 时统计本场已收集的所有文件。"""
        if files is None:
            with self._lock:
                files = list(self._files)
        total = 0
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        return total

    def _finalize(self) -> List[Path]:
        """录制收尾：刷新文件列表，按配置 remux 为 MP4，并按 keep_source 决定是否删源。

        返回最终文件列表（GUI/状态里展示的就是这些文件）。
        """
        self._refresh_files()
        with self._lock:
            files = [f for f in self._files if f.exists()]
        files = [f for f in files if _safe_size(f) > 0]
        if not files:
            return []
        if not self.remux_mp4:
            return files
        originals = list(files)
        result = self._remux_files(originals)
        if not result or result == originals:
            return files
        produced = {p for p in result if p.suffix.lower() == ".mp4"}
        if self.keep_source:
            # 保留原文件：原文件 + MP4 都列出
            return originals + [p for p in result if p not in originals]
        # 删除已成功 remux 的源文件（remux 失败、仍在 result 里的保留）
        result_set = set(result)
        for src in originals:
            if src.suffix.lower() == ".mp4" or src in result_set:
                continue
            try:
                src.unlink()
            except OSError:
                pass
        return result

    # ---------------- 历史记录（失败不影响录制） ----------------
    def _history_begin(self) -> None:
        """开始录制时写入运行记录。"""
        try:
            self._history_id = historymod.record_recording_start(
                anchor=self.name, room_id=self.room_id, quality=self.quality,
                started_at=self._started_at, path=str(self.output_dir),
            )
        except Exception:  # pragma: no cover - history 内部已兜底
            self._history_id = 0
            self.log.debug("写录制开始记录失败", exc_info=True)

    def _history_end(self, files: List[Path], media_duration: float, deleted: bool) -> None:
        """录制结束时更新运行记录。"""
        if not self._history_id:
            return
        try:
            if media_duration > 0:
                duration = media_duration
            else:
                duration = max(0.0, (self._ended_at or time.time()) - self._started_at)
            size = 0 if deleted else sum(_safe_size(f) for f in files)
            path = "" if deleted else (str(files[-1]) if files else "")
            historymod.record_recording_end(
                self._history_id,
                ended_at=self._ended_at or time.time(),
                duration_sec=round(duration, 3),
                size_bytes=size,
                path=path,
                status=("error" if self._error else "stopped"),
                error=self._error,
                deleted=deleted,
            )
        except Exception:  # pragma: no cover - history 内部已兜底
            self.log.debug("写录制结束记录失败", exc_info=True)

    def _record_error(self, message: str) -> None:
        """把录制错误写入历史库。"""
        try:
            historymod.record_error("recorder:%s" % self.name, message)
        except Exception:  # pragma: no cover
            self.log.debug("写错误记录失败", exc_info=True)

    # ---------------- 最短保留时长 ----------------
    def _delete_if_too_short(self, files: List[Path]) -> Tuple[List[Path], bool, float]:
        """录制总时长低于 min_keep_seconds 时删除本场全部产物。

        返回 (剩余文件列表, 是否删除, 用于判断的时长秒数)。
        时长优先用 ffmpeg 解析产物（多段累加）；解析失败回落到运行时计时。
        """
        if self.min_keep_seconds <= 0 or not files:
            return files, False, 0.0
        # keep_source=True 时列表里同时有源文件和 MP4，只用 MP4 计时避免重复累加
        probe_files = [f for f in files if f.suffix.lower() == ".mp4"] or list(files)
        duration = ffmpeg_util.probe_total_duration(self.ffmpeg_path, probe_files)
        if duration <= 0:
            duration = max(0.0, (self._ended_at or time.time()) - self._started_at)
        if duration >= self.min_keep_seconds:
            return files, False, duration
        # 安全保护：ffmpeg 仍在写入（理论上收尾前已退出），绝不删除正在录制的文件
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self.log.warning("ffmpeg 仍在写入，跳过过短录制删除")
            return files, False, duration
        removed = 0
        for path in self._session_products(files):
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                self.log.warning("删除过短录制产物失败 %s: %s", path, exc)
        remaining = [f for f in files if f.exists()]
        if remaining:
            # 部分产物删除失败：保留剩余文件，不宣称「已删除」，避免状态与实际不符
            self.log.warning("过短录制产物未删净，保留 %d 个文件", len(remaining))
            return remaining, False, duration
        self.log.info("过短录制已删除：%d 个产物（%.1f 秒 < %d 秒）",
                      removed, duration, self.min_keep_seconds)
        return [], True, duration

    def _session_products(self, files: List[Path]) -> List[Path]:
        """本场录制的全部产物：收尾文件 + 分段/重连文件 + sidecar/临时文件。"""
        products: List[Path] = []
        seen = set()

        def add(path: Path) -> None:
            key = str(path).lower()
            if key in seen:
                return
            seen.add(key)
            products.append(path)

        for item in files:
            add(Path(item))
        try:
            entries = list(self.output_dir.iterdir())
        except OSError:
            return products
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if self._is_session_file(entry.name):
                add(entry)
        return products

    def _is_session_file(self, name: str) -> bool:
        """判断文件名是否属于本场录制（前缀匹配过宽会误删相邻录像，这里收紧规则）。"""
        base = self.filename_base
        if name.startswith("." + base):
            return True
        stem, dot, ext = name.rpartition(".")
        if not dot or ext.lower() not in ("flv", "ts", "mp4", "mkv"):
            return False
        if stem == base:
            return True
        if not stem.startswith(base + "_"):
            return False
        rest = stem[len(base) + 1:]
        if rest.startswith("r"):  # 重连：base_r1.ext / base_r1_000.ext
            head, _sep, tail = rest.partition("_")
            if not head[1:].isdigit():
                return False
            if not tail:
                return True
            rest = tail
        return rest.isdigit() and len(rest) >= 3  # 分段：base_000.ext

    def _remux_files(self, files: List[Path]) -> List[Path]:
        """把录制文件（单段或多段）转成 MP4。不删除源文件，由 _finalize 决定。"""
        if len(files) == 1:
            src = files[0]
            ok, info = ffmpeg_util.remux_to_mp4(self.ffmpeg_path, src)
            if ok:
                dst = Path(info)
                self.log.info("remux 完成: %s", dst)
                return [dst]
            self.log.warning("单文件 remux 失败，保留原文件: %s", info)
            return files

        # 多段：先尝试 concat 合并再 remux；失败则逐段 remux
        merged = self._concat_and_remux(files)
        if merged is not None:
            return [merged]
        result: List[Path] = []
        for src in files:
            ok, info = ffmpeg_util.remux_to_mp4(self.ffmpeg_path, src)
            if ok:
                result.append(Path(info))
            else:
                self.log.warning("分段 remux 失败，保留原文件: %s", info)
                result.append(src)
        return result

    def _concat_and_remux(self, files: List[Path]) -> Optional[Path]:
        """用 concat demuxer 合并多段后 remux 为 MP4。失败返回 None。"""
        base = self.filename_base
        out = self.output_dir / f"{base}.mp4"
        list_file = self.output_dir / f".{base}_concat.txt"
        try:
            with open(list_file, "w", encoding="utf-8", newline="\n") as fp:
                for f in files:
                    # concat demuxer 的 file 路径用相对 list 文件目录，避免转义问题
                    fp.write(f"file '{f.name}'\n")
            cmd = [
                self.ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c", "copy", "-movflags", "+faststart", str(out),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, timeout=1800,
                creationflags=ffmpeg_util.CREATE_NO_WINDOW,
            )
            list_file.unlink(missing_ok=True)
            if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                err = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
                self.log.warning("concat 合并失败: %s", err)
                try:
                    if out.exists() and out.stat().st_size == 0:
                        out.unlink()
                except OSError:
                    pass
                return None
            self.log.info("多段合并完成: %s", out)
            return out
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.warning("concat 合并异常: %s", exc)
            try:
                list_file.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def _emit(self, kind: str, message: str = "", *, path: str = "", size: int = 0,
              duration: int = 0, deleted: bool = False) -> None:
        ev = RecordEvent(
            kind=kind, room_id=self.room_id, name=self.name, message=message,
            path=path, size=size, duration=duration,
        )
        # 不改 model.py（其他人维护）：用「动态属性 + 文本」双重表达「已删除」，
        # 事件消费者可读 ev.deleted / message 里的「已删除」文案。
        if deleted:
            try:
                setattr(ev, "deleted", True)
            except Exception:  # pragma: no cover
                pass
        cb = self.on_event
        if cb is not None:
            try:
                cb(ev)
            except Exception:  # pragma: no cover - 回调异常不致命
                self.log.exception("录制事件回调异常")
        if kind == EventType.ERROR:
            self.log.error("[%s] %s", self.name, message)
            self._record_error(message)
        elif kind == EventType.PROGRESS:
            self.log.debug("[%s] 录制中 %s", self.name, diskmod.human_size(size))
        else:
            self.log.info("[%s] %s", self.name, message)


def _fmt_seconds(seconds: float) -> str:
    """秒数转简短文本（整数不带小数，用于「过短录制已删除」提示）。"""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "0"
    if value >= 100 or value == int(value):
        return str(int(round(value)))
    return ("%.1f" % value).rstrip("0").rstrip(".")


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _short_cmd(cmd: List[str]) -> str:
    """日志里显示命令（隐藏过长 URL）。"""
    parts = []
    for c in cmd:
        s = str(c)
        if len(s) > 120:
            s = s[:60] + "..." + s[-30:]
        parts.append(s)
    return " ".join(parts)