# -*- coding: utf-8 -*-
"""通知：企业微信/钉钉/飞书群机器人 webhook + 开播提示声音。

配置（config.json）：
    "notify": {
        "enabled": true,
        "webhooks": ["https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
                     "https://oapi.dingtalk.com/robot/send?access_token=xxx"],
        "sound": true,
        "events": ["start", "live", "stopped", "error"]
    }

发送在后台线程完成（5 秒超时），失败只记日志，绝不影响录制。
webhook 类型按域名自动识别，也可写成 {type, url} 显式指定。
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("dyrec.notify")

#: 事件 -> 标题前缀
EVENT_TITLES = {
    "start": "开始录制",
    "live": "主播开播",
    "stopped": "录制结束",
    "error": "录制错误",
}


def _detect_type(url: str) -> str:
    """按 webhook 域名识别机器人类型：wecom / dingtalk / feishu / generic。"""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if "qyapi.weixin.qq.com" in host or "wecom" in host:
        return "wecom"
    if "dingtalk" in host or "dingtalk.com" in host:
        return "dingtalk"
    if "feishu" in host or "larksuite" in host:
        return "feishu"
    return "generic"


def build_payload(url: str, text: str, explicit_type: str = "") -> Dict[str, Any]:
    """构造各平台兼容的 JSON 体。"""
    kind = explicit_type or _detect_type(url)
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    # 企业微信/钉钉/通用都用 msgtype=text text.content
    return {"msgtype": "text", "text": {"content": text}}


class Notifier:
    """事件通知器（线程安全，失败静默）。"""

    def __init__(self, config=None, logger: Optional[logging.Logger] = None):
        self.log = logger or LOG
        self.enabled = False
        self.sound = True
        self.events = set()
        self.webhooks: List[Dict[str, str]] = []
        if config is not None:
            self.configure(config.get("notify") or {})

    def configure(self, notify_cfg: Dict[str, Any]) -> None:
        notify_cfg = notify_cfg if isinstance(notify_cfg, dict) else {}
        self.enabled = bool(notify_cfg.get("enabled", False))
        self.sound = bool(notify_cfg.get("sound", True))
        events = notify_cfg.get("events")
        self.events = set(events) if isinstance(events, list) else {
            "start", "live", "stopped", "error"
        }
        hooks = []
        for entry in notify_cfg.get("webhooks") or []:
            if isinstance(entry, str) and entry.strip():
                hooks.append({"url": entry.strip(), "type": _detect_type(entry.strip())})
            elif isinstance(entry, dict) and entry.get("url"):
                url = str(entry["url"]).strip()
                hooks.append({"url": url, "type": str(entry.get("type") or _detect_type(url))})
        self.webhooks = hooks

    @property
    def has_webhook(self) -> bool:
        return bool(self.webhooks)

    def notify(self, kind: str, title: str, message: str = "") -> None:
        """发送一条通知（异步）。kind 见 EVENT_TITLES。"""
        if not self.enabled or kind not in self.events:
            return
        text = (title + "\n" + message).strip()
        for hook in self.webhooks:
            threading.Thread(
                target=self._post,
                args=(hook, text),
                name="notify",
                daemon=True,
            ).start()

    def beep(self) -> None:
        """开播提示声音（Windows）。"""
        if not self.sound:
            return
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            try:
                import winsound
                winsound.Beep(1000, 200)
            except Exception:  # pragma: no cover - 非 Windows/无声卡
                pass

    def _post(self, hook: Dict[str, str], text: str) -> None:
        url = hook.get("url") or ""
        if not url:
            return
        payload = build_payload(url, text, hook.get("type") or "")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read(300).decode("utf-8", "replace")
                self.log.info("通知已发送(%s): %s", resp.status, body[:200])
        except Exception as exc:
            self.log.warning("通知发送失败 %s: %s", url, exc)