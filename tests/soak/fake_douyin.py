# -*- coding: utf-8 -*-
"""假抖音直播接口服务（浸泡测试专用，不联真实抖音）。

模拟两个接口：
  GET /webcast/room/web/enter/?web_rid=<rid>   房间状态（在线 ~700KB / 离线 ~48KB，支持 gzip）
  GET /webcast/feed/                           发现页在播列表（~700KB）
  GET /stats                                   服务端统计（请求数/错误数/字节数）

特性：
- 每个主播有独立的开播/下播周期，稳态在播比例接近 live_ratio；
- 随机延迟 latency_min~latency_max（默认 0.15~0.9s），模拟真实接口耗时；
- 按 error_rate 注入错误：4001038 风控 JSON / HTTP 403 / 空响应 / 超时（16s 不响应）；
- 响应体大小接近真实（在线含签名拉流地址 + 合成 padding）。

独立运行（给别的进程/脚本用）：
    python tests/soak/fake_douyin.py --port 18765 --anchors 1000 --live-ratio 0.1
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import random
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

# 响应体目标大小（字节）
ONLINE_SIZE = 700 * 1024
OFFLINE_SIZE = 48 * 1024


def _make_pad(n: int, seed: str) -> str:
    """生成 n 字节的 base64 文本（不可压缩，模拟真实响应里的长字段）。"""
    rng = random.Random(hash(seed) & 0xFFFFFFFF)
    raw = bytes(rng.getrandbits(8) for _ in range(max(1, n * 3 // 4)))
    return base64.b64encode(raw).decode("ascii")[:n]


# 在线/离线各预生成 3 份 padding 轮换，避免每次请求都拼 700KB
_PADS_ONLINE = [_make_pad(ONLINE_SIZE - 4096, "online%d" % i) for i in range(3)]
_PADS_OFFLINE = [_make_pad(OFFLINE_SIZE - 2048, "offline%d" % i) for i in range(2)]


@dataclass
class Anchor:
    """一个假主播的直播状态机。"""
    rid: str
    name: str
    live: bool
    phase_end: float

    def refresh(self, now: float, rng: random.Random, live_ratio: float) -> bool:
        while now >= self.phase_end:
            self.live = not self.live
            if self.live:
                duration = rng.uniform(180.0, 360.0)
            else:
                ratio = max(0.01, min(0.99, live_ratio))
                duration = rng.uniform(180.0, 360.0) * (1.0 - ratio) / ratio
            self.phase_end = max(self.phase_end, now) + duration
        return self.live


class FakeDouyin:
    """假抖音服务的数据与统计。"""

    def __init__(self, anchors: int = 1000, live_ratio: float = 0.1,
                 latency_min: float = 0.15, latency_max: float = 0.9,
                 error_rate: float = 0.03, seed: int = 2026):
        self.live_ratio = max(0.0, min(1.0, live_ratio))
        self.latency_min = max(0.0, latency_min)
        self.latency_max = max(self.latency_min, latency_max)
        self.error_rate = max(0.0, min(1.0, error_rate))
        self.rng = random.Random(seed)
        now = time.time()
        self._lock = threading.RLock()
        self.anchors: Dict[str, Anchor] = {}
        ratio = max(0.01, min(0.99, self.live_ratio))
        for i in range(anchors):
            rid = str(10000000000 + i)
            live = self.rng.random() < self.live_ratio
            live_duration = self.rng.uniform(180.0, 360.0)
            if live:
                # 随机相位：当前状态剩余时间在 [0, 时长) 内，保证任意时刻稳态分布接近 live_ratio
                phase = self.rng.uniform(0.0, live_duration)
            else:
                offline_duration = live_duration * (1.0 - ratio) / ratio
                phase = self.rng.uniform(0.0, offline_duration)
            self.anchors[rid] = Anchor(
                rid=rid, name="主播%04d" % i, live=live, phase_end=now + phase,
            )
        self.started_at = now
        self.stats = {
            "requests": 0, "ok": 0, "errors": 0, "gzip": 0, "bytes": 0,
            "enter": 0, "feed": 0, "stats": 0, "inflight": 0,
            "error_kinds": {},
        }
        self._pad_idx = 0

    # ---------------- 状态 ----------------
    def refresh_anchor(self, rid: str) -> Optional[Anchor]:
        anchor = self.anchors.get(rid)
        if anchor is None:
            return None
        with self._lock:
            anchor.refresh(time.time(), self.rng, self.live_ratio)
        return anchor

    def is_live(self, rid: str) -> bool:
        anchor = self.refresh_anchor(rid)
        return bool(anchor and anchor.live)

    def live_rids(self) -> list:
        now = time.time()
        result = []
        with self._lock:
            for rid, anchor in self.anchors.items():
                anchor.refresh(now, self.rng, self.live_ratio)
                if anchor.live:
                    result.append(rid)
        return result

    # ---------------- 响应体 ----------------
    def _stream_payload(self, anchor: Anchor, now: float) -> dict:
        """构造与真实 enter 接口结构相近的 payload。"""
        expiry = int(now) + 3600
        token = "%032x" % self.rng.getrandbits(128)
        base = ("http://pull-flv-l11.douyincdn.com/stage/stream-12043"
                "?expire=%d&sign=%s&volcSecret=%s" % (expiry, token, token))
        resolutions = ["FULL_HD1", "HD1", "SD1", "SD2"]
        flv = {}
        hls = {}
        for res in resolutions:
            flv[res] = base + "&resolution=" + res
            hls[res] = base.replace("pull-flv", "pull-hls") + "&resolution=" + res + ".m3u8"
        pad = _PADS_ONLINE[self._pad_idx % len(_PADS_ONLINE)]
        self._pad_idx += 1
        return {
            "data": {
                "data": [{
                    "id_str": "7707609448951466779",
                    "status": 2,
                    "title": "假直播间标题 %s" % anchor.name,
                    "create_time": int(now) - 1200,
                    "stream_url": {
                        "default_resolution": "FULL_HD1",
                        "flv_pull_url": flv,
                        "hls_pull_url_map": hls,
                        "hls_pull_url": hls["FULL_HD1"],
                    },
                    "owner": {
                        "id_str": "88" + anchor.rid[-8:],
                        "nickname": anchor.name,
                        "avatar_thumb": {"url_list": [base + "&avatar=1"]},
                    },
                    "room_view_stats": {"display_value": 1234, "display_short": "1.2万"},
                    "raw_padding": pad,
                }],
                "user": {"id_str": "0", "nickname": ""},
            },
            "status_code": 0,
        }

    def _offline_payload(self, anchor: Anchor, now: float) -> dict:
        pad = _PADS_OFFLINE[self._pad_idx % len(_PADS_OFFLINE)]
        self._pad_idx += 1
        return {
            "data": {
                "data": [{
                    "id_str": "7707609448951466779",
                    "status": 4,
                    "title": "",
                    "owner": {"nickname": anchor.name},
                    "raw_padding": pad,
                }],
                "user": {"id_str": "0"},
            },
            "status_code": 0,
        }

    def enter_payload(self, rid: str) -> bytes:
        anchor = self.refresh_anchor(rid)
        if anchor is None:
            body = {"status_code": 4001044, "data": {"data": []}}
            return json.dumps(body, ensure_ascii=False).encode("utf-8")
        now = time.time()
        if anchor.live:
            body = self._stream_payload(anchor, now)
        else:
            body = self._offline_payload(anchor, now)
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def feed_payload(self) -> bytes:
        rids = self.live_rids()[:20]
        items = []
        for rid in rids:
            anchor = self.anchors[rid]
            items.append({
                "web_rid": rid,
                "title": "假直播 %s" % anchor.name,
                "owner": {"nickname": anchor.name},
                "stream_url": self._stream_payload(anchor, time.time())["data"]["data"][0]["stream_url"],
            })
        pad = _PADS_ONLINE[self._pad_idx % len(_PADS_ONLINE)]
        self._pad_idx += 1
        body = {"data": {"data": items, "raw_padding": pad}, "status_code": 0}
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    # ---------------- 统计 ----------------
    def stats_snapshot(self) -> dict:
        with self._lock:
            data = dict(self.stats)
            data["error_kinds"] = dict(self.stats["error_kinds"])
            data["uptime"] = round(time.time() - self.started_at, 1)
            data["live_rooms"] = len(self.live_rids())
            return data

    def _bump(self, key: str, delta: int = 1) -> None:
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + delta

    def _bump_error(self, kind: str) -> None:
        with self._lock:
            self.stats["errors"] += 1
            kinds = self.stats["error_kinds"]
            kinds[kind] = kinds.get(kind, 0) + 1

    # ---------------- HTTP 服务 ----------------
    def start(self, port: int = 0) -> int:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):
                pass

            def _send(self, body: bytes, status: int = 200, gzip_ok: bool = False):
                accept = self.headers.get("Accept-Encoding", "") or ""
                if gzip_ok and "gzip" in accept.lower():
                    body = gzip.compress(body, compresslevel=1)
                    fake._bump("gzip")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                if gzip_ok and "gzip" in accept.lower():
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                fake._bump("bytes", len(body))

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                fake._bump("requests")
                fake._bump("inflight")
                try:
                    # 随机延迟
                    delay = fake.rng.uniform(fake.latency_min, fake.latency_max)
                    time.sleep(delay)
                    if path.startswith("/stats"):
                        fake._bump("stats")
                        body = json.dumps(fake.stats_snapshot(), ensure_ascii=False).encode("utf-8")
                        self._send(body, 200)
                        fake._bump("ok")
                        return
                    if path.rstrip("/").endswith("enter"):
                        fake._bump("enter")
                        qs = parse_qs(parsed.query)
                        rid = (qs.get("web_rid") or qs.get("room_id") or [""])[0]
                        if fake.rng.random() < fake.error_rate:
                            self._inject_error()
                            return
                        body = fake.enter_payload(str(rid))
                        self._send(body, 200, gzip_ok=True)
                        fake._bump("ok")
                        return
                    if path.rstrip("/").endswith("feed"):
                        fake._bump("feed")
                        if fake.rng.random() < fake.error_rate:
                            self._inject_error()
                            return
                        self._send(fake.feed_payload(), 200, gzip_ok=True)
                        fake._bump("ok")
                        return
                    self._send(json.dumps({"status_code": 404, "data": {}}).encode("utf-8"), 404)
                finally:
                    fake._bump("inflight", -1)

            def _inject_error(self):
                roll = fake.rng.random()
                if roll < 0.40:
                    fake._bump_error("4001038")
                    body = json.dumps({"status_code": 4001038, "data": {}}).encode("utf-8")
                    self._send(body, 200)
                elif roll < 0.60:
                    fake._bump_error("http403")
                    body = json.dumps({"status_code": 403, "data": {}}).encode("utf-8")
                    self._send(body, 403)
                elif roll < 0.75:
                    fake._bump_error("http500")
                    body = b"internal error"
                    self._send(body, 500)
                elif roll < 0.90:
                    fake._bump_error("empty")
                    self._send(b"", 200)
                else:
                    # 超时：sleep 16 秒后直接断开，客户端会超时
                    fake._bump_error("timeout")
                    time.sleep(16)
                    self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-douyin", daemon=True,
        )
        self._thread.start()
        self.port = self._server.server_address[1]
        return self.port

    def stop(self) -> None:
        server = getattr(self, "_server", None)
        if server is not None:
            server.shutdown()
            server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="假抖音直播接口服务")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--anchors", type=int, default=1000)
    parser.add_argument("--live-ratio", type=float, default=0.1)
    parser.add_argument("--error-rate", type=float, default=0.03)
    parser.add_argument("--latency-min", type=float, default=0.15)
    parser.add_argument("--latency-max", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    fake = FakeDouyin(
        anchors=args.anchors, live_ratio=args.live_ratio,
        latency_min=args.latency_min, latency_max=args.latency_max,
        error_rate=args.error_rate, seed=args.seed,
    )
    port = fake.start(args.port)
    print("fake douyin listening on http://127.0.0.1:%d  anchors=%d live_ratio=%.2f"
          % (port, len(fake.anchors), args.live_ratio), flush=True)
    print("enter: http://127.0.0.1:%d/webcast/room/web/enter/?web_rid=10000000000" % port, flush=True)
    try:
        while True:
            time.sleep(10)
            snap = fake.stats_snapshot()
            print("[stats] req=%d ok=%d err=%d gzip=%d bytes=%.1fMB live=%d" % (
                snap["requests"], snap["ok"], snap["errors"], snap["gzip"],
                snap["bytes"] / 1048576.0, snap["live_rooms"]), flush=True)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()