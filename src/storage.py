# -*- coding: utf-8 -*-
"""watchlist.json 持久化。

- UTF-8 编码、ensure_ascii=False（中文可读）
- 原子写（临时文件 + os.replace）
- 文件损坏时自动备份为 *.corrupt-*.bak 并重建为空列表
- 线程安全（GUI 线程与监控线程都会读写）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import DEFAULT_WATCHLIST_PATH, atomic_write_json
from .model import WatchItem, parse_target_text

LOG = logging.getLogger("dyrec.storage")


def load_json_safe(path: Path, default, logger: logging.Logger = LOG):
    """读取 JSON；文件不存在返回 default；损坏时备份并返回 default。"""
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:
            return json.load(fp)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        logger.error("读取 %s 失败: %s", path, exc)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
        try:
            os.replace(path, backup)
            logger.warning("已备份损坏文件到 %s", backup)
        except OSError as exc2:
            logger.error("备份失败: %s", exc2)
        return default


class Watchlist:
    """主播列表。所有公开方法内部加锁，可跨线程调用。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DEFAULT_WATCHLIST_PATH
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()   # 串行化落盘（后台合并保存与主线程保存不能并发写同一临时文件）
        self._items: List[WatchItem] = []
        self.load()

    # ---------------- 读写 ----------------
    def load(self) -> List[WatchItem]:
        raw = load_json_safe(self.path, {"items": []})
        items: List[WatchItem] = []
        if isinstance(raw, list):  # 兼容纯数组格式
            raw = {"items": raw}
        if isinstance(raw, dict):
            for entry in raw.get("items") or []:
                if isinstance(entry, dict):
                    try:
                        items.append(WatchItem.from_dict(entry))
                    except (TypeError, ValueError) as exc:
                        LOG.warning("跳过非法主播条目: %s", exc)
        with self._lock:
            self._items = items
        return list(items)

    def save(self) -> bool:
        with self._save_lock:
            with self._lock:
                data = {"items": [it.to_dict() for it in self._items]}
            try:
                atomic_write_json(self.path, data)
                return True
            except OSError as exc:
                LOG.error("保存主播列表失败 %s: %s", self.path, exc)
                return False

    # ---------------- 查询 ----------------
    def all(self) -> List[WatchItem]:
        with self._lock:
            return list(self._items)

    def enabled(self) -> List[WatchItem]:
        return [it for it in self.all() if it.enabled]

    def get(self, item_id: str) -> Optional[WatchItem]:
        with self._lock:
            for it in self._items:
                if it.id == item_id:
                    return it
        return None

    def find_by_room(self, web_rid: str) -> Optional[WatchItem]:
        with self._lock:
            for it in self._items:
                if it.web_rid and it.web_rid == web_rid:
                    return it
        return None

    def index_of(self, item_id: str) -> int:
        with self._lock:
            for idx, it in enumerate(self._items):
                if it.id == item_id:
                    return idx
        return -1

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    # ---------------- 增删改 ----------------
    def add(self, item: WatchItem) -> WatchItem:
        with self._lock:
            self._items.append(item)
        self.save()
        LOG.info("添加主播: %s (%s)", item.display_name, item.web_rid or item.target)
        return item

    def add_from_text(self, text: str, name: str = "", auto_record: bool = True,
                      quality: str = "origin", output_template: str = "",
                      save: bool = True) -> WatchItem:
        """从分享文案/链接/房间号添加。

        save=False 时只加入内存、不落盘（批量导入时由调用方合并保存一次，
        避免 1360 条逐条写 JSON 造成 UI 卡顿）。默认行为保持兼容。
        """
        info = parse_target_text(text)
        item = WatchItem(
            name=name or "",
            target=text.strip(),
            web_rid=info.get("web_rid") or "",
            url=info.get("url") or "",
            auto_record=auto_record,
            quality=quality,
            output_template=output_template,
        )
        with self._lock:
            self._items.append(item)
        if save:
            self.save()
        LOG.info("添加主播: %s (%s)", item.display_name, item.web_rid or item.target)
        return item

    def update(self, item: WatchItem, save: bool = True) -> bool:
        """按 id 更新条目；save=False 只改内存（批量补全时由调用方合并保存）。"""
        with self._lock:
            for idx, cur in enumerate(self._items):
                if cur.id == item.id:
                    self._items[idx] = item
                    break
            else:
                return False
        if save:
            self.save()
        return True

    def replace(self, item: WatchItem) -> bool:
        """按 id 覆盖（不存在则追加），返回是否命中已有条目。"""
        with self._lock:
            for idx, cur in enumerate(self._items):
                if cur.id == item.id:
                    self._items[idx] = item
                    self.save()
                    return True
            self._items.append(item)
        self.save()
        return False

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if it.id != item_id]
            changed = len(self._items) != before
        if changed:
            self.save()
            LOG.info("删除主播 id=%s", item_id)
        return changed

    def remove_many(self, ids: Iterable[str]) -> int:
        id_set = set(ids)
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if it.id not in id_set]
            removed = before - len(self._items)
        if removed:
            self.save()
        return removed

    def clear(self) -> None:
        with self._lock:
            self._items = []
        self.save()
