# 模块接口契约（Lead 定义，改动需同步 lead）

目标：本地自用「抖音直播录制工具」。Python 3.13 + tkinter + ffmpeg（子进程）。运行：`python run.py`。

## 目录/文件归属（写权限边界）
- `analysis/static/**`            → static-re
- `analysis/dynamic/**`           → dyn-re
- `analysis/probe/**`、`docs/douyin_api.md` → dy-proto
- `src/dy_api.py`                 → dy-proto（唯一写者）
- `src/` 其余文件、`run.py`、`assets/`、`README.md`、`docs/report.md` → recorder-dev（lead 集成时也会改 src 根文件，需先在任务里说明）
- `docs/ENV.md`、`docs/INTERFACES.md` → lead

## src/dy_api.py 契约（唯一协议层）
必须提供：

```python
@dataclass
class LiveStatus:
    is_live: bool
    room_id: str = ""          # web_rid（live.douyin.com/<rid> 里的数字）
    title: str = ""
    anchor_name: str = ""
    cover_url: str = ""
    started_at: int = 0        # unix 秒，0=未知
    streams: dict = field(default_factory=dict)  # {"flv": [url...], "hls": [url...], "origin": {...}}
    raw: dict = field(default_factory=dict)
    error: str = ""            # 非空表示本次查询失败（网络/风控/房间不存在）

def resolve_target(text: str) -> dict:
    """接受直播链接/房间号/分享文案，返回 {"web_rid": str|None, "sec_uid": str|None, "url": str}"""

def get_live_status(target: str, cookie: dict | None = None, timeout: float = 15) -> LiveStatus:
    """target 可为 web_rid、直播间 URL 或分享文案；返回直播状态。内部负责 cookie(ttwid) 初始化与缓存、失败重试。"""

def choose_stream(status: LiveStatus, prefer: str = "flv", quality: str = "origin") -> str | None:
    """按优先级取可录制流地址（flv 优先，其次 hls）。"""

def get_cookie() -> dict:   # {"ttwid": "...", ...} 供手动复用
```

约定：
- 纯标准库 + `.pylibs` 可用库；不引入浏览器自动化/Playwright。
- 失败必须返回 LiveStatus(error=...) 而不是抛未捕获异常（网络抖动时上层要能继续）。
- 自带 `python src/dy_api.py --selftest <target>` 自测入口，打印状态 JSON。
- 报告里写清接口、参数、签名/风控处理方式、实测证据（时间、房间号、返回）。

## 录制引擎（recorder-dev）
- 用 ffmpeg 子进程：`-i <stream_url> -c copy` 单文件，或 `-f segment` 分段；FLV 直录，需要时 remux 成 mp4。
- 需求：按房间任务并发录制、断流自动重连（退避）、分段（按时长/大小）、下播自动停止并收尾、磁盘保留策略、录制事件回调、日志。
- 配置 `config.json` + `watchlist.json`（UTF-8，路径相对项目根，可配置输出目录）。
- GUI：tkinter，列表显示 主播/状态/本次时长/文件大小，支持增删改、开始/停止、日志面板；后台线程跑监控循环，不阻塞 UI。
- 无头模式：`python run.py --cli`。

## 验收（lead 负责跑）
1. 用真实在线房间测通 status + 录制 ≥60s 文件可播放。
2. 断流重连、下播停止、分段、多任务并发、退出清理子进程。
3. GUI 能启动、增删主播、显示状态。
4. 文档：README 使用说明 + 逆向分析报告 docs/report.md。
