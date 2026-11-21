# -*- coding: utf-8 -*-
"""直播状态查询层：封装 src/dy_api.py 契约，失败时降级。

职责：
1. 加载协议模块（优先真实 dy_api，缺失/导入失败时回落内置 mock）；
2. 把 dy_api 返回的 LiveStatus 转成内部 StatusView，任何异常都转成 error 文本
   （网络抖动不能把监控循环打挂）；
3. 提供目标解析（链接/房间号/分享文案 -> web_rid）与流地址选择。

dy_api.py 由 dy-proto 负责实现；本模块按 docs/INTERFACES.md 的契约调用，
不对其内部实现做任何假设。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import ROOT
from .model import parse_target_text

LOG = logging.getLogger("dyrec.checker")

#: 契约要求的 API 函数
_REQUIRED = ("get_live_status", "choose_stream", "resolve_target")


@dataclass
class StatusView:
    """内部统一的直播状态视图（与具体协议模块解耦）。"""
    is_live: bool = False
    room_id: str = ""
    title: str = ""
    anchor_name: str = ""
    cover_url: str = ""
    started_at: int = 0
    streams: dict = field(default_factory=dict)
    error: str = ""
    raw_obj: Any = None
    checked_at: float = field(default_factory=time.time)


def ensure_pylibs_path() -> None:
    """把项目中解包的 .pylibs 加入 sys.path（dy_api 可能用到 requests）。"""
    p = ROOT / ".pylibs"
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _api_ok(module: Any) -> bool:
    return all(hasattr(module, name) for name in _REQUIRED)


def load_api(use_mock: bool = False) -> Tuple[Any, str, str]:
    """返回 (api 模块, 类型名, 错误说明)。错误说明非空表示回落到了 mock。"""
    ensure_pylibs_path()
    errors = []
    if not use_mock:
        # 方式 1：包内相对导入（python run.py）
        try:
            from . import dy_api  # type: ignore
            if _api_ok(dy_api):
                return dy_api, "dy_api", ""
            errors.append("src/dy_api.py 缺少契约函数")
        except Exception as exc:  # 导入期异常都不致命
            errors.append("相对导入失败: %s" % exc)
        # 方式 2：顶层导入（sys.path 含 src）
        try:
            import dy_api  # type: ignore
            if _api_ok(dy_api):
                return dy_api, "dy_api", ""
            errors.append("顶层导入缺函数")
        except Exception as exc:
            errors.append("顶层导入失败: %s" % exc)
        # 方式 3：按文件路径直接加载
        path = Path(__file__).resolve().parent / "dy_api.py"
        if path.exists():
            try:
                spec = importlib.util.spec_from_file_location("dy_api_file", path)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules["dy_api_file"] = mod
                    spec.loader.exec_module(mod)
                    if _api_ok(mod):
                        return mod, "dy_api", ""
                    errors.append("文件加载缺函数")
            except Exception as exc:
                errors.append("文件加载失败: %s" % exc)
    # 兜底：内置 mock
    from . import mock_api
    msg = "；".join(errors) if errors else ""
    return mock_api, "mock", msg


def _to_view(raw: Any) -> StatusView:
    """把 dy_api.LiveStatus（或等价 dict）转成 StatusView。"""
    if raw is None:
        return StatusView(is_live=False, error="协议层返回空状态")
    if isinstance(raw, dict):
        data = dict(raw)
        obj = None
    else:
        obj = raw
        data = {}
        for name in (
            "is_live", "room_id", "title", "anchor_name", "cover_url",
            "started_at", "streams", "error",
        ):
            data[name] = getattr(raw, name, None)
    streams = data.get("streams") or {}
    if not isinstance(streams, dict):
        streams = {}
    try:
        started = int(data.get("started_at") or 0)
    except (TypeError, ValueError):
        started = 0
    return StatusView(
        is_live=bool(data.get("is_live")),
        room_id=str(data.get("room_id") or ""),
        title=str(data.get("title") or ""),
        anchor_name=str(data.get("anchor_name") or ""),
        cover_url=str(data.get("cover_url") or ""),
        started_at=started,
        streams=streams,
        error=str(data.get("error") or ""),
        raw_obj=obj,
    )


def _pick_stream(streams: Dict[str, Any], quality: str = "origin") -> Optional[str]:
    """本地兜底选流：flv > hls > origin 字典。"""
    for key in ("flv", "hls"):
        urls = streams.get(key)
        if isinstance(urls, str) and urls:
            return urls
        if isinstance(urls, list):
            for u in urls:
                if u:
                    return str(u)
    origin = streams.get("origin")
    if isinstance(origin, dict):
        for key in (quality or "origin", "flv", "hd", "sd"):
            val = origin.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, list):
                for u in val:
                    if u:
                        return str(u)
    return None


class LiveChecker:
    """封装协议模块的状态查询（线程安全、异常安全）。"""

    def __init__(self, config=None, api: Any = None, use_mock: Optional[bool] = None):
        self.config = config
        if api is not None:
            self.api = api
            self.api_name = getattr(api, "__name__", "custom")
            self.api_error = ""
        else:
            mock = (
                bool(use_mock)
                if use_mock is not None
                else bool(config.get_bool("use_mock_api") if config is not None else False)
            )
            self.api, self.api_name, self.api_error = load_api(mock)
        if self.api_name == "mock":
            self._configure_mock()
        if self.api_error:
            LOG.warning("协议模块加载异常，已回落 mock: %s", self.api_error)
        else:
            LOG.info("协议模块: %s", self.api_name)

    def _configure_mock(self) -> None:
        try:
            from . import mock_api
            ffmpeg = self.config.get_str("ffmpeg_path") if self.config is not None else ""
            mock_api.configure(ffmpeg_path=ffmpeg)
        except Exception as exc:  # pragma: no cover
            LOG.warning("配置 mock 失败: %s", exc)

    def describe(self) -> str:
        if self.api_error:
            return "%s（%s）" % (self.api_name, self.api_error)
        return self.api_name

    # ---------------- 解析 ----------------
    def resolve(self, text: str) -> Dict[str, Any]:
        """解析用户输入，返回 {web_rid, sec_uid, url, name}（可能联网，失败时用本地解析结果）。"""
        local = parse_target_text(text)
        result = {
            "web_rid": local.get("web_rid") or "",
            "sec_uid": local.get("sec_uid") or "",
            "url": local.get("url") or "",
            "name": "",
            "error": "",
        }
        try:
            info = self.api.resolve_target(text) or {}
            if isinstance(info, dict):
                rid = info.get("web_rid")
                if rid:
                    result["web_rid"] = str(rid)
                sec = info.get("sec_uid")
                if sec:
                    result["sec_uid"] = str(sec)
                url = info.get("url")
                if url:
                    result["url"] = str(url)
                name = info.get("anchor_name") or info.get("name")
                if name:
                    result["name"] = str(name)
        except Exception as exc:
            result["error"] = "解析失败: %s" % exc
        if not result["url"] and result["web_rid"]:
            result["url"] = "https://live.douyin.com/" + result["web_rid"]
        return result

    # ---------------- 查询 ----------------
    def _cookie(self) -> Optional[dict]:
        if self.config is None:
            return None
        raw = self.config.get_str("cookie").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return {"ttwid": raw}

    def target_of(self, item) -> str:
        """取查询用的 target 字符串。"""
        if isinstance(item, str):
            return item
        for attr in ("web_rid", "url", "target"):
            val = getattr(item, attr, "") or ""
            if val:
                return val
        return ""

    def check(self, item) -> StatusView:
        """查询一个房间的直播状态。永不抛异常，失败时 error 非空。"""
        target = self.target_of(item)
        if not target:
            return StatusView(is_live=False, error="缺少房间号/链接")
        try:
            raw = self.api.get_live_status(target, cookie=self._cookie(), timeout=15)
        except TypeError:
            try:
                raw = self.api.get_live_status(target)
            except Exception as exc:
                return StatusView(is_live=False, error="查询失败: %s" % exc)
        except Exception as exc:
            return StatusView(is_live=False, error="查询失败: %s" % exc)
        view = _to_view(raw)
        if not view.room_id:
            parsed = parse_target_text(target)
            view.room_id = parsed.get("web_rid") or ""
        return view

    def choose_url(self, status: StatusView, quality: str = "origin") -> Optional[str]:
        """选择可录制的流地址（优先 flv）。"""
        obj = getattr(status, "raw_obj", None)
        if obj is not None and hasattr(self.api, "choose_stream"):
            try:
                url = self.api.choose_stream(obj, prefer="flv", quality=quality or "origin")
                if url:
                    return str(url)
            except Exception as exc:
                LOG.debug("choose_stream 异常: %s", exc)
        return _pick_stream(status.streams or {}, quality)