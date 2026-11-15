# -*- coding: utf-8 -*-
"""配置管理。

设计目标：
1. 所有配置项缺失时使用默认值，不抛异常；
2. config.json 损坏时自动备份为 *.corrupt-<时间>.bak 并重建默认配置；
3. 保存时使用原子写（先写临时文件再 os.replace），避免断电截断；
4. 数值/枚举类配置做范围校验，非法值回退默认。

配置文件位置：项目根目录 config.json
"""
from __future__ import annotations

import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .ffmpeg_util import DEFAULT_HTTP_INPUT_ARGS

# 项目根目录：src/ 的上一级
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
DEFAULT_WATCHLIST_PATH = ROOT / "watchlist.json"

# 项目自带的 ffmpeg（随工具分发，卸载原软件也能用）：<项目根>/bin/ffmpeg.exe
LOCAL_FFMPEG = ROOT / "bin" / "ffmpeg.exe"
# 原版软件目录里的 ffmpeg（兼容旧用户，作为兜底候选）
BUNDLED_FFMPEG = Path(r"D:\抖音录制\抖音直播录制工具\PotPlayer\cmdffmpeg.exe")

# 默认输出根目录：项目根下 records/
DEFAULT_OUTPUT_DIR = ROOT / "records"
DEFAULT_LOG_DIR = ROOT / "logs"

#: 配置默认值。新增配置项只需加在这里，旧配置文件会自动补齐。
DEFAULTS: Dict[str, Any] = {
    # ---- 监控 ----
    "check_interval": 60,            # 兼容旧配置；P1 起实际轮询由 poll_interval_* 档位控制
    "max_concurrent": 0,             # 同时录制房间数上限，0 = 不限（P1 起默认不限）
    "use_mock_api": False,           # True 时不用真实 dy_api，改用内置 mock（离线自测）
    "cookie": "",                    # 可选：手动 cookie（dy_api 支持时使用）
    "proxy": "",                     # 可选：HTTP 代理，例如 http://127.0.0.1:7890

    # ---- 调度/限速/熔断（P1：面向 1000+ 主播长期挂机）----
    "max_poll_workers": 12,          # 状态查询固定工作线程数（不再按 tick 全量提交）
    "poll_rate_limit": 6,            # 全局查询限速（次/秒），带 ±20% 抖动
    "poll_interval_live": 30,        # 直播中复查间隔（秒）
    "poll_interval_warm": 90,        # 24 小时内开播过的复查间隔（秒）
    "poll_interval_cold": 300,       # 长期未开播的复查间隔（秒）
    "poll_backoff_start": 30,        # 单房间查询失败退避起始（秒）
    "poll_backoff_max": 300,         # 单房间查询失败退避上限（秒，指数）
    "breaker_window_seconds": 30,    # 熔断统计窗口（秒）
    "breaker_error_rate": 0.3,       # 窗口错误率阈值（超过即计入）
    "breaker_min_requests": 20,      # 窗口内最少请求数（不足不判定）
    "breaker_pause_start": 30,       # 首次熔断暂停（秒）
    "breaker_pause_max": 120,        # 熔断暂停上限（秒，指数增长）
    "breaker_halfopen_requests": 5,  # 半开阶段放行请求数
    "resolve_workers": 3,            # 批量导入解析线程数（2~4）
    "invalid_auto_disable_after": 3, # 连续 N 次确认条目无效后自动停用（0=不停用）
    "resolve_retries": 2,            # 解析失败重试次数

    # ---- 录制/并发闸门 ----
    "max_total_mbps": 0,             # 录制总带宽预算（Mbps），0 = 不限；超出则排队
    "max_ffmpeg_processes": 300,     # 同时运行的 ffmpeg 进程数上限（0 = 不限）
    "output_dir": str(DEFAULT_OUTPUT_DIR),   # 录制输出根目录
    "output_template": "{anchor}/{date}",     # 房间子目录模板（相对 output_dir）
    "output_template_custom": "{anchor}",     # 设置页选「自定义…」时使用的模板
    "filename_template": "{anchor}_{date}_{time}",  # 文件名模板（不含扩展名）
    # ffmpeg 路径：留空 = 自动（bin/ffmpeg.exe -> 原版目录 -> PATH），也可填绝对路径
    "ffmpeg_path": "",
    "ffmpeg_extra_args": [],                  # 追加给 ffmpeg 的通用输出参数
    "ffmpeg_http_args": list(DEFAULT_HTTP_INPUT_ARGS),  # HTTP 流输入参数（UA/Referer/重连等）
    "container": "flv",              # 录制容器：flv（默认，直录最稳）或 ts
    "remux_mp4": True,               # 录制结束后是否 remux 成 MP4（-c copy 不转码）
    "keep_source": False,            # remux 成 MP4 后是否保留原文件
    "segment_duration": 0,           # 分段时长（秒），0 = 不分段
    "segment_size_mb": 0,            # 单文件大小上限（MB），0 = 不限；超出后自动换新文件
    "stall_check_seconds": 30,       # 卡死看门狗：每隔多少秒检查一次录制文件是否增长
    "stall_kill_after": 2,           # 连续 N 次无增长则判定 ffmpeg 卡死并重启录制
    "quality": "origin",             # 默认画质：origin（原画优先）/best（最高可用档）/hd/sd
    # 断流自动降档：一场录制内累计「重连/卡死」达到阈值后，停止当前录制并按阶梯降一档重开
    "quality_degrade_enabled": True,
    "quality_degrade_after_reconnects": 2,
    "quality_ladder": "origin,hd,sd",       # 降档阶梯（左高右低）
    "retry_max": 5,                  # 断流后最大重连次数，0 = 不重连
    "retry_interval": 5,             # 首次重连间隔（秒），之后递增
    "retry_interval_max": 120,       # 重连间隔上限（秒）
    "min_free_gb": 2.0,              # 磁盘剩余低于该值（GB）时停止录制并告警
    "keep_days": 0,                  # 保留天数，0 = 不自动清理
    "keep_max_files": 0,             # 每个主播目录最多保留文件数，0 = 不限
    # 最短保留时长（秒）：录制总时长低于该值则自动删除本场所有产物，0 = 关闭
    "min_keep_seconds": 30,
    "record_timeout_min": 0,         # 单次录制最长分钟数，0 = 不限
    "graceful_stop_timeout": 20,     # 停止时等待 ffmpeg 退出的秒数
    "shutdown_wait_seconds": 600,    # 停止监控/关闭软件时，等待录制收尾并转 MP4 的上限（0=不限时）

    # ---- 通知 ----
    "notify": {
        "enabled": False,            # 是否启用群机器人/声音通知
        "webhooks": [],              # 企业微信/钉钉/飞书 群机器人 webhook 地址列表
        "sound": True,               # 开播提示声音（Windows 蜂鸣）
        "events": ["start", "live", "stopped", "error"],  # 需要通知的事件
    },

    # ---- 日志/界面 ----
    "log_dir": str(DEFAULT_LOG_DIR),
    "log_level": "INFO",
    "log_keep_days": 30,             # 日志保留天数（logs/run-YYYY-MM-DD.log），0 = 不清理
    "ui_refresh_ms": 1000,           # GUI 状态刷新间隔（毫秒）
}

#: 数值型配置的 (下限, 上限)，用于修正非法值
_RANGES = {
    "check_interval": (5, 3600),
    "max_concurrent": (0, 4096),          # 0 = 不限
    "max_total_mbps": (0, 1000000),       # 0 = 不限
    "max_ffmpeg_processes": (0, 10000),   # 0 = 不限
    "max_poll_workers": (1, 128),
    "poll_rate_limit": (1, 1000),
    "poll_interval_live": (1, 86400),
    "poll_interval_warm": (1, 86400),
    "poll_interval_cold": (1, 86400),
    "poll_backoff_start": (1, 3600),
    "poll_backoff_max": (1, 86400),
    "breaker_window_seconds": (1, 3600),
    "breaker_error_rate": (0.05, 1.0),
    "breaker_min_requests": (1, 100000),
    "breaker_pause_start": (1, 3600),
    "breaker_pause_max": (1, 3600),
    "breaker_halfopen_requests": (1, 1000),
    "resolve_workers": (1, 16),
    "resolve_retries": (0, 10),
    "segment_duration": (0, 86400),
    "segment_size_mb": (0, 1024 * 1024),
    "retry_max": (0, 100),
    "retry_interval": (1, 600),
    "retry_interval_max": (1, 3600),
    "min_free_gb": (0.0, 1024.0),
    "keep_days": (0, 3650),
    "keep_max_files": (0, 100000),
    "min_keep_seconds": (0, 86400),
    "record_timeout_min": (0, 60 * 24 * 30),
    "graceful_stop_timeout": (2, 300),
    "ui_refresh_ms": (200, 60000),
    "stall_check_seconds": (5, 600),
    "stall_kill_after": (1, 20),
    "log_keep_days": (0, 3650),
    "quality_degrade_after_reconnects": (1, 20),
}

NOTIFY_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "webhooks": [],
    "sound": True,
    "events": ["start", "live", "stopped", "error"],
}

def _migrate_legacy(raw: Any) -> Any:
    """老配置迁移（P1）。

    P1 之前没有 max_poll_workers 键；那时 max_concurrent 的默认值是 3（"只能同时录 3 个"）。
    用户明确要求"开播都要录"，因此把这种"旧默认 3"迁移为 0=不限（显式设置其它值的保留）。
    """
    if not isinstance(raw, dict) or not raw:
        return raw
    if "max_poll_workers" in raw:
        return raw
    out = dict(raw)
    try:
        if int(out.get("max_concurrent", 3)) == 3:
            out["max_concurrent"] = 0
    except (TypeError, ValueError):
        pass
    return out


def _missing_default_keys(raw: Dict[str, Any]) -> bool:
    """配置文件是否缺少默认项（新增配置项后用于自动补写 config.json）。"""
    if not isinstance(raw, dict):
        return True
    for key, default in DEFAULTS.items():
        if key not in raw:
            return True
        if key == "notify" and isinstance(default, dict):
            current = raw.get(key)
            if not isinstance(current, dict):
                return True
            if any(sub not in current for sub in default):
                return True
    return False


_ENUMS = {
    "container": ("flv", "ts"),
    "quality": ("origin", "best", "hd", "sd"),
    "log_level": ("DEBUG", "INFO", "WARNING", "ERROR"),
}

#: 清晰度降档阶梯允许的档位（best 视为最高档）
_QUALITY_LADDER_VALUES = ("origin", "best", "hd", "sd")
#: 默认降档阶梯
_QUALITY_LADDER_DEFAULT = ("origin", "hd", "sd")

LOG = logging.getLogger("dyrec.config")


def _coerce_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    return default


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON（UTF-8，中文不转义）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


def _backup_broken(path: Path) -> Optional[Path]:
    """把损坏文件改名备份，返回备份路径。"""
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
    try:
        os.replace(path, backup)
        LOG.warning("配置文件损坏，已备份为 %s", backup)
        return backup
    except OSError as exc:  # pragma: no cover - 极端权限场景
        LOG.error("备份损坏配置失败: %s", exc)
        return None


class Config(dict):
    """配置字典。支持 cfg["key"] 与 cfg.get_int("key") 两种访问方式。"""

    def __init__(self, path: Path, data: Optional[Dict[str, Any]] = None):
        super().__init__()
        self.path = Path(path)
        merged = copy.deepcopy(DEFAULTS)
        if data:
            merged.update({k: v for k, v in data.items() if not k.startswith("_")})
        self.update(merged)
        self._normalize()
        self._extra = {}  # 保留用户自定义但未知的键？暂不保留

    # ---------------- 加载/保存 ----------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8-sig") as fp:
                    raw = json.load(fp)
                if isinstance(raw, dict):
                    data = raw
                else:
                    LOG.warning("配置文件根节点不是对象，按空配置处理")
                    _backup_broken(path)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                LOG.error("读取配置失败（%s），将重建默认配置", exc)
                _backup_broken(path)
        data = _migrate_legacy(data)
        cfg = cls(path, data)
        # 首次运行、重建后、或缺少新增配置项时落盘，方便用户看到/编辑新配置
        if not path.exists() or _missing_default_keys(data):
            cfg.save()
        return cfg

    def save(self) -> bool:
        try:
            atomic_write_json(self.path, dict(self))
            return True
        except OSError as exc:
            LOG.error("保存配置失败: %s", exc)
            return False

    # ---------------- 校验 ----------------
    def _normalize(self) -> None:
        for key, default in DEFAULTS.items():
            if key not in self:
                self[key] = copy.deepcopy(default)
        for key, default in DEFAULTS.items():
            cur = self.get(key)
            if isinstance(default, bool):
                self[key] = _coerce_bool(cur, default)
            elif isinstance(default, int):
                val = _coerce_int(cur, default)
                lo, hi = _RANGES.get(key, (None, None))
                if lo is not None:
                    val = max(lo, min(hi, val))
                self[key] = val
            elif isinstance(default, float):
                val = _coerce_float(cur, default)
                lo, hi = _RANGES.get(key, (None, None))
                if lo is not None:
                    val = max(lo, min(hi, val))
                self[key] = val
            elif isinstance(default, str):
                val = cur if isinstance(cur, str) else str(cur)
                if key in _ENUMS:
                    allowed = _ENUMS[key]
                    matched = ""
                    for candidate in allowed:
                        if val.strip().lower() == candidate.lower():
                            matched = candidate
                            break
                    if not matched:
                        LOG.warning("配置项 %s=%r 非法，回退为 %r", key, cur, default)
                        matched = default
                    val = matched
                self[key] = val
            elif isinstance(default, list):
                if not isinstance(cur, list):
                    self[key] = copy.deepcopy(default)
        # 清晰度降档阶梯：统一小写逗号分隔，过滤非法/重复项
        ladder: list = []
        for part in str(self.get("quality_ladder") or "").replace("，", ",").split(","):
            value = part.strip().lower()
            if value in _QUALITY_LADDER_VALUES and value not in ladder:
                ladder.append(value)
        if not ladder:
            ladder = list(_QUALITY_LADDER_DEFAULT)
        self["quality_ladder"] = ",".join(ladder)

        # 交叉校验：退避/暂停的上限不能小于起始值
        if self.get("poll_backoff_max", 0) < self.get("poll_backoff_start", 0):
            self["poll_backoff_max"] = self["poll_backoff_start"]
        if self.get("breaker_pause_max", 0) < self.get("breaker_pause_start", 0):
            self["breaker_pause_max"] = self["breaker_pause_start"]

        # 通知配置是嵌套字典，单独合并默认值
        notify = self.get("notify")
        if not isinstance(notify, dict):
            notify = {}
        merged_notify = copy.deepcopy(NOTIFY_DEFAULTS)
        merged_notify.update({k: v for k, v in notify.items() if k in NOTIFY_DEFAULTS})
        if not isinstance(merged_notify.get("webhooks"), list):
            merged_notify["webhooks"] = []
        if not isinstance(merged_notify.get("events"), list):
            merged_notify["events"] = list(NOTIFY_DEFAULTS["events"])
        merged_notify["enabled"] = _coerce_bool(merged_notify.get("enabled"), False)
        merged_notify["sound"] = _coerce_bool(merged_notify.get("sound"), True)
        self["notify"] = merged_notify

        # 兼容旧版配置 output_format=mp4 -> remux_mp4
        legacy = self.pop("output_format", None)
        if legacy == "mp4":
            self["remux_mp4"] = True

        # 相对输出目录 -> 相对项目根解析（保存时保留用户原值，使用时解析）
        if not self.get("output_dir"):
            self["output_dir"] = str(DEFAULT_OUTPUT_DIR)
        if not self.get("log_dir"):
            self["log_dir"] = str(DEFAULT_LOG_DIR)

    # ---------------- 便捷访问 ----------------
    def get_int(self, key: str, default: int = 0) -> int:
        return _coerce_int(self.get(key, default), default)

    def get_float(self, key: str, default: float = 0.0) -> float:
        return _coerce_float(self.get(key, default), default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        return _coerce_bool(self.get(key, default), default)

    def get_str(self, key: str, default: str = "") -> str:
        val = self.get(key, default)
        return val if isinstance(val, str) else str(val)

    def get_list(self, key: str) -> list:
        val = self.get(key)
        return val if isinstance(val, list) else []

    # ---------------- 路径 ----------------
    @property
    def output_root(self) -> Path:
        """输出根目录（相对路径按项目根解析）。"""
        return resolve_path(self.get_str("output_dir") or str(DEFAULT_OUTPUT_DIR))

    @property
    def log_dir_path(self) -> Path:
        return resolve_path(self.get_str("log_dir") or str(DEFAULT_LOG_DIR))

    def ensure_dirs(self) -> None:
        for p in (self.output_root, self.log_dir_path):
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                LOG.error("创建目录失败 %s: %s", p, exc)


def resolve_path(value: str) -> Path:
    """把配置里的路径解析为绝对路径：相对路径相对项目根。"""
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p


def load_config(path: Optional[Path] = None) -> Config:
    """加载配置并确保目录存在（供 run.py / GUI / CLI 调用）。"""
    cfg = Config.load(path)
    cfg.ensure_dirs()
    # 把「最短保留时长」注入录制引擎：Manager 构造 RoomRecorder 时未显式传参则用它。
    # （manager.py 由其他人维护，这里用模块级默认值解耦，避免修改它）
    try:
        from . import recorder as _recorder
        _recorder.set_default_min_keep_seconds(cfg.get_int("min_keep_seconds"))
    except Exception:  # pragma: no cover - 极端导入失败不影响启动
        LOG.warning("注入 min_keep_seconds 失败", exc_info=True)
    return cfg


if __name__ == "__main__":  # 简单自检：python src/config.py
    logging.basicConfig(level=logging.INFO)
    c = load_config()
    print(json.dumps(c, ensure_ascii=False, indent=2))
