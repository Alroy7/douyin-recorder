# -*- coding: utf-8 -*-
"""浸泡测试专用 HTTP-FLV 测试流服务（多客户端共享一路流）。

与 src/mock_api.py 内嵌服务器的区别：
- request_queue_size 放大到 256（默认 5 太小，10 路并发 FFmpeg 时会有连接排队，
  导致 ffmpeg 拿到连接但迟迟收不到数据 -> 被卡死看门狗误杀，这是 harness 侧的问题）；
- 每个连接独立线程、按媒体时长实时限速发送；
- 支持任意并发客户端，记录连接数/字节数统计。

用法（在 run_soak.py 内部使用）：
    srv = StreamServer(ffmpeg, tmp_dir / "stream", seconds=120)
    url = srv.start()   # http://127.0.0.1:port/live.flv
    ...
    srv.stop()
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("soak.stream")

CHUNK = 64 * 1024


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


class StreamServer:
    """生成一次测试 FLV，然后以 HTTP 提供（多客户端、实时限速）。"""

    def __init__(self, ffmpeg: str, media_dir: Path, seconds: int = 120,
                 realtime: bool = True) -> None:
        self.ffmpeg = ffmpeg
        self.media_dir = Path(media_dir)
        self.seconds = max(5, int(seconds))
        self.realtime = realtime
        self._media_path: Optional[Path] = None
        self._server: Optional[_ThreadingServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connections = 0
        self._active = 0
        self._bytes_sent = 0
        self.port = 0

    # ---------------- 媒体 ----------------
    def ensure_media(self) -> Path:
        with self._lock:
            if self._media_path is not None and self._media_path.exists():
                return self._media_path
            self.media_dir.mkdir(parents=True, exist_ok=True)
            target = self.media_dir / ("stream_%ds.flv" % self.seconds)
            if target.exists() and target.stat().st_size > 10000:
                self._media_path = target
                return target
            LOG.info("生成共享测试流（%d 秒）: %s", self.seconds, target)
            cmd = [
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", str(self.seconds),
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-g", "30",
                "-c:a", "aac", "-b:a", "96k",
                "-f", "flv", str(target),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=600)
            if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
                err = (proc.stderr or b"").decode("utf-8", "replace")[-500:]
                raise RuntimeError("生成测试流失败: " + err)
            self._media_path = target
            LOG.info("测试流就绪: %s (%.1f MB)", target, target.stat().st_size / 1048576.0)
            return target

    # ---------------- HTTP ----------------
    def start(self) -> str:
        media = self.ensure_media()
        size = media.stat().st_size
        bps = max(32 * 1024, size / float(self.seconds)) if self.realtime else 0.0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.send_header("Content-Length", str(size))
                self.end_headers()

            def do_GET(self):
                with owner._lock:
                    owner._connections += 1
                    owner._active += 1
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/x-flv")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        with open(media, "rb") as fp:
                            while True:
                                chunk = fp.read(CHUNK)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                self.wfile.flush()
                                with owner._lock:
                                    owner._bytes_sent += len(chunk)
                                if bps > 0:
                                    time.sleep(min(1.0, len(chunk) / bps))
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                finally:
                    with owner._lock:
                        owner._active -= 1

        self._server = _ThreadingServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="soak-stream", daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]
        LOG.info("HTTP-FLV 测试流: http://127.0.0.1:%d/live.flv (%d 连接 backlog 256)",
                 self.port, self.seconds)
        return "http://127.0.0.1:%d/live.flv" % self.port

    def stats(self) -> dict:
        with self._lock:
            return {
                "connections": self._connections,
                "active": self._active,
                "bytes_mb": round(self._bytes_sent / 1048576.0, 1),
            }

    def stop(self) -> None:
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
            self._server = None
