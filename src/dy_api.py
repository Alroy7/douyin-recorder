#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音直播取流协议层 —— 本项目唯一的抖音接口实现（按 docs/INTERFACES.md 契约）。

面向「本地抖音直播录制工具」：给定 房间号 / 直播间链接 / 分享短链 / 分享文案 / 电脑主播主页链接，
返回直播状态（标题、主播、封面、开播时间）与可录制的 flv/hls 拉流地址。

实测结论（2026-11-13，见 docs/douyin_api.md、analysis/probe/）：
  * 只需要 ttwid cookie（GET https://live.douyin.com/ 下发），**不需要 a_bogus / X-Bogus**。
  * 房间状态/取流：GET https://live.douyin.com/webcast/room/web/enter/?...&web_rid=<web_rid>
  * 在播发现：    GET https://live.douyin.com/webcast/feed/?...
  * 分享短链：    v.douyin.com 302 -> webcast.amemv.com/douyin/webcast/reflow/<19位room_id>?sec_user_id=...
                  -> webcast.amemv.com/webcast/room/reflow/info/?room_id=&sec_user_id=  (未签名可用)
                  -> data.room.owner.web_rid，再用 enter 接口取流。

设计约束：
  * 只用 Python 标准库（urllib / http.client / ssl / http.cookiejar / zlib），不依赖第三方、不做浏览器自动化。
  * 对外函数**绝不抛未捕获异常**：网络/解析/风控错误一律返回 LiveStatus(error="...")。
  * 带整体超时预算（默认 12~15s），上层可 60s 轮询；连接失败自动重试并刷新 ttwid。
  * 拉流地址带时效签名，每次开播/重连都必须重新调用 get_live_status 获取，不要长期缓存。

性能（v1.1，挂机监控 1000+ 房间）：
  * HTTP 响应请求 gzip/deflate 并解压（在线房间响应 ~776KB -> ~108KB 线上流量）。
  * 每线程 keep-alive 连接复用（http.client.HTTPSConnection），不再每次新建 TLS。
  * 连接超时 5s / 读取超时 10s；单响应上限 2MB，超限中止并返回 error（不抛异常）。
  * 解析后不保留原始大 JSON：LiveStatus.raw 只留精简字段，URL 只存一份（streams 列表 +
    qualities 用下标引用）。
  * 线程安全：每线程独立连接、cookie/sec_uid 缓存加锁，可直接多线程并发查询。
  * 负缓存（v1.1.1）：主播主页链接解析出「该账号明确没有抖音号/无直播间」时缓存
    NEGATIVE_CACHE_TTL（默认 600s），期间重复查询直接返回相同 error、不再发解析请求；
    解析到直播间后自动失效。统计见 get_http_stats()["negative_cache_hits"]。
  * 解析缓存落盘（v1.1.2）：sec_uid -> web_rid 的正缓存写入 data/resolve_cache.json
    （环境变量 DYREC_RESOLVE_CACHE 可覆盖路径），重启后首次使用自动加载；写入去抖
    （最多 10s 一次，tmp+os.replace 原子写），失败只 warning 不影响运行。

命令行自测：
  python src/dy_api.py --selftest                     # 自动从在播列表挑一个房间实测
  python src/dy_api.py --selftest 76274427953         # 房间号（web_rid）
  python src/dy_api.py --selftest https://live.douyin.com/76274427953
  python src/dy_api.py --selftest https://v.douyin.com/iQLgKSj/   # 分享短链
  python src/dy_api.py --selftest "长按复制此条消息... https://v.douyin.com/xxx/"
  python src/dy_api.py --selftest https://www.douyin.com/user/<sec_uid>  # 主播主页链接
  python src/dy_api.py --recommend 10                 # 发现当前在播（GUI「发现当前在播」）
  python src/dy_api.py --cookie                       # 打印当前 cookie（含 ttwid）
  python src/dy_api.py --qualities 76274427953        # 打印清晰度列表（GUI 画质选择/排错用）
  python src/dy_api.py --stats                        # 打印本进程 HTTP 统计（请求/连接/流量/缓存）
  python src/dy_api.py --check                        # 查看解析缓存文件与条目数（落盘缓存诊断）
退出码：0=在播且取流成功  2=查询成功但未开播  1=查询失败（error 非空）
"""

from __future__ import annotations

import atexit
import http.client
import http.cookiejar
import json
import logging
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field

__version__ = "1.1.2"
__all__ = [
    "LiveStatus",
    "resolve_target",
    "get_live_status",
    "choose_stream",
    "get_qualities",
    "get_cookie",
    "fetch_recommend",
    "get_http_stats",
    "reset_http_stats",
    "close_connections",
    "flush_resolve_cache",
    "get_resolve_cache_info",
]

LOG = logging.getLogger("dyrec.dy_api")

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
COOKIE_FILE = os.path.join(DATA_DIR, "cookie.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

HOME_URL = "https://live.douyin.com/"
ENTER_API = "https://live.douyin.com/webcast/room/web/enter/"
FEED_API = "https://live.douyin.com/webcast/feed/"
REFLOW_API = "https://webcast.amemv.com/webcast/room/reflow/info/"
REFERER = "https://live.douyin.com/"

DEFAULT_TIMEOUT = 12.0          # get_live_status 默认整体超时（秒）
COOKIE_TTL = 1800               # 内存 cookie 有效期 30min
COOKIE_FILE_TTL = 12 * 3600     # data/cookie.json 兜底最长信任 12h

# --- HTTP 层（v1.1） ---
CONNECT_TIMEOUT = 5.0           # 建连/TLS 超时（秒）
READ_TIMEOUT = 10.0             # 单次读取超时（秒）
MAX_RESPONSE_BYTES = 2 * 1024 * 1024   # 单个响应上限 2MB
_ACCEPT_ENCODING = "gzip, deflate"     # 服务端实测支持 gzip；deflate 做兼容

# enter / feed 接口公共参数（实测可用，缺项可能被风控）
BASE_PARAMS = {
    "aid": "6383",
    "app_name": "douyin_web",
    "live_id": "1",
    "device_platform": "web",
    "language": "zh-CN",
    "enter_from": "web_live",
    "cookie_enabled": "true",
    "screen_width": "1920",
    "screen_height": "1080",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "138.0.0.0",
}

# reflow/info（app 侧接口）参数；未签名实测可用
REFLOW_PARAMS = {
    "verifyFp": "verify_hwj52020_7szNlAB7_pxNY_48Vh_ALKF_GA1Uf3yteoOY",
    "type_id": "0",
    "live_id": "1",
    "version_code": "99.99.99",
    "app_id": "1128",
}

# 主播主页链接（只有 sec_uid）兜底解析：sec_uid -> 抖音号(unique_id) -> enter -> reflow -> 数字 web_rid
USER_INFO_API = "https://www.iesdouyin.com/web/api/v2/user/info/"
PROFILE_API = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
PROFILE_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "version_code": "190500",
    "version_name": "19.5.0",
    "cookie_enabled": "true",
    "platform": "PC",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "138.0.0.0",
    "screen_width": "1920",
    "screen_height": "1080",
}
SEC_UID_CACHE_TTL = 12 * 3600    # 主播主页 sec_uid -> 数字 web_rid 映射缓存 12h（web_rid 基本终身不变）
SEC_UID_ALIAS_TTL = 600         # reflow 临时失败退回抖音号别名时只缓存 10min，之后重试拿数字 web_rid
NEGATIVE_CACHE_TTL = 600        # 「明确无直播间/无抖音号」负缓存 10min（可改；期间重复查询不发请求）

# --- 解析缓存落盘（v1.1.2）---
RESOLVE_CACHE_VERSION = 1
RESOLVE_CACHE_SAVE_INTERVAL = 10.0   # 落盘去抖：最多 10s 写一次（可改，测试会调小）
RESOLVE_CACHE_ENV = "DYREC_RESOLVE_CACHE"   # 环境变量覆盖缓存文件路径（测试用）

# flv_pull_url / hls_pull_url_map 里的档位，从高到低
QUALITY_ORDER = ("ORIGIN", "FULL_HD1", "HD1", "SD1", "SD2", "LD1", "LD2")
# live_core_sdk_data 里的 sdk_key，从高到低（origin=原画）
SDK_QUALITY_ORDER = ("origin", "uhd", "hd", "sd", "ld", "md", "ao")

# choose_stream(quality=...) 的候选名（按顺序尝试：先老档位键，再 sdk_key/中文名）。
# 说明：不同房间 uhd/origin 的语义并不统一，所以这里只做"名称→候选"的容错，
# 真正的最高档由 get_qualities/choose_stream(best) 依据 resolution×bitrate 现算，不硬编码语义。
_QUALITY_CANDIDATES = {
    "origin": ("ORIGIN", "origin", "原画", "or4"),
    "原画": ("ORIGIN", "origin", "原画", "or4"),
    "uhd": ("FULL_HD1", "uhd", "蓝光", "full_hd1"),
    "蓝光": ("uhd", "蓝光", "FULL_HD1"),
    "full_hd1": ("FULL_HD1", "uhd", "蓝光"),
    "hd": ("HD1", "hd", "超清"),
    "hd1": ("HD1", "hd", "超清"),
    "超清": ("hd", "超清", "HD1"),
    # 注意：实测 SD1/SD2 这两个老档位键与 sdk_key 的语义可能交叉（不同房间不同），
    # 所以 GUI 用的 "sd"（标清 540p）优先走 sdk_key ld；"高清"优先走 sdk_key sd。
    "sd": ("SD1", "ld", "标清"),
    "sd1": ("SD1", "sd", "高清"),
    "sd2": ("SD2", "sd", "高清"),
    "ld": ("ld", "标清", "SD2"),
    "标清": ("ld", "标清", "SD2"),
    "高清": ("sd", "高清", "SD1"),
    "ao": ("ao", "audio"),
}
# "best" 缺元数据时的回退顺序（origin→FULL_HD1→HD1→SD1→SD2→hls）
_FALLBACK_STREAM_KEYS = ("ORIGIN", "FULL_HD1", "HD1", "SD1", "SD2")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class LiveStatus:
    """一次直播状态查询结果。error 非空表示本次查询失败（网络/风控/房间不存在）。"""

    is_live: bool
    room_id: str = ""            # web_rid（live.douyin.com/<rid> 里的数字）
    title: str = ""
    anchor_name: str = ""
    cover_url: str = ""
    started_at: int = 0          # unix 秒，0=未知（enter 接口不返回开播时间，reflow 路径用 create_time）
    streams: dict = field(default_factory=dict)  # {"flv": [url...], "hls": [url...], "origin": {...}}
    raw: dict = field(default_factory=dict)      # 原始关键字段/flv_pull_url/hls_pull_url_map/qualities
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "is_live": self.is_live,
            "room_id": self.room_id,
            "title": self.title,
            "anchor_name": self.anchor_name,
            "cover_url": self.cover_url,
            "started_at": self.started_at,
            "streams": self.streams,
            "raw": self.raw,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# HTTP 基础设施
# --------------------------------------------------------------------------- #
def _make_ssl_context() -> ssl.SSLContext:
    """本机 TLS 证书链校验不可靠（见 docs/ENV.md），抖音接口实测关闭校验可正常访问。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL_CONTEXT = _make_ssl_context()


_STATS_LOCK = threading.Lock()
_HTTP_STATS = {
    "requests": 0,      # HTTP 请求数（含重试/重定向）
    "connects": 0,      # 新建 TCP+TLS 连接数
    "reconnects": 0,    # 网络异常后丢连接重试次数
    "redirects": 0,     # 30x 跳转次数
    "wire_bytes": 0,    # 线上实际收到的字节（压缩后）
    "decoded_bytes": 0, # 解压后的字节
    "errors": 0,        # 失败请求数
    "over_limit": 0,    # 超过 2MB 上限次数
    "negative_cache_hits": 0,  # 「明确无直播间」负缓存命中次数（省掉的解析请求）
    "cache_disk_hits": 0,      # 命中「从 data/resolve_cache.json 加载」的正缓存次数
    "cache_disk_saved": 0,     # 累计写入落盘缓存文件的条目次数
}
_thread_state = threading.local()


def reset_http_stats():
    """清零 HTTP 统计（压测/诊断用）。"""
    with _STATS_LOCK:
        for key in _HTTP_STATS:
            _HTTP_STATS[key] = 0


def get_http_stats() -> dict:
    """返回本进程累计 HTTP 统计（请求数/新建连接数/线上字节/解压后字节/错误数）。"""
    with _STATS_LOCK:
        return dict(_HTTP_STATS)


def _stat(key, amount=1):
    with _STATS_LOCK:
        _HTTP_STATS[key] = _HTTP_STATS.get(key, 0) + amount


def _conn_pool():
    """每线程一个 host -> HTTPSConnection 池（线程间不共享连接）。"""
    pool = getattr(_thread_state, "conns", None)
    if pool is None:
        pool = {}
        _thread_state.conns = pool
    return pool


def _drop_conn(key):
    conn = _conn_pool().pop(key, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def close_connections():
    """关闭当前线程所有 keep-alive 连接（线程退出/进程关闭前可调用）。"""
    for key in list(_conn_pool().keys()):
        _drop_conn(key)


def _get_conn(scheme, host, port):
    key = (scheme, host, port)
    pool = _conn_pool()
    conn = pool.get(key)
    if conn is not None and conn.sock is not None:
        return key, conn
    if conn is not None:
        pool.pop(key, None)
        try:
            conn.close()
        except Exception:
            pass
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=CONNECT_TIMEOUT, context=_SSL_CONTEXT)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
    pool[key] = conn
    return key, conn


class _ResponseTooLarge(Exception):
    """响应超过 MAX_RESPONSE_BYTES 上限。"""


def _decompress_body(raw, content_encoding):
    """按 Content-Encoding 解压（gzip / deflate；br 等不支持时原样返回）。"""
    enc = (content_encoding or "").strip().lower()
    if not enc or enc == "identity":
        return raw
    if "gzip" in enc:
        try:
            return zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        except Exception:
            try:
                return zlib.decompress(raw, zlib.MAX_WBITS | 32)  # 自动识别 gzip/zlib
            except Exception:
                return raw
    if "deflate" in enc:
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS, 16 + zlib.MAX_WBITS):
            try:
                return zlib.decompress(raw, wbits)
            except Exception:
                continue
        return raw
    return raw


def _read_body(resp):
    """分块读响应体，超过 2MB 上限抛 _ResponseTooLarge（防止恶意/异常大包）。"""
    chunks = []
    total = 0
    while True:
        piece = resp.read(65536)
        if not piece:
            break
        total += len(piece)
        if total > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge(total)
        chunks.append(piece)
    return b"".join(chunks), total


def _http_once(url, headers, timeout, cookie):
    """在 keep-alive 连接上发一次 GET。异常向上抛，由 _request 决定重建连接/重试。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("不支持的 URL scheme: %s" % parsed.scheme)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    key, conn = _get_conn(parsed.scheme, host, port)
    if conn.sock is None:
        conn.timeout = max(1.0, min(CONNECT_TIMEOUT, timeout))
        try:
            conn.connect()
        except Exception:
            _drop_conn(key)
            raise
        _stat("connects")
    try:
        conn.sock.settimeout(max(1.0, min(READ_TIMEOUT, timeout)))
    except Exception:
        pass
    hdrs = dict(headers)
    hdrs.setdefault("Accept-Encoding", _ACCEPT_ENCODING)
    hdrs.setdefault("Connection", "keep-alive")
    _stat("requests")
    conn.request("GET", target, headers=hdrs)
    resp = conn.getresponse()
    try:
        raw, wire = _read_body(resp)
    except _ResponseTooLarge:
        _stat("over_limit")
        _drop_conn(key)
        raise
    body = _decompress_body(raw, resp.getheader("Content-Encoding"))
    if len(body) > MAX_RESPONSE_BYTES:
        _stat("over_limit")
        _drop_conn(key)
        raise _ResponseTooLarge(len(body))
    _stat("wire_bytes", wire)
    _stat("decoded_bytes", len(body))
    try:
        set_cookie = list(resp.headers.get_all("Set-Cookie") or [])
    except Exception:
        set_cookie = []
    result = {
        "ok": 200 <= resp.status < 300,
        "status": resp.status,
        "body": body,
        "encoding": resp.getheader("Content-Encoding") or "",
        "wire_bytes": wire,
        "location": resp.getheader("Location"),
        "set_cookie": set_cookie,
    }
    if resp.will_close:
        _drop_conn(key)
    return result


def _request(url, *, headers=None, timeout=12.0, cookie=None):
    """发 GET（跟随最多 5 次 30x），永不抛异常。

    返回 dict(ok/status/body/final_url/hops/error)。
    v1.1：每线程 keep-alive 复用 TLS 连接；请求 gzip/deflate；网络异常丢弃连接重试一次。
    """
    hdrs = {
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "application/json, text/plain, */*",
        "Referer": REFERER,
    }
    if cookie:
        hdrs["Cookie"] = cookie
    if headers:
        hdrs.update({k: v for k, v in headers.items() if v is not None})
    try:
        timeout = float(timeout)
    except Exception:
        timeout = 12.0
    timeout = max(1.0, timeout)
    hops = []
    current = url
    for _hop in range(6):
        resp = None
        for attempt in range(2):
            try:
                resp = _http_once(current, hdrs, timeout, cookie)
                break
            except _ResponseTooLarge as exc:
                _stat("errors")
                got = (exc.args[0] if exc.args else 0) / 1048576.0
                return {
                    "ok": False, "status": -1, "body": b"", "final_url": current, "hops": hops,
                    "set_cookie": [],
                    "error": "响应超过 %.1fMB 上限（已读 %.1fMB），已中止"
                             % (MAX_RESPONSE_BYTES / 1048576.0, got),
                }
            except Exception as exc:
                close_connections()  # 丢弃本线程坏连接
                if attempt == 0:
                    _stat("reconnects")
                    time.sleep(0.05)
                    continue
                _stat("errors")
                return {
                    "ok": False, "status": -1, "body": b"", "final_url": current, "hops": hops,
                    "set_cookie": [],
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
        if resp is None:
            return {"ok": False, "status": -1, "body": b"", "final_url": current,
                    "hops": hops, "set_cookie": [], "error": "请求失败"}
        status = resp["status"]
        if status in (301, 302, 303, 307, 308) and resp.get("location"):
            _stat("redirects")
            nxt = urllib.parse.urljoin(current, resp["location"])
            hops.append({"code": status, "from": current, "to": nxt})
            current = nxt
            continue
        body = resp["body"]
        return {
            "ok": resp["ok"],
            "status": status,
            "body": body,
            "final_url": current,
            "hops": hops,
            "set_cookie": resp.get("set_cookie") or [],
            "error": "" if resp["ok"] else ("HTTP %s %s" % (status, _body_snippet(body))),
        }
    _stat("errors")
    return {"ok": False, "status": -1, "body": b"", "final_url": current,
            "hops": hops, "set_cookie": [], "error": "重定向次数过多(>5)"}


def _body_snippet(body, limit=120):
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        return ""
    text = " ".join(text.split())
    return text[:limit]


def _remaining(deadline, floor=2.0):
    """距离整体超时还剩多少秒（至少 floor）。"""
    try:
        return max(float(floor), float(deadline) - time.monotonic())
    except Exception:
        return float(floor)


# --------------------------------------------------------------------------- #
# cookie（ttwid）
# --------------------------------------------------------------------------- #
_COOKIE_LOCK = threading.RLock()
_cookie_state = {"cookies": {}, "obtained_at": 0.0}
_cookie_last_error = ""


def _load_cookie_file(ignore_age=False):
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    cookies = data.get("cookies") if isinstance(data.get("cookies"), dict) else data
    if not isinstance(cookies, dict) or not cookies.get("ttwid"):
        return {}
    if not ignore_age:
        try:
            age = time.time() - float(data.get("obtained_at") or 0)
        except Exception:
            age = COOKIE_FILE_TTL + 1
        if age > COOKIE_FILE_TTL:
            return {}
    return {str(k): str(v) for k, v in cookies.items() if v}


def _save_cookie_file(cookies):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = COOKIE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(
                {"ttwid": cookies.get("ttwid", ""), "cookies": cookies,
                 "obtained_at": int(time.time()), "source": HOME_URL},
                fp, ensure_ascii=False, indent=1,
            )
        os.replace(tmp, COOKIE_FILE)
    except Exception:
        pass


def _fetch_cookies(timeout=10.0):
    """GET live.douyin.com 首页，从 Set-Cookie 取 ttwid（+ UIFID_TEMP）。

    v1.1：优先走 keep-alive 连接（复用已有 TLS；新建连接被风控 444 时复用连接往往仍可用），
    失败再回退 urllib + CookieJar（首次启动 / 极端情况）。
    """
    global _cookie_last_error
    cookies = {}
    try:
        resp = _request(
            HOME_URL,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=max(2.0, float(timeout)),
        )
        for raw in resp.get("set_cookie") or []:
            try:
                name, _, value = raw.split(";", 1)[0].partition("=")
                name, value = name.strip(), value.strip()
                if name and value:
                    cookies[name] = value
            except Exception:
                pass
        if cookies.get("ttwid"):
            return cookies
        _cookie_last_error = "首页响应未下发 ttwid（疑似风控）"
    except Exception as exc:
        _cookie_last_error = "%s: %s" % (type(exc).__name__, exc)

    # 回退：urllib + CookieJar（老路径，兼容 cookie 解析细节）
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=_SSL_CONTEXT),
        )
        opener.addheaders = [
            ("User-Agent", UA),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "zh-CN,zh;q=0.9"),
        ]
        with opener.open(urllib.request.Request(HOME_URL, headers={"User-Agent": UA}),
                         timeout=max(2.0, float(timeout))) as resp:
            resp.read(256 * 1024)  # Set-Cookie 在响应头里，读一部分即可
        fallback = {c.name: c.value for c in cj if c.name and c.value}
        if fallback.get("ttwid"):
            _cookie_last_error = ""
            return fallback
        if not cookies:
            _cookie_last_error = "首页响应未下发 ttwid（疑似风控）"
    except Exception as exc:
        _cookie_last_error = "%s: %s" % (type(exc).__name__, exc)
    return cookies


def get_cookie(force: bool = False) -> dict:
    """返回可复用的 cookie dict（主要是 ttwid）。

    优先级：内存缓存 -> data/cookie.json -> 现场 GET 首页。失败时返回 {}（不抛异常），
    具体原因见 get_cookie_error()。
    """
    global _cookie_last_error
    with _COOKIE_LOCK:
        now = time.time()
        cached = _cookie_state["cookies"] or {}
        if cached.get("ttwid") and not force:
            if now - float(_cookie_state["obtained_at"] or 0) < COOKIE_TTL:
                return dict(cached)
        if not force:
            disk = _load_cookie_file()
            if disk:
                _cookie_state.update(cookies=disk, obtained_at=now)
                return dict(disk)
        cookies = _fetch_cookies(timeout=10.0)
        if cookies.get("ttwid"):
            _cookie_state.update(cookies=cookies, obtained_at=now)
            _cookie_last_error = ""
            _save_cookie_file(cookies)
            return dict(cookies)
        # 现场取失败：退回磁盘旧 cookie（可能仍可用）
        disk = _load_cookie_file(ignore_age=True)
        if disk:
            return disk
        return {}


def get_cookie_error() -> str:
    """最近一次取 cookie 失败的原因（成功时为空串）。"""
    return _cookie_last_error


def _cookie_to_header(cookie) -> str:
    if not cookie:
        return ""
    if isinstance(cookie, str):
        return cookie.strip()
    if isinstance(cookie, dict):
        return "; ".join("%s=%s" % (k, v) for k, v in cookie.items() if v)
    return ""


def _cookie_header(force=False, override=None):
    if override:
        return override
    return _cookie_to_header(get_cookie(force=force))


# --------------------------------------------------------------------------- #
# JSON 接口调用（带重试 + 风控刷新 ttwid）
# --------------------------------------------------------------------------- #
def _api_json(url, *, timeout=12.0, referer=REFERER, retries=1, cookie_override=None):
    """GET 一个 JSON 接口。返回 (dict|None, err)。

    空响应/非 JSON/连接失败会重试（重试时强制刷新 ttwid），最多 retries+1 次。
    """
    last = ""
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(0.25 + random.uniform(0.0, 0.35))
        cookie = _cookie_header(force=(attempt > 0), override=cookie_override)
        resp = _request(
            url,
            headers={"Accept": "application/json, text/plain, */*", "Referer": referer},
            timeout=max(1.0, float(timeout)),
            cookie=cookie,
        )
        if resp["ok"] and resp["body"]:
            try:
                return json.loads(resp["body"]), ""
            except Exception:
                last = "响应非 JSON（疑似风控/跳登录）: %s" % _body_snippet(resp["body"])
                if not cookie_override:
                    last += "；ttwid=%s" % ("有" if _cookie_state["cookies"].get("ttwid") else "无")
        elif resp["ok"]:
            last = "空响应（疑似风控）"
        else:
            last = resp["error"]
    return None, last


# --------------------------------------------------------------------------- #
# 目标解析（房间号 / 链接 / 短链 / 分享文案）
# --------------------------------------------------------------------------- #
_URL_RE = re.compile(r"https?://[^\s\"'<>，。！？、；：（）【】《》\u3000]+", re.I)
_URL_NO_SCHEME_RE = re.compile(
    r"(?:live|v|webcast|www)\.(?:douyin|iesdouyin)\.com/[^\s\"'<>，。！？、；：（）【】《》\u3000]+", re.I
)
_REFLOW_PATH_RE = re.compile(r"/(?:douyin/webcast/)?reflow/(\d{5,})", re.I)
_SEC_UID_PATH_RE = re.compile(r"/(?:share/)?user/([A-Za-z0-9_\-]{20,})")
_NUM_ONLY_RE = re.compile(r"^(\d{4,20})$")
_ROOM_NO_RE = re.compile(r"(?:房间号|直播号|房间ID|web_rid)[:：\s=]*(\d{4,20})", re.I)


def _parse_target_url(url: str) -> dict:
    """从单个 URL 里抽取 web_rid / room_id(19位) / sec_uid。"""
    out = {}
    if not url:
        return out
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return out
    path = parsed.path or ""
    host = (parsed.netloc or "").lower()
    query = urllib.parse.parse_qs(parsed.query or "")
    if query.get("web_rid"):
        out["web_rid"] = query["web_rid"][0]
    if query.get("sec_user_id"):
        out["sec_uid"] = query["sec_user_id"][0]
    match = _REFLOW_PATH_RE.search(path)
    if match:
        out["room_id"] = match.group(1)
    if host.endswith("live.douyin.com"):
        match = re.match(r"^/(\d{4,20})/?$", path)
        if match:
            out["web_rid"] = match.group(1)
    if host.endswith("iesdouyin.com") or host.endswith("douyin.com"):
        match = _SEC_UID_PATH_RE.search(path)
        if match:
            out["sec_uid"] = match.group(1)
    return out


def _follow_short_url(url, deadline, cookie_override=None):
    """请求短链，跟随 30x，从跳转链 + 最终 URL 里解析目标。返回 (info, err)。"""
    cookie = _cookie_header(override=cookie_override)
    resp = _request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": REFERER,
        },
        timeout=_remaining(deadline, 3.0),
        cookie=cookie,
    )
    candidates = []
    for hop in resp.get("hops") or []:
        if hop.get("to"):
            candidates.append(hop["to"])
    if resp.get("final_url"):
        candidates.append(resp["final_url"])
    info = {}
    for item in candidates:
        info.update(_parse_target_url(item))
    if not candidates:
        return {}, resp.get("error") or "短链无跳转"
    return info, ""


def _reflow_url(room_id, sec_uid):
    params = dict(REFLOW_PARAMS)
    params["room_id"] = str(room_id)
    params["sec_user_id"] = sec_uid or ""
    return REFLOW_API + "?" + urllib.parse.urlencode(params)


def _fetch_reflow(room_id, sec_uid, deadline, cookie_override=None):
    """reflow/info：把 19 位 room_id(+sec_uid) 换成 web_rid / 当前直播状态。"""
    data, err = _api_json(
        _reflow_url(room_id, sec_uid),
        timeout=_remaining(deadline, 3.0),
        referer="https://webcast.amemv.com/",
        retries=2,   # reflow 偶发 status_code=10033 "forbidden / Server upgrading"，多重试一次
        cookie_override=cookie_override,
    )
    if data is None:
        return None, "reflow 接口请求失败: %s" % err
    payload = data.get("data") or {}
    room = payload.get("room") or {}
    if not isinstance(room, dict) or not room:
        code = data.get("status_code")
        message = data.get("message") or ((payload.get("message") if isinstance(payload, dict) else "") or "")
        return None, "reflow 无房间数据 (status_code=%s%s)" % (code, ("，" + message) if message else "")
    return {"room": room, "user": payload.get("user") or {}, "room_id": str(room_id), "sec_uid": sec_uid}, ""


_RESOLVE_LOCK = threading.RLock()
_sec_uid_cache = {}      # sec_uid -> {"web_rid","unique_id","room_id","nickname","web_rid_kind","ttl","obtained_at"}
_sec_uid_neg_cache = {}  # sec_uid -> {"error","obtained_at"}：明确无直播间/无抖音号，10min 内直接返回同一 error


def _sec_uid_cache_get(sec_uid):
    _ensure_disk_cache_loaded()   # 首次使用时加载 data/resolve_cache.json（重启后免冷启动）
    with _RESOLVE_LOCK:
        item = _sec_uid_cache.get(sec_uid)
        if not item:
            return None
        item = dict(item)
    try:
        ttl = float(item.get("ttl") or SEC_UID_CACHE_TTL)
    except Exception:
        ttl = SEC_UID_CACHE_TTL
    if time.time() - float(item.get("obtained_at") or 0) > ttl:
        return None
    if item.get("from_disk"):
        _stat("cache_disk_hits")
    return item


def _sec_uid_cache_put(sec_uid, data):
    payload = dict(data)
    payload["obtained_at"] = time.time()
    payload.pop("from_disk", None)
    with _RESOLVE_LOCK:
        _sec_uid_cache[sec_uid] = payload
    # 落盘（去抖）：重启后仍可复用，避免 1000+ 主播每次重启都重新冷启动
    try:
        _disk_cache_put(sec_uid, payload)
    except Exception as exc:   # 落盘失败不影响解析/录制
        LOG.warning("解析缓存落盘异常（忽略）: %s: %s", type(exc).__name__, exc)


def _sec_uid_neg_get(sec_uid):
    """负缓存命中返回 {"error":...}；过期/不存在返回 None。"""
    with _RESOLVE_LOCK:
        item = _sec_uid_neg_cache.get(sec_uid)
        if not item:
            return None
        item = dict(item)
    try:
        ttl = float(item.get("ttl") or NEGATIVE_CACHE_TTL)
    except Exception:
        ttl = NEGATIVE_CACHE_TTL
    if time.time() - float(item.get("obtained_at") or 0) > ttl:
        with _RESOLVE_LOCK:
            _sec_uid_neg_cache.pop(sec_uid, None)
        return None
    return item


def _sec_uid_neg_put(sec_uid, error):
    with _RESOLVE_LOCK:
        _sec_uid_neg_cache[sec_uid] = {
            "error": str(error or ""),
            "obtained_at": time.time(),
            "ttl": NEGATIVE_CACHE_TTL,
        }
    # 负缓存不落盘；若磁盘里还留着这个 sec_uid 的旧正缓存，顺手删掉避免重启后误用
    try:
        _disk_cache_drop(sec_uid)
    except Exception:
        pass


def _sec_uid_neg_drop(sec_uid):
    with _RESOLVE_LOCK:
        _sec_uid_neg_cache.pop(sec_uid, None)


# --------------------------------------------------------------------------- #
# 解析缓存落盘：data/resolve_cache.json（v1.1.2）
#   {"version":1,"updated_at":ts,"entries":{sec_uid:{"web_rid","ts","kind",...}}}
#   - 只落盘「正缓存」（拿到 web_rid）；负缓存只在内存（10min）
#   - 加载时按 TTL 过滤（数字 web_rid 12h / 别名 10min），损坏文件容错
#   - 写入去抖（默认最多 10s 一次）+ tmp/os.replace 原子写，失败只 warning
# --------------------------------------------------------------------------- #
_DISK_LOCK = threading.RLock()
_disk_cache = {
    "path": "",
    "loaded": False,
    "dirty": False,
    "entries": {},       # sec_uid -> {"web_rid","ts","kind","unique_id","room_id","nickname"}
    "load_error": "",
    "dropped_expired": 0,   # 本次加载时丢弃的过期/非法条目数（--check 展示）
}
_disk_save_timer = None


def _resolve_cache_path():
    """解析缓存路径：环境变量 DYREC_RESOLVE_CACHE 可覆盖（测试用），默认 data/resolve_cache.json。"""
    override = (os.environ.get(RESOLVE_CACHE_ENV) or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(DATA_DIR, "resolve_cache.json")


def _entry_ttl(kind):
    """落盘条目 TTL：数字 web_rid 12h；抖音号别名 10min。"""
    return SEC_UID_ALIAS_TTL if str(kind or "") in ("alias", "unique_id_alias") else SEC_UID_CACHE_TTL


def _disk_cache_reset(path=""):
    """测试/诊断用：重置落盘缓存状态（不删除文件）。"""
    global _disk_save_timer
    with _DISK_LOCK:
        _disk_cache.update(path=path or _resolve_cache_path(), loaded=False,
                           dirty=False, entries={}, load_error="", dropped_expired=0)
        timer, _disk_save_timer = _disk_save_timer, None
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass


def _ensure_disk_cache_loaded():
    """首次使用/路径变化时加载缓存文件。损坏/缺失文件不抛异常，只 warning。"""
    path = _resolve_cache_path()
    with _DISK_LOCK:
        if _disk_cache["loaded"] and _disk_cache["path"] == path:
            return
        had_dirty = bool(_disk_cache["dirty"])
        old_path = _disk_cache["path"]
    if had_dirty and old_path and old_path != path:
        try:
            _flush_disk_cache(force=True)
        except Exception:
            pass
    entries = {}
    load_error = ""
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
        payload_entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(payload_entries, dict):
            if raw is not None:
                load_error = "缓存文件格式异常（缺少 entries）"
                LOG.warning("解析缓存格式异常，忽略: %s", path)
        else:
            now = time.time()
            for sec_uid, rec in payload_entries.items():
                if not isinstance(rec, dict):
                    continue
                web_rid = str(rec.get("web_rid") or "")
                if not web_rid:
                    continue
                try:
                    ts = float(rec.get("ts") or 0)
                except Exception:
                    ts = 0.0
                kind = str(rec.get("kind") or "numeric")
                if ts <= 0 or (now - ts) > _entry_ttl(kind) or (now - ts) < -300:
                    skipped += 1
                    continue
                entries[str(sec_uid)] = {
                    "web_rid": web_rid, "ts": ts, "kind": kind,
                    "unique_id": str(rec.get("unique_id") or ""),
                    "room_id": str(rec.get("room_id") or ""),
                    "nickname": str(rec.get("nickname") or ""),
                }
            if skipped:
                LOG.info("解析缓存加载：%d 条有效，丢弃 %d 条过期/非法", len(entries), skipped)
    except FileNotFoundError:
        pass
    except Exception as exc:
        load_error = "%s: %s" % (type(exc).__name__, exc)
        LOG.warning("解析缓存加载失败（忽略，继续运行）: %s", exc)
    with _DISK_LOCK:
        _disk_cache.update(path=path, loaded=True, entries=entries,
                           load_error=load_error, dirty=False, dropped_expired=skipped)
    # 合并进内存正缓存（内存里已有更新的则保留内存值）
    for sec_uid, rec in entries.items():
        with _RESOLVE_LOCK:
            current = _sec_uid_cache.get(sec_uid)
            if current and float(current.get("obtained_at") or 0) >= rec["ts"]:
                continue
            _sec_uid_cache[sec_uid] = {
                "web_rid": rec["web_rid"],
                "unique_id": rec["unique_id"],
                "room_id": rec["room_id"],
                "nickname": rec["nickname"],
                "web_rid_kind": "numeric" if rec["kind"] == "numeric" else "unique_id_alias",
                "ttl": _entry_ttl(rec["kind"]),
                "obtained_at": rec["ts"],
                "from_disk": True,
            }


def _disk_cache_put(sec_uid, item):
    """把一个正缓存条目写进落盘缓存（内存 dict），并安排去抖落盘。"""
    _ensure_disk_cache_loaded()
    kind = "numeric" if item.get("web_rid_kind") == "numeric" else "unique_id_alias"
    rec = {
        "web_rid": str(item.get("web_rid") or ""),
        "ts": time.time(),
        "kind": kind,
    }
    if not rec["web_rid"]:
        return
    for key in ("unique_id", "room_id", "nickname"):
        if item.get(key):
            rec[key] = str(item[key])
    with _DISK_LOCK:
        _disk_cache["entries"][sec_uid] = rec
        _disk_cache["dirty"] = True
    _schedule_disk_save()


def _disk_cache_drop(sec_uid):
    """从落盘缓存里移除一个 sec_uid（解析结果变为无直播间时避免重启后误用旧值）。"""
    try:
        _ensure_disk_cache_loaded()
    except Exception:
        return
    with _DISK_LOCK:
        removed = _disk_cache["entries"].pop(sec_uid, None)
        if removed is not None:
            _disk_cache["dirty"] = True
    if removed is not None:
        _schedule_disk_save()


def _schedule_disk_save():
    """去抖落盘：最多 RESOLVE_CACHE_SAVE_INTERVAL 秒写一次文件。"""
    global _disk_save_timer
    with _DISK_LOCK:
        if _disk_save_timer is not None and _disk_save_timer.is_alive():
            return
        try:
            interval = max(0.05, float(RESOLVE_CACHE_SAVE_INTERVAL or 10.0))
        except Exception:
            interval = 10.0
        timer = threading.Timer(interval, _flush_disk_cache)
        timer.daemon = True
        _disk_save_timer = timer
        timer.start()


def _flush_disk_cache(force=False):
    """原子落盘（tmp + os.replace）。失败只 warning，保留 dirty 稍后重试。返回 bool。"""
    global _disk_save_timer
    tmp = ""
    with _DISK_LOCK:
        _disk_save_timer = None
        if not force and not _disk_cache["dirty"]:
            return True
        entries = {k: dict(v) for k, v in _disk_cache["entries"].items()}
        path = _disk_cache["path"] or _resolve_cache_path()
        _disk_cache["dirty"] = False
    payload = {"version": RESOLVE_CACHE_VERSION, "updated_at": time.time(), "entries": entries}
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        _stat("cache_disk_saved", len(entries))
        return True
    except Exception as exc:
        with _DISK_LOCK:
            _disk_cache["dirty"] = True
        LOG.warning("解析缓存落盘失败（忽略，不影响录制）: %s: %s", type(exc).__name__, exc)
        if tmp:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
        _schedule_disk_save()
        return False


def flush_resolve_cache():
    """立即把解析缓存落盘（测试/退出前可调用）。返回 True/False，不抛异常。"""
    try:
        return _flush_disk_cache(force=True)
    except Exception:
        return False


def get_resolve_cache_info():
    """--check 用：返回缓存文件路径/条目数/有效条目/内存缓存统计（不打印敏感信息）。"""
    _ensure_disk_cache_loaded()
    path = _resolve_cache_path()
    with _DISK_LOCK:
        entries = {k: dict(v) for k, v in _disk_cache["entries"].items()}
        load_error = _disk_cache["load_error"]
        dirty = _disk_cache["dirty"]
        dropped_expired = int(_disk_cache.get("dropped_expired") or 0)
    with _RESOLVE_LOCK:
        mem_positive = len(_sec_uid_cache)
        mem_negative = len(_sec_uid_neg_cache)
    now = time.time()
    valid_numeric = valid_alias = 0
    expired = dropped_expired
    newest = oldest = 0.0
    for rec in entries.values():
        try:
            ts = float(rec.get("ts") or 0)
        except Exception:
            ts = 0.0
        if not ts or (now - ts) > _entry_ttl(rec.get("kind")):
            expired += 1
            continue
        if str(rec.get("kind") or "") == "numeric":
            valid_numeric += 1
        else:
            valid_alias += 1
        if ts > newest:
            newest = ts
        if not oldest or ts < oldest:
            oldest = ts
    exists = os.path.exists(path)
    return {
        "cache_file": path,
        "env_override": os.environ.get(RESOLVE_CACHE_ENV) or "",
        "exists": exists,
        "size_bytes": os.path.getsize(path) if exists else 0,
        "entries_total": len(entries),
        "entries_valid": valid_numeric + valid_alias,
        "entries_valid_numeric": valid_numeric,
        "entries_valid_alias": valid_alias,
        "entries_expired": expired,
        "newest_ts": round(newest, 3),
        "oldest_ts": round(oldest, 3),
        "in_memory_positive": mem_positive,
        "in_memory_negative": mem_negative,
        "negative_persisted": False,
        "dirty_pending": bool(dirty),
        "load_error": load_error,
        "version": RESOLVE_CACHE_VERSION,
    }


def _flush_on_exit():
    """进程退出前把未落盘的解析缓存写掉（没 dirty 就不写，避免无谓创建文件）。"""
    try:
        _flush_disk_cache(force=False)
    except Exception:
        pass


atexit.register(_flush_on_exit)


def _fetch_user_info(sec_uid, deadline, cookie_override=None):
    """sec_uid -> (抖音号 unique_id, 昵称, definitive)。两个候选接口实测都**不需要签名**。

    definitive=True 表示「接口明确返回了账号资料，但该账号确实没有抖音号」
    → 属于「明确无直播间/无法解析」结果，可以进 10min 负缓存；
    definitive=False 表示请求失败/结果不确定（网络/风控），不得负缓存。
    """
    unique_id = ""
    nickname = ""
    definitive = False
    # 1) iesdouyin 旧分享接口（响应小、稳定）
    url = USER_INFO_API + "?" + urllib.parse.urlencode({"sec_uid": sec_uid})
    data, _err = _api_json(
        url,
        timeout=_remaining(deadline, 3.0),
        referer="https://www.iesdouyin.com/",
        retries=1,
        cookie_override=cookie_override,
    )
    info = (data or {}).get("user_info") if isinstance(data, dict) else None
    if isinstance(info, dict):
        unique_id = str(info.get("unique_id") or "")
        nickname = str(info.get("nickname") or "")
        if not unique_id:
            definitive = True   # 接口明确返回资料，但没有 unique_id
    # 2) 兜底：douyin 网页版 profile 接口（用户资料，实测未签名也返回数据）
    if not unique_id:
        params = dict(PROFILE_PARAMS)
        params["sec_user_id"] = sec_uid
        url2 = PROFILE_API + "?" + urllib.parse.urlencode(params)
        data2, _err2 = _api_json(
            url2,
            timeout=_remaining(deadline, 3.0),
            referer="https://www.douyin.com/",
            retries=1,
            cookie_override=cookie_override,
        )
        user = (data2 or {}).get("user") if isinstance(data2, dict) else None
        if isinstance(user, dict):
            unique_id = str(user.get("unique_id") or "")
            nickname = nickname or str(user.get("nickname") or "")
            definitive = not unique_id
        # 两个接口都没有有效 JSON -> definitive 保持 False（transient）
    return unique_id, nickname, definitive


def _resolve_sec_uid(sec_uid, deadline, cookie_override=None):
    """主播主页 sec_uid -> 数字 web_rid + 预取直播状态。未签名可用。

    链路（2026-11-13 实测）：
      sec_uid --user/info(未签名)--> unique_id(抖音号)
             --enter(web_rid=<抖音号>，enter 接口接受抖音号别名)--> 19 位 room_id + 当前状态
             --reflow/info(room_id, sec_uid)--> owner.web_rid（数字）
    离线主播也适用（enter/reflow 都能返回“最后一场”的 room 与 owner.web_rid）；
    reflow 偶发 10033 时会重试，仍失败则退回抖音号别名（enter 同样可用）。
    返回 (info|None, prefetched_status|None, err)。
    """
    cached = _sec_uid_cache_get(sec_uid)
    if cached and cached.get("web_rid"):
        return cached, None, ""
    # 负缓存：10min 内「明确无直播间/无抖音号」直接返回同一 error，不再发解析请求
    negative = _sec_uid_neg_get(sec_uid)
    if negative is not None:
        _stat("negative_cache_hits")
        return None, None, negative.get("error") or ""
    unique_id, nickname, definitive = _fetch_user_info(sec_uid, deadline, cookie_override=cookie_override)
    if not unique_id:
        error = "无法从 sec_uid 解析出抖音号（unique_id），该主播可能未设置抖音号或接口受限"
        if definitive:
            _sec_uid_neg_put(sec_uid, error)
        return None, None, error
    enter_status, enter_err = _fetch_enter(str(unique_id), deadline, cookie_override=cookie_override)
    if enter_status is None:
        return None, None, "按抖音号(%s) enter 查询失败: %s" % (unique_id, enter_err)
    room_id = str((enter_status.raw or {}).get("room_id") or "")
    web_rid = ""
    reflow_status = None
    if room_id:
        reflow_status, _reflow_err = _status_from_reflow(
            room_id, sec_uid, deadline, cookie_override=cookie_override
        )
        if reflow_status is not None:
            web_rid = str((reflow_status.raw or {}).get("web_rid") or "")
    info = {
        "web_rid": web_rid or str(unique_id),
        "unique_id": str(unique_id),
        "room_id": room_id,
        "nickname": nickname or (reflow_status.anchor_name if reflow_status else "") or enter_status.anchor_name,
        "web_rid_kind": "numeric" if web_rid else "unique_id_alias",
    }
    if not web_rid:
        info["note"] = "reflow 未返回数字 web_rid，退用抖音号别名查询（enter 接口可用，但非规范值）"
    _sec_uid_neg_drop(sec_uid)   # 解析成功（拿到直播间）后负缓存立即失效
    cache_payload = {k: info[k] for k in ("web_rid", "unique_id", "room_id", "nickname", "web_rid_kind")}
    cache_payload["ttl"] = SEC_UID_CACHE_TTL if web_rid else SEC_UID_ALIAS_TTL
    _sec_uid_cache_put(sec_uid, cache_payload)
    prefetched = None
    if reflow_status is not None and str((reflow_status.raw or {}).get("web_rid") or "") == info["web_rid"]:
        prefetched = reflow_status
    elif enter_status is not None:
        prefetched = enter_status
    return info, prefetched, ""


def _resolve_target_impl(text, deadline, cookie_override=None):
    """内部实现：解析目标，并尽量顺便预取一次直播状态（省一次请求）。

    返回 (result, prefetched_status|None)。result 结构见 resolve_target()。
    """
    result = {
        "web_rid": None, "sec_uid": None, "url": "", "room_id": None,
        "unique_id": None, "source": "unknown", "error": "",
    }
    prefetched = None
    if text is None:
        result["error"] = "目标为空"
        return result, prefetched
    if isinstance(text, dict):
        for key in ("web_rid", "sec_uid", "url", "room_id", "unique_id"):
            if text.get(key):
                result[key] = str(text[key])
        result["source"] = "dict"
        return result, prefetched
    raw = str(text).strip()
    if not raw:
        result["error"] = "目标为空"
        return result, prefetched

    # 1) 纯数字：短的是 web_rid，18~20 位大概率是 19 位 room_id
    match = _NUM_ONLY_RE.match(raw)
    if match:
        digits = match.group(1)
        if len(digits) >= 16:
            result.update(room_id=digits, source="room_id_numeric")
        else:
            result.update(web_rid=digits, source="web_rid_numeric",
                          url="https://live.douyin.com/%s" % digits)
        return result, prefetched

    # 2) 先把 URL 抠出来（分享文案里常夹带中文）
    url = ""
    match = _URL_RE.search(raw)
    if match:
        url = match.group(0).rstrip(".,;:!?)]}\"'")
    else:
        match = _URL_NO_SCHEME_RE.search(raw)
        if match:
            url = "https://" + match.group(0).rstrip(".,;:!?)]}\"'")
    if url:
        result["url"] = url
        info = _parse_target_url(url)
        # 短链（v.douyin.com）或不认识的链接 -> 跟 30x
        if not info.get("web_rid") and not info.get("room_id") and not info.get("sec_uid"):
            short_info, err = _follow_short_url(url, deadline)
            info.update(short_info)
            if not info:
                result["error"] = "短链解析失败: %s" % (err or "未跳转到直播间/主播页")
                return result, prefetched
        result.update({k: info.get(k) for k in ("web_rid", "sec_uid", "room_id") if info.get(k)})
        # 短链 302 给的是 19 位 room_id + sec_uid，需经 reflow 换成 web_rid
        if not result["web_rid"] and result["room_id"] and result["sec_uid"]:
            reflow, err = _fetch_reflow(result["room_id"], result["sec_uid"], deadline,
                                        cookie_override=cookie_override)
            if reflow:
                owner = reflow["room"].get("owner") or {}
                web_rid = str(owner.get("web_rid") or owner.get("short_id") or "")
                if web_rid:
                    result["web_rid"] = web_rid
                result["source"] = "short_link_reflow"
                prefetched = _build_status(
                    reflow["room"], reflow.get("user") or {}, web_rid=web_rid, source="reflow"
                )
            elif not result.get("web_rid"):
                result["source"] = "short_link_reflow_failed"
                result["error"] = err or "reflow 未返回 web_rid"
        elif not result["web_rid"] and result["sec_uid"]:
            # 电脑主播主页链接：只有 sec_uid，需要先换 web_rid
            resolved, status, rerr = _resolve_sec_uid(
                result["sec_uid"], deadline, cookie_override=cookie_override
            )
            if resolved:
                for key in ("web_rid", "unique_id", "room_id"):
                    if resolved.get(key):
                        result[key] = resolved[key]
                result["source"] = "user_profile_reflow"
                result["nickname"] = resolved.get("nickname")
                result["web_rid_kind"] = resolved.get("web_rid_kind")
                prefetched = status
            else:
                result["source"] = "user_profile"
                result["error"] = ("已解析到主播 sec_uid=%s，但没能解析出直播间 web_rid: %s"
                                   % (result["sec_uid"], rerr))
        elif result["web_rid"]:
            result["source"] = "url"
            result["url"] = result["url"] or "https://live.douyin.com/%s" % result["web_rid"]
        if not result["web_rid"] and not result["room_id"] and not result["sec_uid"]:
            result["error"] = "链接里没有房间号/主播信息"
        return result, prefetched

    # 3) 文本里的「房间号：12345678」
    match = _ROOM_NO_RE.search(raw)
    if match:
        result.update(web_rid=match.group(1), source="text_room_no",
                      url="https://live.douyin.com/%s" % match.group(1))
        return result, prefetched

    # 4) 兜底：文本里孤立的 5~15 位数字当 web_rid
    match = re.search(r"(?<!\d)(\d{5,15})(?!\d)", raw)
    if match:
        result.update(web_rid=match.group(1), source="text_digits",
                      url="https://live.douyin.com/%s" % match.group(1))
        return result, prefetched

    result["error"] = "无法识别的目标（支持房间号/直播间链接/v.douyin.com 短链/分享文案）"
    return result, prefetched


def resolve_target(text, timeout: float | None = None) -> dict:
    """把 房间号 / 直播间链接 / v.douyin.com 短链 / 分享文案 / 电脑主播主页链接 解析成目标信息。

    返回 {"web_rid": str|None, "sec_uid": str|None, "url": str,
          "room_id": str|None, "unique_id": str|None, "source": str, "error": str}
    - web_rid：live.douyin.com/<web_rid> 里的数字（可直接查直播状态）
    - room_id：19 位直播间 id（来自短链 302 / 主播主页解析，需要 sec_uid 才能走 reflow）
    - sec_uid：主播主页链接里解析出的用户 id
    - unique_id：抖音号（主播主页链接解析的中间结果）
    支持的主页链接：www.douyin.com/user/<sec_uid>、v.douyin.com 短链到主页。
    永不抛异常；解析失败时 error 非空。
    """
    try:
        deadline = time.monotonic() + max(4.0, float(timeout or 12.0))
    except Exception:
        deadline = time.monotonic() + 12.0
    try:
        result, _prefetched = _resolve_target_impl(text, deadline)
        return result
    except Exception as exc:  # 兜底：任何异常都转成 error 字符串
        return {
            "web_rid": None, "sec_uid": None, "url": "", "room_id": None,
            "unique_id": None, "source": "unknown",
            "error": "解析目标异常: %s: %s" % (type(exc).__name__, exc),
        }


# --------------------------------------------------------------------------- #
# 响应 -> LiveStatus
# --------------------------------------------------------------------------- #
def _first_url(obj):
    if not isinstance(obj, dict):
        return ""
    urls = obj.get("url_list") or []
    if isinstance(urls, list):
        for item in urls:
            if item:
                return str(item)
    return ""


def _ordered_map_keys(mapping):
    keys = [k for k in QUALITY_ORDER if mapping.get(k)]
    rest = [k for k in mapping.keys() if k not in QUALITY_ORDER and mapping.get(k)]
    return keys + rest


def _parse_resolution(value):
    """'1920x1080' -> (1920, 1080)；解析不出返回 (0, 0)。"""
    match = re.search(r"(\d{2,5})\s*[xX*×]\s*(\d{2,5})", str(value or ""))
    if match:
        try:
            return int(match.group(1)), int(match.group(2))
        except Exception:
            pass
    return 0, 0


def _quality_score(resolution, bitrate, level=0):
    """画质评分：分辨率面积 × 码率（缺失时按面积/码率/level 逐级回退）。"""
    width, height = resolution
    area = width * height
    try:
        rate = int(bitrate or 0)
    except Exception:
        rate = 0
    if area > 0 and rate > 0:
        return area * rate
    if area > 0:
        return area * 1000
    if rate > 0:
        return rate
    try:
        return int(level or 0)
    except Exception:
        return 0


def _extract_streams(stream_url):
    """从 room.stream_url 抽出流地址 + 清晰度元数据（v1.1：URL 只存一份，不保留原始 JSON）。

    返回 (streams, qualities, meta)：
      streams   = {"flv": [url...], "hls": [url...], "origin": {"flv","hls","codec"}}
      qualities = [{"name","sdk_key","resolution","bitrate","fps","flv_idx","hls_idx",
                    "map_key","score"}, ...]
      meta      = {"flv_pull_url_keys": [...], "hls_pull_url_keys": [...]}
    qualities 里的 flv_idx/hls_idx 是 streams 列表下标，避免同一 URL 在内存里存多份。
    """
    streams = {"flv": [], "hls": [], "origin": {}}
    qualities = []
    meta = {"flv_pull_url_keys": [], "hls_pull_url_keys": []}
    if not isinstance(stream_url, dict):
        return streams, qualities, meta

    # 1) live_core_sdk_data.pull_data.stream_data（原画/各档位，JSON 字符串）
    sdk_data = {}
    options = {}
    try:
        pull = (stream_url.get("live_core_sdk_data") or {}).get("pull_data") or {}
        options = pull.get("options") or {}
        stream_data = pull.get("stream_data") or ""
        if not stream_data:
            pull_datas = stream_url.get("pull_datas") or {}
            if pull_datas:
                stream_data = (pull_datas.get(next(iter(pull_datas))) or {}).get("stream_data") or ""
        if stream_data:
            sdk_data = json.loads(stream_data)
    except Exception:
        sdk_data = {}
        options = {}

    quality_meta = {}
    try:
        for item in options.get("qualities") or []:
            if isinstance(item, dict) and item.get("sdk_key"):
                quality_meta[str(item["sdk_key"])] = item
    except Exception:
        quality_meta = {}

    data = (sdk_data.get("data") or {}) if isinstance(sdk_data, dict) else {}
    flv_map = stream_url.get("flv_pull_url") if isinstance(stream_url.get("flv_pull_url"), dict) else {}
    hls_map = stream_url.get("hls_pull_url_map") if isinstance(stream_url.get("hls_pull_url_map"), dict) else {}
    meta["flv_pull_url_keys"] = _ordered_map_keys(flv_map)
    meta["hls_pull_url_keys"] = _ordered_map_keys(hls_map)

    flv_index, hls_index = {}, {}

    def _add_url(kind, url):
        if not url:
            return None
        url = str(url)
        index = flv_index if kind == "flv" else hls_index
        bucket = streams[kind]
        if url in index:
            return index[url]
        index[url] = len(bucket)
        bucket.append(url)
        return index[url]

    used_map_keys = set()

    # 2) live_core_sdk_data 各档位（含 origin），按 sdk_key 顺序 + 其余键
    keys = [k for k in SDK_QUALITY_ORDER if isinstance(data.get(k), dict)]
    keys += [k for k in data.keys() if k not in SDK_QUALITY_ORDER and isinstance(data.get(k), dict)]
    for key in keys:
        item = data.get(key)
        main = item.get("main") if isinstance(item, dict) else None
        if not isinstance(main, dict):
            continue
        flv_url = str(main.get("flv") or "")
        hls_url = str(main.get("hls") or "")
        if not flv_url and not hls_url:
            continue
        info = quality_meta.get(str(key)) or {}
        resolution_text = str(info.get("resolution") or "")
        if not resolution_text:
            try:
                params = json.loads(main.get("sdk_params") or "{}")
                resolution_text = str(params.get("resolution") or "")
            except Exception:
                resolution_text = ""
        bitrate = info.get("v_bit_rate") or 0
        fps = info.get("fps") or 0
        map_key = ""
        for name in _ordered_map_keys(flv_map):
            if flv_map.get(name) and str(flv_map[name]) == flv_url:
                map_key = name
                used_map_keys.add(("flv", name))
                break
        if not map_key:
            for name in _ordered_map_keys(hls_map):
                if hls_map.get(name) and str(hls_map[name]) == hls_url:
                    map_key = name
                    used_map_keys.add(("hls", name))
                    break
        qualities.append({
            "name": str(info.get("name") or ""),
            "sdk_key": str(key),
            "resolution": resolution_text,
            "bitrate": int(bitrate or 0),
            "fps": int(fps or 0),
            "flv_idx": _add_url("flv", flv_url),
            "hls_idx": _add_url("hls", hls_url),
            "map_key": map_key,
            "score": _quality_score(_parse_resolution(resolution_text), bitrate, info.get("level")),
        })

    # 3) flv_pull_url / hls_pull_url_map 里未被 sdk 数据覆盖的档位 → 补成 pseudo quality
    for kind, mapping in (("flv", flv_map), ("hls", hls_map)):
        for name in _ordered_map_keys(mapping):
            if (kind, name) in used_map_keys or not mapping.get(name):
                continue
            idx = _add_url(kind, mapping[name])
            if idx is None:
                continue
            merged = False
            for existing in qualities:
                if existing.get("map_key") == name:
                    existing["flv_idx" if kind == "flv" else "hls_idx"] = idx
                    merged = True
                    break
            if not merged:
                qualities.append({
                    "name": name, "sdk_key": "", "resolution": "", "bitrate": 0, "fps": 0,
                    "flv_idx": idx if kind == "flv" else None,
                    "hls_idx": idx if kind == "hls" else None,
                    "map_key": name, "score": 0,
                })

    # 4) origin 原画（data.origin.main）：复用 streams 里的同一 URL 字符串对象
    origin_main = ((data.get("origin") or {}).get("main") or {}) if isinstance(data.get("origin"), dict) else {}
    origin_flv = str(origin_main.get("flv") or "")
    origin_hls = str(origin_main.get("hls") or "")
    if origin_flv or origin_hls:
        origin = {}
        if origin_flv:
            origin["flv"] = streams["flv"][_add_url("flv", origin_flv)]
        if origin_hls:
            origin["hls"] = streams["hls"][_add_url("hls", origin_hls)]
        try:
            sdk_params = json.loads(origin_main.get("sdk_params") or "{}")
        except Exception:
            sdk_params = {}
        if sdk_params and sdk_params.get("VCodec"):
            origin["codec"] = sdk_params["VCodec"]
        streams["origin"] = origin

    return streams, qualities, meta


def _build_status(room, user=None, *, web_rid="", source="") -> LiveStatus:
    """把 room dict（enter 的 data.data[0] / feed 的 data / reflow 的 data.room）转成 LiveStatus。"""
    room = room if isinstance(room, dict) else {}
    user = user if isinstance(user, dict) else {}
    owner = room.get("owner") if isinstance(room.get("owner"), dict) else {}

    status = room.get("status")
    is_live = status == 2 or str(status) == "2"
    room_id19 = str(room.get("id_str") or room.get("id") or "")
    title = str(room.get("title") or "")
    anchor = str(owner.get("nickname") or user.get("nickname") or "")
    cover = _first_url(room.get("cover")) or _first_url(owner.get("avatar_thumb")) or _first_url(user.get("avatar_thumb"))
    started_at = 0
    try:
        started_at = int(room.get("create_time") or 0)
    except Exception:
        started_at = 0

    stream_url = room.get("stream_url") if isinstance(room.get("stream_url"), dict) else {}
    if is_live:
        streams, qualities, stream_meta = _extract_streams(stream_url)
    else:
        streams = {"flv": [], "hls": [], "origin": {}}
        qualities, stream_meta = [], {"flv_pull_url_keys": [], "hls_pull_url_keys": []}

    # v1.1 瘦身：不保留原始大 JSON；URL 只在 streams 里存一份，qualities 用下标引用。
    raw = {
        "source": source,
        "status": status,
        "web_rid": str(web_rid or ""),
        "room_id": room_id19,
        "sec_uid": str(owner.get("sec_uid") or user.get("sec_uid") or ""),
        "default_resolution": stream_url.get("default_resolution") if is_live else None,
        "flv_pull_url_keys": stream_meta["flv_pull_url_keys"],
        "hls_pull_url_keys": stream_meta["hls_pull_url_keys"],
        "qualities": qualities,
    }
    return LiveStatus(
        is_live=is_live,
        room_id=str(web_rid or room_id19 or ""),
        title=title,
        anchor_name=anchor,
        cover_url=cover,
        started_at=started_at,
        streams=streams,
        raw=raw,
        error="",
    )


# --------------------------------------------------------------------------- #
# 三个核心接口
# --------------------------------------------------------------------------- #
def _fetch_enter(web_rid, deadline, cookie_override=None):
    """enter 接口：按 web_rid 查状态 + 取流。返回 (LiveStatus|None, err)。"""
    params = dict(BASE_PARAMS)
    params["web_rid"] = str(web_rid)
    url = ENTER_API + "?" + urllib.parse.urlencode(params)
    data, err = _api_json(
        url,
        timeout=_remaining(deadline, 3.0),
        referer="https://live.douyin.com/%s" % web_rid,
        retries=1,
        cookie_override=cookie_override,
    )
    if data is None:
        return None, "enter 接口请求失败: %s" % err
    code = data.get("status_code")
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    rooms = payload.get("data") or []
    user = payload.get("user") or {}
    if not rooms:
        if code == 4001038 or str(code) == "4001038":
            return LiveStatus(is_live=False, room_id=str(web_rid),
                              error="房间不存在或 web_rid 非法 (status_code=4001038)"), ""
        return LiveStatus(is_live=False, room_id=str(web_rid),
                          error="enter 接口无房间数据 (status_code=%s)" % code), ""
    return _build_status(rooms[0], user, web_rid=str(web_rid), source="enter"), ""


def _status_from_reflow(room_id, sec_uid, deadline, cookie_override=None):
    """reflow 接口：19 位 room_id(+sec_uid) -> 当前直播状态（含 web_rid）。"""
    reflow, err = _fetch_reflow(room_id, sec_uid, deadline, cookie_override=cookie_override)
    if not reflow:
        return None, err
    room = reflow["room"]
    owner = room.get("owner") if isinstance(room.get("owner"), dict) else {}
    web_rid = str(owner.get("web_rid") or owner.get("short_id") or "")
    status = _build_status(room, reflow.get("user") or {}, web_rid=web_rid, source="reflow")
    if not web_rid:
        status.raw["hint"] = "reflow 未返回 owner.web_rid"
    return status, ""


def get_live_status(target, cookie: dict | str | None = None, timeout: float = 15) -> LiveStatus:
    """查询直播状态（唯一入口）。

    target 可为 web_rid、直播间 URL、v.douyin.com 短链、分享文案、电脑主播主页链接。
    cookie 可传 {"ttwid": "..."} 或 "ttwid=..." 字符串覆盖内部 cookie 缓存。
    永不抛异常：失败返回 LiveStatus(is_live=False, error="...")。
    """
    try:
        timeout = float(timeout)
    except Exception:
        timeout = DEFAULT_TIMEOUT
    timeout = max(4.0, timeout)
    deadline = time.monotonic() + timeout
    cookie_override = _cookie_to_header(cookie) if cookie else None

    prefetched = None
    try:
        if isinstance(target, dict):
            target_info = dict(target)
        else:
            target_info, prefetched = _resolve_target_impl(target, deadline, cookie_override=cookie_override)
    except Exception as exc:  # 理论上解析不抛，兜底
        return LiveStatus(is_live=False, error="解析目标异常: %s: %s" % (type(exc).__name__, exc))

    if not isinstance(target_info, dict):
        return LiveStatus(is_live=False, error="目标解析结果异常")

    web_rid = str(target_info.get("web_rid") or "")
    room_id = str(target_info.get("room_id") or "")
    sec_uid = str(target_info.get("sec_uid") or "")

    # A) 已知 web_rid —— 工具主路径
    if web_rid:
        # 解析阶段刚由 reflow/enter 取到的状态（<1s），直接用，省一次请求
        if prefetched is not None and str((prefetched.raw or {}).get("web_rid") or "") == web_rid:
            return prefetched
        status, err = _fetch_enter(web_rid, deadline, cookie_override=cookie_override)
        if status is not None:
            return status
        if prefetched is not None:
            return prefetched
        if room_id and sec_uid:
            fallback, err2 = _status_from_reflow(room_id, sec_uid, deadline, cookie_override=cookie_override)
            if fallback is not None:
                return fallback
            err = err2 or err
        return LiveStatus(is_live=False, room_id=web_rid, error=err or "查询失败")

    # B) 只有 19 位 room_id + sec_uid（短链 302 直接给出的组合）
    if room_id and sec_uid:
        status, err = _status_from_reflow(room_id, sec_uid, deadline, cookie_override=cookie_override)
        if status is not None:
            return status
        return LiveStatus(is_live=False, error=err or "查询失败")

    # C) 只有 sec_uid（解析阶段没能换成 web_rid，例如主播未设置抖音号）
    if sec_uid:
        return LiveStatus(
            is_live=False,
            error=(target_info.get("error") or
                   ("已解析到主播 sec_uid=%s，但没能解析出直播间 web_rid" % sec_uid)),
        )

    # D) 无法识别
    return LiveStatus(
        is_live=False,
        error=target_info.get("error") or "无法识别的目标（支持房间号/直播间链接/v.douyin.com 短链/分享文案）",
    )


def fetch_recommend(limit: int = 10, timeout: float = 15) -> list:
    """发现当前在播房间（GUI「发现当前在播」用）。失败返回 []，不抛异常。"""
    try:
        limit = max(1, min(int(limit), 30))
    except Exception:
        limit = 10
    try:
        timeout = max(5.0, float(timeout))
    except Exception:
        timeout = 15.0
    deadline = time.monotonic() + timeout
    count = min(30, max(limit * 2, limit + 5))
    params = {
        "aid": "6383",
        "app_name": "douyin_web",
        "live_id": "1",
        "device_platform": "web",
        "language": "zh-CN",
        "cookie_enabled": "true",
        "count": str(count),
    }
    url = FEED_API + "?" + urllib.parse.urlencode(params)
    data, _err = _api_json(url, timeout=_remaining(deadline, 3.0), retries=1)
    if data is None:
        return []
    out = []
    seen = set()
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        room = item.get("data") if isinstance(item.get("data"), dict) else item
        if not isinstance(room, dict):
            continue
        owner = room.get("owner") if isinstance(room.get("owner"), dict) else {}
        web_rid = str(owner.get("web_rid") or owner.get("short_id") or "")
        status = _build_status(room, owner, web_rid=web_rid, source="feed")
        if not status.is_live:
            continue
        key = status.raw.get("room_id") or status.room_id
        if key in seen:
            continue
        seen.add(key)
        status.raw["feed_item"] = True
        out.append(status)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# 选流
# --------------------------------------------------------------------------- #
def _quality_candidates(quality_lower):
    """quality 参数 -> 候选名列表（先老档位键，再 sdk_key / 中文名）。"""
    candidates = _QUALITY_CANDIDATES.get(quality_lower)
    if candidates is None:
        candidates = (quality_lower,)
    out = []
    for name in candidates:
        if name and name not in out:
            out.append(name)
    return out


def _match_quality(qualities, candidate):
    """在 qualities 里按 map_key -> sdk_key -> name 的顺序找匹配项。"""
    cand = str(candidate or "").strip().lower()
    if not cand:
        return None
    for field in ("map_key", "sdk_key", "name"):
        for entry in qualities:
            if str(entry.get(field) or "").strip().lower() == cand:
                return entry
    return None


def _entry_url(status, entry, kinds):
    """按 kinds 优先级取某个 quality 条目的流地址（flv_idx/hls_idx 指向 streams 列表）。"""
    if not isinstance(entry, dict):
        return ""
    streams = getattr(status, "streams", None) or {}
    for kind in kinds:
        idx = entry.get("flv_idx" if kind == "flv" else "hls_idx")
        if idx is None:
            continue
        bucket = streams.get(kind) or []
        try:
            idx = int(idx)
        except Exception:
            continue
        if 0 <= idx < len(bucket) and bucket[idx]:
            return str(bucket[idx])
    return ""


def _scored_qualities(status):
    """按 score（分辨率面积×码率）从高到低返回 quality 条目。"""
    raw = getattr(status, "raw", None) or {}
    qualities = [q for q in (raw.get("qualities") or []) if isinstance(q, dict)]
    kinds = ["flv", "hls"]
    scored = []
    for entry in qualities:
        score = int(entry.get("score") or 0)
        bitrate = int(entry.get("bitrate") or 0)
        if _entry_url(status, entry, kinds):
            scored.append((score, bitrate, entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _score, _bitrate, entry in scored]


def choose_stream(status: LiveStatus, prefer: str = "flv", quality: str = "origin") -> str | None:
    """按优先级取可录制流地址。

    prefer: "flv"（默认，直录最快）或 "hls"
    quality:
      * "origin"（默认）：原画流（stream_url.live_core_sdk_data 的 origin 档），没有则取最高档
      * "best"：按 qualities 的 分辨率面积×码率 选最高档（缺失回退 origin→FULL_HD1→HD1→SD1→SD2→hls）
      * "uhd"/"hd"/"sd"/"ld"/"ao"：对应清晰度档（按 sdk_key / 老档位键 / 中文名匹配，容错）
      * "FULL_HD1"/"HD1"/"SD1"/"SD2"：老档位键
    找不到时返回 None。
    """
    if status is None or not getattr(status, "is_live", False):
        return None
    streams = getattr(status, "streams", None) or {}
    if not isinstance(streams, dict):
        return None
    raw = getattr(status, "raw", None) or {}
    qualities = [q for q in (raw.get("qualities") or []) if isinstance(q, dict)]

    pref = (prefer or "flv").strip().lower()
    kinds = []
    for kind in ((pref if pref in ("flv", "hls") else "flv"), "flv", "hls"):
        if kind not in kinds:
            kinds.append(kind)

    want = (quality or "origin").strip()
    want_lower = want.lower()
    is_best = want_lower == "best"
    is_origin = want_lower in ("origin", "原画", "")

    # 1) 明确指定档位（best/origin 除外）
    if not is_best and not is_origin:
        for candidate in _quality_candidates(want_lower):
            entry = _match_quality(qualities, candidate)
            if entry is not None:
                url = _entry_url(status, entry, kinds)
                if url:
                    return url

    # 2) best：按元数据取最高档
    if is_best:
        scored = _scored_qualities(status)
        if scored:
            url = _entry_url(status, scored[0], kinds)
            if url:
                return url

    # 3) origin：原画流优先
    origin = streams.get("origin") or {}
    if isinstance(origin, dict):
        for kind in kinds:
            url = origin.get(kind)
            if url:
                return str(url)

    # 4) origin 缺原画 / best 缺元数据：按元数据最高档
    if is_origin or is_best or not qualities:
        scored = _scored_qualities(status)
        if scored:
            url = _entry_url(status, scored[0], kinds)
            if url:
                return url

    # 5) 老档位键回退链（ORIGIN/FULL_HD1/HD1/SD1/SD2）
    for map_key in _FALLBACK_STREAM_KEYS:
        entry = _match_quality(qualities, map_key)
        if entry is not None:
            url = _entry_url(status, entry, kinds)
            if url:
                return url

    # 6) 最后：streams 顺序表第一个（服务端顺序，通常是最高档）
    for kind in kinds:
        for url in streams.get(kind) or []:
            if url:
                return str(url)
    for kind in ("hls", "flv"):
        for url in streams.get(kind) or []:
            if url:
                return str(url)
    return None


def get_qualities(status: LiveStatus, prefer: str = "flv") -> list:
    """清晰度列表（GUI 展示/选择用），按画质从高到低排序。

    返回 [{"name","sdk_key","resolution","bitrate","fps","url"}]；
    prefer="flv"/"hls" 决定 url 取哪一路（缺该路时自动用另一路）。
    """
    if status is None or not getattr(status, "is_live", False):
        return []
    streams = getattr(status, "streams", None) or {}
    if not isinstance(streams, dict):
        return []
    raw = getattr(status, "raw", None) or {}
    qualities = [q for q in (raw.get("qualities") or []) if isinstance(q, dict)]
    kind = "hls" if str(prefer or "flv").strip().lower() == "hls" else "flv"
    other = "flv" if kind == "hls" else "hls"
    rows = []
    for entry in qualities:
        url = _entry_url(status, entry, [kind, other])
        if not url:
            continue
        rows.append((
            int(entry.get("score") or 0),
            int(entry.get("bitrate") or 0),
            {
                "name": str(entry.get("name") or ""),
                "sdk_key": str(entry.get("sdk_key") or ""),
                "resolution": str(entry.get("resolution") or ""),
                "bitrate": int(entry.get("bitrate") or 0),
                "fps": int(entry.get("fps") or 0),
                "url": url,
            },
        ))
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in rows]


# --------------------------------------------------------------------------- #
# 命令行自测
# --------------------------------------------------------------------------- #
def _brief(status: LiveStatus, with_urls=False) -> dict:
    streams = status.streams or {}
    raw = status.raw or {}
    qualities = [q for q in (raw.get("qualities") or []) if isinstance(q, dict)]
    brief = {
        "is_live": status.is_live,
        "room_id": status.room_id,
        "title": status.title,
        "anchor_name": status.anchor_name,
        "cover_url": status.cover_url,
        "started_at": status.started_at,
        "streams": {
            "flv_count": len(streams.get("flv") or []),
            "hls_count": len(streams.get("hls") or []),
            "has_origin": bool((streams.get("origin") or {}).get("flv") or (streams.get("origin") or {}).get("hls")),
        },
        "choose_url": choose_stream(status) or "",
        "quality_options": [
            {"sdk_key": q.get("sdk_key"), "name": q.get("name"), "resolution": q.get("resolution"),
             "bitrate": q.get("bitrate"), "fps": q.get("fps")}
            for q in get_qualities(status)
        ],
        "raw": {
            "source": raw.get("source"),
            "status": raw.get("status"),
            "web_rid": raw.get("web_rid"),
            "room_id": raw.get("room_id"),
            "default_resolution": raw.get("default_resolution"),
            "flv_pull_url_keys": raw.get("flv_pull_url_keys") or [],
            "hls_pull_url_keys": raw.get("hls_pull_url_keys") or [],
            "qualities": [q.get("sdk_key") or q.get("map_key") for q in qualities],
        },
        "error": status.error,
    }
    if with_urls:
        brief["streams"]["flv"] = streams.get("flv") or []
        brief["streams"]["hls"] = streams.get("hls") or []
        brief["raw"]["qualities"] = qualities
        brief["quality_options"] = get_qualities(status)
    return brief


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="dy_api.py",
        description="抖音直播取流协议层自测（python src/dy_api.py --selftest <target>）",
    )
    parser.add_argument("target", nargs="?", default=None, help="房间号/直播间链接/短链/分享文案")
    parser.add_argument("--selftest", nargs="?", const="", default=None, metavar="TARGET",
                        help="自测：查询直播状态并打印 JSON（不给 TARGET 则自动从在播列表挑一个）")
    parser.add_argument("--recommend", nargs="?", const="10", default=None, metavar="N",
                        help="发现当前在播房间（默认 10 个）")
    parser.add_argument("--cookie", action="store_true", help="打印当前 cookie（含 ttwid）")
    parser.add_argument("--qualities", nargs="?", const="", default=None, metavar="TARGET",
                        help="打印指定房间的清晰度列表（JSON，GUI 画质选择/排错用）")
    parser.add_argument("--stats", action="store_true", help="打印本进程 HTTP 统计（请求/连接/流量/缓存）")
    parser.add_argument("--check", action="store_true", help="查看解析缓存文件路径与条目数（落盘缓存诊断）")
    parser.add_argument("--timeout", type=float, default=15.0, help="整体超时秒数，默认 15")
    parser.add_argument("--json", action="store_true", help="打印完整 streams 地址（默认也打印 choose_url）")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.check:
        info = get_resolve_cache_info()
        info["http_stats"] = get_http_stats()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if args.stats:
        print(json.dumps(get_http_stats(), ensure_ascii=False, indent=2))
        return 0

    if args.cookie:
        cookies = get_cookie()
        print(json.dumps({"ok": bool(cookies.get("ttwid")), "cookies": cookies,
                          "error": get_cookie_error()}, ensure_ascii=False, indent=2))
        return 0 if cookies.get("ttwid") else 1

    if args.qualities is not None:
        target = args.qualities or args.target
        if not target:
            print(json.dumps({"error": "用法: python src/dy_api.py --qualities <房间号/链接>"},
                             ensure_ascii=False, indent=2))
            return 1
        status = get_live_status(target, timeout=args.timeout)
        print(json.dumps({
            "target": target, "is_live": status.is_live, "room_id": status.room_id,
            "title": status.title, "error": status.error,
            "qualities": get_qualities(status),
        }, ensure_ascii=False, indent=2))
        return 0 if not status.error else 1

    if args.recommend is not None:
        try:
            count = int(args.recommend or 10)
        except Exception:
            count = 10
        rooms = fetch_recommend(count, timeout=args.timeout)
        print(json.dumps([_brief(s) for s in rooms], ensure_ascii=False, indent=2))
        return 0 if rooms else 1

    target = args.selftest if args.selftest is not None else args.target
    if args.selftest is not None and not target:
        rooms = fetch_recommend(1, timeout=args.timeout)
        if not rooms:
            print(json.dumps({"error": "未发现当前在播房间（feed 接口为空或风控）"}, ensure_ascii=False, indent=2))
            return 1
        target = rooms[0].room_id
        print("# 自动选中在播房间: %s (%s)" % (rooms[0].room_id, rooms[0].anchor_name or rooms[0].title))
    if not target:
        parser.print_help()
        return 1
    status = get_live_status(target, timeout=args.timeout)
    print(json.dumps(_brief(status, with_urls=args.json), ensure_ascii=False, indent=2))
    if status.error:
        return 1
    return 0 if status.is_live else 2


if __name__ == "__main__":
    raise SystemExit(_main())
