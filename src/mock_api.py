# -*- coding: utf-8 -*-
"""离线 mock 协议层：在真实 dy_api.py 未就绪/无网络时提供「假直播」。

作用：
1. 让录制引擎、监控调度、GUI、CLI 在没有任何网络请求的情况下完整跑通；
2. 提供一个真实的 HTTP-FLV 测试源（本机 http.server），ffmpeg 按实时速率读取，
   可模拟断流（mock_disconnect_after）与下播（mock_offline_after），用于测试重连/自动停止。

接口与 src/dy_api.py 契约完全一致，可以直接作为其替代品：
    LiveStatus / resolve_target / get_live_status / choose_stream / get_cookie

配置优先级：configure(...) 参数 > 环境变量 > 默认值。
环境变量：DYREC_MOCK_FFMPEG、DYREC_MOCK_LIVE_SECONDS、DYREC_MOCK_OFFLINE_AFTER、
          DYREC_MOCK_DISCONNECT_AFTER、DYREC_MOCK_USE_HTTP。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

LOG = logging.getLogger("dyrec.mock_api")

ROOT = Path(__file__).resolve().parent.parent
MOCK_DIR = ROOT / "tests" / "tmp" / "mock"
BUNDLED_FFMPEG = r"D:\抖音录制\抖音直播录制工具\PotPlayer\cmdffmpeg.exe"


@dataclass
class LiveStatus:
    """与 dy_api.LiveStatus 同构。"""
    is_live: bool
    room_id: str = ""
    title: str = ""
    anchor_name: str = ""
    cover_url: str = ""
    started_at: int = 0
    streams: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# 可配置状态
# ---------------------------------------------------------------------------
_cfg = {
    "ffmpeg": os.environ.get("DYREC_MOCK_FFMPEG", "") or "",
    "live_seconds": int(os.environ.get("DYREC_MOCK_LIVE_SECONDS", "120")),
    "offline_after": float(os.environ.get("DYREC_MOCK_OFFLINE_AFTER", "0")),
    "disconnect_after": float(os.environ.get("DYREC_MOCK_DISCONNECT_AFTER", "0")),
    "use_http": os.environ.get("DYREC_MOCK_USE_HTTP", "1") not in ("0", "false", "False"),
}
_lock = threading.RLock()
_first_call_at = 0.0
_media_path: Optional[Path] = None
_media_duration = 0.0
_server: Optional[ThreadingHTTPServer] = None
_server_port = 0


def configure(ffmpeg_path: str = "", live_seconds: Optional[int] = None,
              offline_after: Optional[float] = None,
              disconnect_after: Optional[float] = None,
              use_http: Optional[bool] = None) -> None:
    """由 Manager/测试脚本调用，覆盖默认 mock 参数。"""
    with _lock:
        if ffmpeg_path:
            _cfg["ffmpeg"] = ffmpeg_path
        if live_seconds is not None:
            _cfg["live_seconds"] = max(5, int(live_seconds))
        if offline_after is not None:
            _cfg["offline_after"] = max(0.0, float(offline_after))
        if disconnect_after is not None:
            _cfg["disconnect_after"] = max(0.0, float(disconnect_after))
        if use_http is not None:
            _cfg["use_http"] = bool(use_http)
        global _media_path, _first_call_at
        _media_path = None  # 参数变了重新生成
        _first_call_at = 0.0  # 重置 mock 时间线（离线/断流计时从下一次查询开始）


def _find_ffmpeg() -> str:
    """按 配置 > 项目 bin > 原软件目录 > PATH 查找 ffmpeg。"""
    configured = _cfg.get("ffmpeg") or ""
    if configured and Path(configured).is_file():
        return configured
    for cand in (ROOT / "bin" / "ffmpeg.exe", ROOT / "bin" / "ffmpeg", BUNDLED_FFMPEG):
        try:
            if Path(cand).is_file() and Path(cand).stat().st_size > 0:
                return str(cand)
        except OSError:
            continue
    import shutil
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    return found or ""


def _ensure_media() -> Path:
    """生成（或复用）一段测试 FLV，作为假直播源。"""
    global _media_path, _media_duration
    with _lock:
        if _media_path is not None and _media_path.exists() and _media_path.stat().st_size > 0:
            return _media_path
        MOCK_DIR.mkdir(parents=True, exist_ok=True)
        target = MOCK_DIR / f"mock_live_{int(_cfg['live_seconds'])}s.flv"
        if target.exists() and target.stat().st_size > 10_000:
            _media_path = target
            _media_duration = float(_cfg['live_seconds'])
            return target
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("mock 需要 ffmpeg 生成测试流：请在配置中指定 ffmpeg 路径")
        seconds = int(_cfg["live_seconds"])
        LOG.info("生成 mock 测试流（%d 秒）: %s", seconds, target)
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", str(seconds),
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", "30",
            "-c:a", "aac", "-b:a", "96k",
            "-f", "flv", str(target),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
        if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            err = (proc.stderr or b"").decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"生成 mock 测试流失败: {err}")
        _media_path = target
        _media_duration = float(seconds)
        LOG.info("mock 测试流就绪: %s (%.1f MB)", target, target.stat().st_size / 1048576)
        return target


class _MockHandler(BaseHTTPRequestHandler):
    """极简 HTTP-FLV 服务：按实时速率发送，可选中途断开模拟断流。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默
        pass

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _serve(self, head_only: bool):
        try:
            media = _ensure_media()
            size = media.stat().st_size
        except Exception as exc:  # pragma: no cover
            self.send_error(500, str(exc))
            return
        duration = max(1.0, _media_duration)
        bps = max(64 * 1024, size / duration)  # 至少 64KB/s，避免测试等太久
        disconnect_after = float(_cfg.get("disconnect_after") or 0)
        self.send_response(200)
        self.send_header("Content-Type", "video/x-flv")
        self.send_header("Content-Length", str(size))
        self.send_header("Connection", "close")
        self.end_headers()
        if head_only:
            return
        start = time.time()
        try:
            with open(media, "rb") as fp:
                while True:
                    chunk = fp.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if disconnect_after > 0 and (time.time() - start) >= disconnect_after:
                        LOG.info("mock: 模拟断流，已发送 %.1f 秒", time.time() - start)
                        self.close_connection = True
                        return
                    if bps > 0:
                        time.sleep(min(1.0, len(chunk) / bps))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _ensure_server() -> int:
    """启动（一次）本机 HTTP 服务，返回端口。"""
    global _server, _server_port
    with _lock:
        if _server is not None:
            return _server_port
        _ensure_media()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        srv.daemon_threads = True
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, name="mock-flv-server", daemon=True)
        t.start()
        _server = srv
        _server_port = port
        LOG.info("mock HTTP-FLV 服务已启动: http://127.0.0.1:%d/live.flv", port)
        return port


def _stream_url() -> str:
    global _first_call_at
    with _lock:
        if not _first_call_at:
            _first_call_at = time.time()
        if _cfg.get("use_http"):
            port = _ensure_server()
            return f"http://127.0.0.1:{port}/live.flv"
        media = _ensure_media()
        return media.resolve().as_uri()


# ---------------------------------------------------------------------------
# 契约实现
# ---------------------------------------------------------------------------
_RE_RID = re.compile(r"(\d{5,})")


def resolve_target(text: str) -> dict:
    """本地解析：从链接/房间号/分享文案里取 web_rid。"""
    raw = (text or "").strip()
    rid = ""
    m = _RE_RID.search(raw)
    if m:
        rid = m.group(1)
    url = ""
    if raw.startswith("http"):
        url = raw
    elif rid:
        url = f"https://live.douyin.com/{rid}"
    return {"web_rid": rid or None, "sec_uid": None, "url": url}


def get_live_status(target: str, cookie: dict | None = None, timeout: float = 15) -> LiveStatus:
    """返回 mock 直播状态；mock_offline_after 秒后变为「已下播」。"""
    global _first_call_at
    try:
        url = _stream_url()
    except Exception as exc:
        return LiveStatus(is_live=False, error=f"mock 初始化失败: {exc}")
    info = resolve_target(target)
    with _lock:
        started = _first_call_at or time.time()
        elapsed = time.time() - started
        offline_after = float(_cfg.get("offline_after") or 0)
        if offline_after > 0 and elapsed > offline_after:
            return LiveStatus(
                is_live=False,
                room_id=str(info.get("web_rid") or ""),
                anchor_name="测试主播",
                title="Mock 测试直播（已下播）",
            )
    return LiveStatus(
        is_live=True,
        room_id=str(info.get("web_rid") or "mock"),
        title="Mock 测试直播（本地测试源）",
        anchor_name="测试主播",
        started_at=int(started),
        streams={"flv": [url], "hls": []},
        raw={"mock": True, "url": url},
    )


def choose_stream(status: LiveStatus, prefer: str = "flv", quality: str = "origin") -> Optional[str]:
    """优先 FLV，其次 HLS。"""
    streams = getattr(status, "streams", None) or {}
    order = [prefer] if prefer else []
    order += ["flv", "hls"]
    seen = set()
    for key in order:
        if key in seen:
            continue
        seen.add(key)
        urls = streams.get(key)
        if isinstance(urls, str) and urls:
            return urls
        if isinstance(urls, list):
            for u in urls:
                if u:
                    return u
    # 兜底：origin 字典里找
    origin = streams.get("origin")
    if isinstance(origin, dict):
        for key in (quality, "flv", "hd", "sd"):
            val = origin.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, list) and val:
                return val[0]
    return None


def get_cookie() -> dict:
    """mock cookie。"""
    return {"ttwid": "mock-ttwid-for-local-test"}


def selftest() -> None:
    """python src/mock_api.py --selftest：打印 mock 状态并可手动验证。"""
    st = get_live_status("123456789")
    print(json.dumps({
        "is_live": st.is_live,
        "room_id": st.room_id,
        "title": st.title,
        "streams": st.streams,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    selftest()