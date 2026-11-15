# 抖音直播录制工具（本地自用版）

一个纯 Python 标准库 + ffmpeg 实现的「抖音直播自动录制」工具：监控主播是否开播，开播自动直录 FLV/TS，
下播自动停止，支持 **1000+ 直播间长期挂机监控**、批量导入/批量操作、断流重连、卡死看门狗、分段、
磁盘保留、过短录制自动删除、群机器人通知，以及清新简洁的中文图形界面（风格 A「青屿」）。

**两种用法**：① 直接运行源码（需 Python 3.10+）；② 解压 `dist/DouyinRecorder_win64.zip` 双击 exe（免 Python，见 [docs/packaging.md](docs/packaging.md)）。

> 仅用于个人学习与直播内容本地备份，请遵守抖音平台规则与主播版权；本项目不做任何授权绕过/破解。

## 特性

- **直播状态监控**：按可配置间隔轮询 watchlist 中的所有房间，自动开录/停录，失败指数退避。
- **零转码录制**：ffmpeg `-c copy` 直录 FLV，CPU 占用极低（1 路 1080p 约 1% CPU）。
- **断流自愈**：ffmpeg 内部短重连 + 上层「重新取流地址再重启」双保险；重连间隔指数递增。
- **卡死看门狗**：录制文件连续 N 次不增长判定 ffmpeg 卡死，自动 kill 并重新取流重启（与原版逻辑一致）。
- **分段录制**：按秒数分段（`-f segment`），支持单文件大小上限，文件名带主播名+日期+时间。
- **格式策略**：录制容器 flv/ts；结束后可选 remux 成 MP4（不转码），可选保留原文件，多段自动 concat 合并。
- **磁盘守护**：剩余空间低于阈值自动停录；支持按天数/文件数保留策略自动清理。
- **通知**：企业微信 / 钉钉 / 飞书 群机器人 webhook（开播/开录/结束/错误），Windows 开播提示音。
- **中文 GUI**：主播表格、工具栏、日志面板、设置对话框；后台线程监控，关闭窗口优雅停录并等待 ffmpeg 退出。
- **CLI/无头模式**：`python run.py --cli` 常驻监控，`--once` 只查一次，适合服务器/PowerShell 计划任务。
- **离线可测**：内置 mock 直播源（本机 HTTP-FLV），无网络也能跑通录制/GUI/CLI 全流程。
- **批量导入/批量操作**：一行一个粘贴导入（自动识别/跳过无效行）；表格多选后批量开始/停止录制、批量设置（自动录制/画质/分组/启用）。
- **过短录制自动删除**：一场录制总时长低于设定值（默认 30 秒，可关）自动删除该场全部产物，避免留下碎片。
- **运行记录 + 诊断包**：按天日志 + SQLite 运行记录；一键导出脱敏诊断包，方便反馈问题。
- **免 Python 打包**：PyInstaller onedir 打包（GUI + CLI 双入口，自带 ffmpeg），Win10/11 x64 解压即用。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11（也已兼容 Linux/macOS 的基本路径逻辑，但未实测） |
| Python | 3.10+（本项目在 Python 3.13.14 实测），**不需要安装任何第三方库** |
| GUI | Python 自带 tkinter（Windows 官方安装包默认包含） |
| ffmpeg | 推荐使用项目自带的 `bin/ffmpeg.exe`（gyan.dev build 2025-02-26）；程序会自动检测常见安装位置，找不到时提示下载 |
| 免安装包 | 不想装 Python：用 `dist/DouyinRecorder_win64.zip`（解压双击 `DouyinRecorder.exe`） |

## 目录结构

```
douyin-recorder/
├─ run.py                  程序入口（GUI/CLI 统一入口）
├─ 启动.bat                 Windows 双击启动（等价于 python run.py）
├─ config.json             配置文件（首次运行自动生成）
├─ watchlist.json          主播列表（GUI/CLI 添加后自动生成）
├─ bin/ffmpeg.exe          自带 ffmpeg
├─ assets/                 示例文件等
├─ records/                默认录制输出目录
├─ logs/                   运行日志（UTF-8，按 5MB 轮转）
├─ src/                    源码
│  ├─ config.py            配置加载/默认值/校验/损坏重建
│  ├─ model.py             数据模型（主播条目/状态/事件/文件名模板）
│  ├─ storage.py           watchlist 持久化（原子写、损坏备份）
│  ├─ logger.py            日志初始化（文件 UTF-8 + 控制台）
│  ├─ ffmpeg_util.py       ffmpeg 查找/命令构造/remux/子进程清理
│  ├─ disk.py              磁盘空间检查与保留策略清理
│  ├─ recorder.py          单房间录制引擎（ffmpeg 子进程/断流重连/分段/看门狗）
│  ├─ checker.py           直播状态查询（按契约调用 dy_api，失败降级 mock）
│  ├─ manager.py           监控调度（轮询/自动录制/并发/通知/事件）
│  ├─ mock_api.py          离线 mock 协议层 + 本地 HTTP-FLV 测试源
│  ├─ gui.py               清新风格中文界面（左侧导航：监控/录制记录/运行日志/设置/关于）
│  ├─ ui_theme.py          UI 主题与自定义控件（圆角按钮/状态胶囊/开关/导航，高 DPI）
│  ├─ history.py           运行记录（SQLite：录制/运行/错误）
│  ├─ cli.py               命令行模式
│  ├─ notify.py            群机器人/声音通知
│  └─ dy_api.py            抖音直播取流协议层
├─ data/history.db         运行记录数据库（自动生成）
├─ build/                  打包脚本（build.ps1 + spec + 图标）
├─ dist/                   免 Python 打包产物（DouyinRecorder/ + zip）
└─ tests/                  机制测试（录制/GUI/监控/最短时长/ffmpeg 检测/历史记录/真实流）
```

## 快速开始

### 0. 不想装 Python？用打包版

解压 `dist/DouyinRecorder_win64.zip` 到**可写目录**（不要放 Program Files），双击 `DouyinRecorder.exe` 即可；
脚本/计划任务用同目录的 `DouyinRecorder-CLI.exe`（带控制台）。详见 [docs/packaging.md](docs/packaging.md)。

### 1. 启动图形界面

双击 `启动.bat`，或在项目根目录执行：

```bat
python run.py
```

首次运行会自动生成 `config.json`、`watchlist.json`、`records/`、`logs/`。

### 2. 添加主播

点「添加主播」，在直播间输入框里直接粘贴以下任意一种形式（支持整段分享文案）：

1. 纯数字房间号：`76274427953`
2. 电脑直播间链接：`https://live.douyin.com/76274427953`
3. 手机分享短链：`https://v.douyin.com/iQLgKSj/`（或粘贴整条分享文案）
4. 电脑主播主页链接：`https://www.douyin.com/user/MS4wLjABAAAA...`

名称可留空，程序会联网补全主播昵称；画质默认「原画」，网络不稳时建议改「高清/标清」（原画多人同时录制容易断流）。

### 3. 开始监控

点「开始监控」，程序会按 `check_interval`（默认 60 秒）轮询所有启用主播：开播且开启自动录制的房间会自动开始录制。

**批量操作**（1000+ 直播间场景）：
- 表格支持 Ctrl/Shift 多选；「批量录制 ▾」→ 对选中主播 **开始录制 / 停止录制 / 立即检查**；
- 「批量设置 ▾」→ 批量修改 **自动录制 / 画质 / 分组 / 参与监控**；
- 顶部「开始监控 / 停止监控」是全局开关；「批量导入」可一次粘贴上千条链接；
- 右侧「详情」抽屉显示选中主播的实时状态、输出目录和最近文件；底部可展开运行日志。

### 4. 命令行模式

```bat
python run.py --check                 :: 环境自检（Python/ffmpeg/目录/协议层）
python run.py --cli                   :: 无 GUI 常驻监控，Ctrl+C 优雅退出
python run.py --once                  :: 只查询一次所有主播的直播状态
python run.py --list                  :: 列出主播列表
python run.py --add 76274427953 --name 某主播   :: 添加主播
python run.py --remove <id或房间号>   :: 删除主播
python run.py --mock --cli            :: 用内置 mock 测试源跑通流程（离线演示）
```

通用参数：`--config <路径>` 指定配置文件，`--watchlist <路径>` 指定主播列表，`--log-level DEBUG|INFO|WARNING|ERROR`。

### 5. 运行记录与诊断包

```bat
python run.py --import-file 主播列表.xlsx --import-group 重点  :: 从 Excel/CSV 批量导入（自动去重）
python run.py --history --days 7                  :: 最近 7 天录制记录 + 今日/7日统计
python run.py --export-diagnostics out.zip        :: 导出脱敏诊断包（配置/日志/统计/系统信息）
```

- 日志：`logs/run-YYYY-MM-DD.log`，按天保存，`log_keep_days` 天自动清理；
- 数据库：`data/history.db`（每场录制/每次运行/错误记录），GUI「录制记录」页可筛选查看；
- 诊断包已自动脱敏（cookie/webhook/token 打码、用户名打码、watchlist 只统计数量），可直接发给维护者分析。

## 配置说明（config.json）

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `poll_interval_live` | `30` | 直播中复查间隔（秒），快速发现下播/断流 |
| `poll_interval_warm` | `90` | 24 小时内开播过的主播复查间隔（秒） |
| `poll_interval_cold` | `300` | 长期未开播的主播复查间隔（秒），降风控/省流量 |
| `poll_rate_limit` | `6` | 全局轮询限速（次/秒，±20% 抖动）；1000+ 房间建议 6-10 |
| `max_poll_workers` | `12` | 轮询线程数，1000+ 房间可加到 16-24 |
| `resolve_workers` | `3` | 批量导入时解析主页链接/短链的线程数 |
| `check_interval` | `60` | **旧版兼容字段**，P1 起轮询由上面三个档位控制 |
| `quality` | `origin` | 默认清晰度：origin=原画优先（默认最高档）/ best=按分辨率码率自动最高 / hd / sd |
| `quality_degrade_enabled` | `true` | 断流自动降档开关 |
| `quality_degrade_after_reconnects` | `2` | 本场累计断流/卡死达到几次后降一档 |
| `quality_ladder` | `origin,hd,sd` | 降档阶梯（逗号分隔） |
| `min_keep_seconds` | `30` | 最短保留时长（秒）：整场总时长低于此值自动删除产物，0=关闭 |
| `log_keep_days` | `30` | 运行日志保留天数，0=不清理 |
| `max_concurrent` | `0` | 同时录制上限：**0 = 不限**，由带宽/进程/磁盘闸门自动调度 |
| `max_total_mbps` | `0` | 总带宽预算（Mbps）：0 = 不限；原画约 6.4Mbps/路，超预算排队 |
| `max_ffmpeg_processes` | `300` | ffmpeg 进程上限：0 = 不限，防进程过多拖垮系统 |
| `use_mock_api` | `false` | true 时不联网，使用内置 mock 测试源（离线演示/自测） |
| `cookie` | `""` | 可选，手动指定 ttwid cookie（一般留空，协议层自动获取） |
| `proxy` | `""` | 可选，HTTP 代理，如 `http://127.0.0.1:7890` |
| `output_dir` | `<项目根>/records` | 录制输出根目录（相对路径相对项目根） |
| `output_template` | `{anchor}/{date}` | 房间子目录模板（相对 output_dir） |
| `filename_template` | `{anchor}_{date}_{time}` | 文件名模板（不含扩展名） |
| `ffmpeg_path` | `""` | ffmpeg 路径；**留空自动**：`bin/ffmpeg.exe` → 原版目录 → PATH |
| `ffmpeg_extra_args` | `[]` | 追加给 ffmpeg 的输出侧参数（如 `["-max_muxing_queue_size","1024"]`） |
| `ffmpeg_http_args` | 见下 | HTTP 流输入参数模板（UA/Referer/reconnect/genpts 等），可自行增删 |
| `container` | `flv` | 录制容器：`flv`（默认，直录最稳）或 `ts` |
| `remux_mp4` | `true` | 录制结束后是否 remux 成 MP4（`-c copy` 不转码） |
| `keep_source` | `false` | remux 成 MP4 后是否保留原始 flv/ts 文件 |
| `segment_duration` | `0` | 分段时长（秒），`0`=不分段（原版默认 300 秒但默认关闭） |
| `segment_size_mb` | `0` | 单文件大小上限（MB），超过后自动换新文件，`0`=不限 |
| `stall_check_seconds` | `30` | 卡死看门狗检查间隔（秒），`0`=关闭 |
| `stall_kill_after` | `2` | 连续多少次文件不增长判定卡死并重启录制 |
| `quality` | `origin` | 默认画质 `origin`/`hd`/`sd`（新添加主播的默认值） |
| `retry_max` | `5` | 断流后最多连续重连次数（之后收尾） |
| `retry_interval` | `5` | 首次重连间隔（秒），之后指数递增 |
| `retry_interval_max` | `120` | 重连间隔上限（秒） |
| `min_free_gb` | `2.0` | 磁盘剩余低于该值（GB）停止录制并告警 |
| `keep_days` | `0` | 录像保留天数，`0`=不自动清理 |
| `keep_max_files` | `0` | 每个目录最多保留录像文件数，`0`=不限 |
| `record_timeout_min` | `0` | 单次录制最长分钟数，`0`=不限 |
| `graceful_stop_timeout` | `20` | 停止时等待 ffmpeg 退出的秒数 |
| `notify.enabled` | `false` | 是否启用群机器人/声音通知 |
| `notify.webhooks` | `[]` | 群机器人 webhook 地址列表（企业微信/钉钉/飞书） |
| `notify.sound` | `true` | 开播提示声音（Windows 蜂鸣） |
| `notify.events` | `["start","live","stopped","error"]` | 需要通知的事件 |
| `log_dir` | `<项目根>/logs` | 日志目录 |
| `log_level` | `INFO` | 日志级别 |
| `ui_refresh_ms` | `1000` | GUI 表格刷新间隔（毫秒） |

`ffmpeg_http_args` 默认值（参考原版程序内存中的 ffmpeg 命令模板）：

```text
-user_agent <Chrome UA> -referer https://live.douyin.com/
-timeout 15000000 -rw_timeout 15000000
-reconnect 1 -reconnect_on_network_error 1 \
-reconnect_on_http_error 403,404,500,502,503,504 -reconnect_streamed 1 \
-reconnect_delay_max 10 -reconnect_max_retries 3 -reconnect_delay_total_max 30
-fflags +genpts -analyzeduration 5000000 -probesize 5000000
```

其中 `-reconnect_max_retries` / `-reconnect_delay_total_max` 是给 ffmpeg 内部重连设的上限：
签名地址失效时 ffmpeg 不会无限重试卡死，而是退出后由上层重新调用取流接口换新地址。

## 主播列表（watchlist.json）

由 GUI/CLI 维护，也可手工编辑（UTF-8 JSON）：

```json
{
  "items": [
    {
      "id": "a1b2c3d4e5f6",
      "name": "主播名",
      "target": "https://live.douyin.com/76274427953",
      "web_rid": "76274427953",
      "url": "https://live.douyin.com/76274427953",
      "auto_record": true,
      "quality": "origin",
      "output_template": "",
      "enabled": true,
      "note": "",
      "added_at": 1763000000
    }
  ]
}
```

`enabled=false` 的房间不参与监控；`auto_record=false` 只监控不自动录制。

## 输出目录与文件名模板

输出路径 = `output_dir / output_template`，文件名 = `filename_template` + 扩展名，可用占位符：

| 占位符 | 含义 |
| --- | --- |
| `{anchor}` / `{name}` | 主播名（已清洗 Windows 非法字符） |
| `{room}` / `{web_rid}` | 房间号 |
| `{date}` | 日期 `2026-11-13` |
| `{date8}` | 日期 `20261113` |
| `{time}` | 时间 `193000` |
| `{datetime}` | `20261113_193000` |
| `{year}` `{month}` `{day}` | 年/月/日 |
| `{title}` | 直播标题（已清洗） |

示例：默认配置下会输出 `records/主播名/2026-11-13/主播名_2026-11-13_193000.flv`（remux 后为 `.mp4`）。

## 通知配置

在 GUI「设置」里勾选「启用通知」并粘贴 webhook（每行一个），或直接改 config.json：

```json
"notify": {
  "enabled": true,
  "webhooks": [
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
    "https://oapi.dingtalk.com/robot/send?access_token=xxx",
    "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
  ],
  "sound": true,
  "events": ["start", "live", "stopped", "error"]
}
```

程序按域名自动选择企业微信/钉钉/飞书的消息格式；也可写成 `{"type":"feishu","url":"..."}` 显式指定。
通知在后台线程发送（5 秒超时），失败只记日志，不影响录制。

## 常见问题

**Q1：提示「未找到 ffmpeg」/ 无法录制？**

把 ffmpeg.exe 放到项目 `bin/ffmpeg.exe`，或在 GUI「设置 → ffmpeg 路径」填绝对路径，或加入系统 PATH。
然后运行 `python run.py --check` 查看最终解析到的 ffmpeg 路径与版本。

**Q2：能查到直播间但拉不到流 / 提示 403、空数据？**

- 抖音拉流地址带签名且有时效（通常几分钟），本工具每次开播/重连都会重新调用取流接口，不会复用旧地址；
- 部分房间有风控/地区限制，可能需要在 `config.json` 里配置 `cookie`（ttwid）或代理；
- 原画在多房间同时录制时更容易断流，建议把画质改成「高清」，或降低 `max_total_mbps` 预算。

**Q3：断流后为什么会有多个文件？**

每次重连会写新文件（避免覆盖已录内容），文件名追加 `_r1`、`_r2`；如果 `remux_mp4=true`，多段会先 concat 合并成一个 MP4。

**Q4：怎么判断主播下播了？**

监控循环每轮查询直播状态，明确「未开播」时立即停止录制并收尾；录制中若 ffmpeg 自己退出且确实有数据，会走断流重连。

**Q5：想按 30 分钟一段？**

设置 `segment_duration=1800`；分段默认关闭（原版默认 300 秒但同样默认不启用）。

**Q6：磁盘满了怎么办？**

`min_free_gb` 默认 2GB，低于该值会停止新录制并告警；可配合 `keep_days` / `keep_max_files` 自动清理旧录像。

**Q7：中文路径/主播名会有问题吗？**

所有路径用 pathlib 处理，ffmpeg 参数以参数列表传递（不经 shell），中文路径实测正常；文件名会自动剔除 `\/:*?"<>|` 等非法字符。

**Q8：录制中的文件能直接播放吗？**

FLV 正在写入时部分播放器可以播放；建议停止录制（或下播）后再播放。remux 在录制停止后自动执行，大文件需要几秒。

**Q9：退出时 ffmpeg 残留？**

窗口关闭会优雅停止所有录制并等待 ffmpeg 退出；Windows 下还有 `taskkill /F /T` 兜底杀进程树。若程序被强杀，可手动结束 cmdffmpeg/ffmpeg 进程。

**Q10：能挂后台不显示窗口吗？**

用 `python run.py --cli` 常驻；或用 `pythonw run.py` 启动 GUI（无控制台，日志仍写入 logs/）。

## 如何替换 ffmpeg

1. 下载 Windows 版 ffmpeg（推荐 gyan.dev 的 essentials build）；
2. 用新 `ffmpeg.exe` 覆盖项目 `bin/ffmpeg.exe`（或放在任意位置并在设置里指定路径）；
3. 运行 `python run.py --check` 确认版本；录制只用到 `-c copy`、`-f segment`、concat remux，几乎所有 build 都支持。

## 录制机制说明

```text
调度线程（优先队列 + 固定工作池 + 全局限速 + 自适应档位，见「性能」配置）
   ├─ 开播 + 自动录制 + 并发未满 → 启动 RoomRecorder（ffmpeg 子进程）
   ├─ 录制中 + 查询到未开播      → 停止录制并收尾
   └─ 录制线程自行结束           → 回收状态、按保留策略清理

RoomRecorder：
  spawn ffmpeg -i <每次重连重新获取的流地址> -c copy ... <文件>
  监控：进程退出 / 磁盘空间 / 是否下播 / 录制时长上限 / 文件是否卡死
  断流：递增退避后重新取流地址再启动（新文件）
  收尾：可选 remux MP4 + concat 合并，返回最终文件列表
```

## 与原版工具的功能对照与差异

| 能力 | 原版商业工具 | 本工具 |
| --- | --- | --- |
| 授权 | 需购买/登录（日志出现「登录失败：1003，已过期」） | 无授权、无登录、无联网校验 |
| 轮询间隔 | 约 10 分钟 | 默认 60 秒，可配置（更及时） |
| 录制方式 | 调用 cmdffmpeg.exe 直录 | 同样 ffmpeg `-c copy` 直录 |
| 断流重连 | 有（内部重连参数） | ffmpeg 内部重连 + 上层重新取流双保险 |
| 卡死处理 | 按监测间隔对比文件大小，二次确认后重启 | 同思路，`stall_check_seconds`/`stall_kill_after` 可配 |
| 分段 | 默认 300 秒，默认关闭 | 同（默认关闭，可配） |
| 磁盘保留 | 约 4024MB 阈值 | 按剩余空间 + 天数/文件数策略，阈值可配 |
| 通知 | 企业微信/钉钉/飞书 + 开播提示音 | 同（webhook 自动识别平台） |
| 数据文件 | dyuser.json（加密，620 主播） | watchlist.json（明文，可手工编辑） |
| 界面 | 商业 GUI（VMProtect 保护） | tkinter 中文 GUI，代码完全开源可改 |
| 播放器 | 内置 PotPlayer | 不内置，可自行用系统播放器打开输出文件 |
| 主播名自动更新 | 有 | 有（查询到昵称时补全；改名后未录制时可手动编辑） |

## 开发与测试

```bat
python tests\test_recorder.py   :: 录制引擎机制（基础/断流重连/分段/下播停止/MP4/失败收尾）
python tests\test_manager.py    :: 监控调度（自动开录/下播停止/并发上限）
python tests\test_gui.py        :: GUI 冒烟测试（窗口/表格/监控启停）
python tests\test_real.py       :: 真实抖音流联调（需网络，默认录 30 秒）
python tests\test_min_keep.py   :: 最短保留时长自动删除
python tests\test_ffmpeg_detect.py :: ffmpeg 自动检测候选链
python tests\test_history.py    :: 运行记录/统计/诊断包
```

测试使用 `src/mock_api.py` 在本机启动一个 HTTP-FLV 测试源（ffmpeg testsrc2 生成），不依赖真实抖音流；
`tests/test_real.py` 会通过 `src/dy_api.py` 自动发现一个在播房间做真实录制验证。


## 1000+ 直播间长期挂机（v2.1 性能模式）

- **导入**：监控页「批量导入」→ 可粘贴，也可直接选 Excel/CSV 文件；自动识别表头、自动去重（房间号/主页 sec_uid/URL）。
  主页链接首次需要解析（每个约 4-5 次请求），1000+ 条首次解析约 3–15 分钟（一次性，缓存 12 小时），期间监控会照常工作。
- **默认不限并发**：`max_concurrent=0`、`max_total_mbps=0`，由带宽/进程/磁盘闸门自动调度；超闸门的房间显示「排队中」，
  闸门释放后 1 秒内自动开录（详情里能看到排队原因）。
- **限速与档位**：全局 `poll_rate_limit=6` 次/秒；直播中 30s / 近期开播 90s / 长期离线 300s 自适应轮询；
  1000+ 房间可把 `max_poll_workers` 提到 16–24、`poll_rate_limit` 提到 6–10（太高易触发风控）。
- **省流量**：协议层自动 gzip + keep-alive（1000 次轮询流量约 -82%，连接数 1035→12）。
- **清晰度**：默认原画优先（缺失自动回退 HD/SD）；每主播可单独选「跟随最高/高清/标清」。
- **长时间运行**：失败退避、全局熔断（错误率过高自动暂停并刷新会话）、卡死看门狗、磁盘保留、过短录制自动删除。
- **浸泡测试**：1000 主播 / 10% 在线 / 10 路并发的 1 小时基线报告见 [docs/soak_report.md](docs/soak_report.md)。

## 已知限制

- 只做录制，不做转码/剪辑/上传；需要转码可自行用 ffmpeg 处理输出文件。
- 抖音接口随时可能变化，若取流失败请先运行 `python run.py --once` 和 `python src/dy_api.py --selftest` 排查。
- 未实现原版的「主播名变更自动重命名文件夹」等增强功能（属可选 enhancement）。
- 高并发原画录制对上行带宽/磁盘 IO 要求高（原画 1080p ≈ 6.4Mbps ≈ 2.9GB/小时/路）：1000+ 房间同时开播时，
  请按实际带宽设置上限或改用高清/标清；后续性能版本会加入「带宽预算自动调度」。
- 打包版未签名：首次运行 SmartScreen 需「更多信息 → 仍要运行」，杀软可能误报（PyInstaller 通病），加白名单即可；
  仅支持 Win10/11 x64，GUI 需要桌面会话（服务器请用 CLI 版）。

## 致谢

- ffmpeg（gyan.dev build）
- 抖音直播取流协议调研（docs/douyin_api.md）

## 相关文档

| 文档 | 说明 |
| --- | --- |
| [docs/report.md](docs/report.md) | 逆向分析 + 自研工具交付报告（目标程序技术栈/保护壳/协议/录制链路/功能对照/验收记录） |
| [docs/douyin_api.md](docs/douyin_api.md) | 抖音直播取流协议手册（接口/参数/错误码/风控对策/更新方法） |
| [docs/migrate_anchors.md](docs/migrate_anchors.md) | 原版主播列表迁移说明（含从系统痕迹恢复的 394 个主播昵称） |
| [docs/packaging.md](docs/packaging.md) | 免 Python 打包说明（构建/体积/SmartScreen/替换 ffmpeg） |
| [docs/design_v2.md](docs/design_v2.md) | v2 设计方案（UI 风格/1000+ 性能/清晰度/登录策略） |
| [docs/mockups/](docs/mockups) | UI 效果图（A/B/C 三种风格） |
| [analysis/](analysis/) | 逆向分析原始产物（静态/动态/协议探测/可复现脚本） |

## 更新日志

### v2.5.3（2026-11-15）
- 磁盘保护：设置里的「最小剩余空间」保存后立即生效——低于保留值时立刻停止所有录制并暂停开录，恢复后自动继续；
- 转 MP4 空间预检：空间不足以写出 MP4 时保留 FLV/TS 原文件（不再生成损坏的半截 MP4），腾出空间后可用「补转历史 MP4」转换；
- 转 MP4 并发限制（默认 2，设置→录制可调），避免多路同时 remux 瞬间写爆磁盘；
- 修复「导出诊断包」选择路径后没有生成文件：改为程序内生成（打包版不再依赖 spawn run.py），并弹窗提示成功/失败。

### v2.5.2（2026-11-15）
- 无效直播间自动停用：连续 N 次（默认 3，可在 设置→轮询 调整，0=不停用）确认「条目无效」（只有昵称/房间号错/链接错）后自动停用，不再反复查询；
  未开播（enter 无房间数据等）按离线正常轮询；网络/风控错误仍按退避重试；
- 右键新增「重新启用并检查」，被停用的主播可一键恢复检测；停用状态会持久化。

### v2.5.1（2026-11-15）
- 新增「主播活跃度」：记录每个主播最近一次开播时间（持久化到 watchlist），列表新增「最近开播」列；
- 录制记录页「主播活跃度」窗口：按最久未开播排序，统计从未开播/超 7 天/超 30 天，支持导出 CSV；
- 对没有 last_live_at 记录的主播，会从录像目录最新文件时间近似回填，方便分析历史；
- 命令行：python run.py --activity-report 活跃度.csv

### v2.5.0（2026-11-15）
- 主播列表去掉分页：所有（筛选后的）主播一次性放进表格，用滚动条/滚轮查看；「全选」即勾选全部，不再只选当前页；
- 底部显示「共 N 个主播 · 已勾选 M 个」；默认排序改为保持列表原顺序，避免监控中行频繁跳动；
- 600 行实测渲染 8.8ms，1000+ 也流畅。

### v2.4.9（2026-11-15）
- 修复「选中主播后详情/直播画面几秒后自动回到"选择主播"」：表格因状态/排序变化整表重建时会清空选中行，现在重建后恢复选中与滚动位置；
- 直播画面改为**连续实时播放**：独立窗口用 ffmpeg 管道解码（约 1-3 秒延迟，8fps），不是每 4 秒一张截图；关闭窗口自动结束解码进程；
- 详情小封面仍保留定期抓帧（省 CPU），点封面打开实时播放窗口。

### v2.4.8（2026-11-14）
- 导入结果统计更清晰：弹框显示 成功导入 / 已存在（重复跳过）/ 失败（无法识别/缺链接）+ 当前总数；文件里无法识别的行现在也计入失败数；
- 导入对话框实时统计改为「将导入 N 个 · 已存在（重复）N 个 · 无法识别 N 个」。

### v2.4.7（2026-11-14）
- 修复按钮“消失”：开始/停止监控、导入直播间、添加、导出列表 之前以整页为父控件创建、再用 pack(in_=容器) 放置，
  Windows 上会被后创建的工具栏帧覆盖（看不见但占位）；现在改为直接以工具栏/头部为父控件创建，彻底修复；
- 回归测试新增父容器断言，防止再次出现“看不见但布局占位”的问题。

### v2.4.6（2026-11-14）
- 修复所有子窗口（添加主播、导入直播间、批量设置、正在退出、直播画面预览）打开时跑到屏幕左上角的问题：统一居中显示在主窗口中间。

### v2.4.5（2026-11-14）
- 修复「导入直播间」不容易找到的问题：把「＋ 导入直播间 / ＋ 添加 / 导出列表」从标题栏移到监控页工具栏左侧（搜索框旁边），更醒目；
- 顶栏只保留「开始监控 / 停止监控」；按钮宽度紧凑化，最小窗口下也不会被挤压变形；
- 列表为空时，表格中央显示引导提示「点击上方 ＋ 导入直播间 批量导入（Excel / CSV / 粘贴链接）」。

### v2.4.4（2026-11-14）
- 停止监控/关闭软件时，先停止所有录制并等待收尾：把已录制的文件全部转成 MP4 后才退出（退出时有进度窗口，显示剩余路数）；
- 新增设置「退出时等待转换上限（秒）」（默认 600，0=一直等）；超时会询问继续等待还是强制退出；
- 新增「补转历史 MP4」：录制记录页按钮 / 命令行 --convert-leftovers，把输出目录里残留的 .flv/.ts 批量补转为 MP4（旧版本异常退出遗留）；
- 说明：转 MP4 时机 = 每场录制结束时（下播/手动停止/达到单次时长/连续失败停止），分段录制会先合并再转；
  关闭软件以前只等 40 秒就退出，可能中断转换，现已修复。

### v2.4.3（2026-11-14）
- 修复「打开目录」打开到软件目录的问题：config 的 output_root/log_dir_path 是属性，GUI 误当函数调用导致异常回退到当前工作目录；
  现在右键/详情「打开目录」正确打开该主播的录像目录（日期目录未创建时打开主播目录），「打开日志目录」同样修复；
- 同时修复详情「最近文件」与「输出目录」显示（此前因同一异常一直为空）；新增 tests/test_open_dir.py 回归。

### v2.4.2（2026-11-14）
- 设置 → 录制 新增「保存目录结构」：可选 主播/日期（默认）、仅主播（所有录像直接放主播目录，不再建日期文件夹）、日期/主播、标题/日期、自定义模板；
- 自定义模板支持 {anchor} {date} {time} {title} {room} 占位符；选「仅主播」时文件名仍带日期时间，不会重名；
- 配置项 output_template（全局）；单主播的输出模板仍可单独覆盖。

### v2.4.1（2026-11-14）
- 新增「导出列表」：一键把当前全部主播（名称/房间号/主页链接/分组/画质/自动录制/参与监控/输出模板/备注）导出为 Excel 或 CSV；
  升级版本后用「批量导入」选择该文件即可恢复全部主播与设置（仅昵称的旧条目会以「停用」保留，不丢数据）；
- 命令行：python run.py --export-watchlist 主播列表.xlsx
- 已对现有 172 条列表做实机导出→重新导入验证：172 条全部恢复（141 启用 + 31 仅昵称停用）。

### v2.4.0（2026-11-14）
- 详情「播放」改为实时直播画面：选中开播中的主播自动用 ffmpeg 抓帧显示（约每 8 秒刷新），点封面打开独立预览窗口（每 4 秒刷新），可一键打开抖音主页/直播间或用播放器打开；
- 右键主播新增「跳转到抖音主页」「浏览器打开直播间」「查看直播画面」；
- 查询错误分类优化：「账号当前没有直播间/未开播」不再显示为错误（按离线处理），只有「条目只有昵称/房间号非法」才标记错误并给出明确提示；
- 批量导入自动跳过「只有昵称、没有房间号/主页链接」的无效行，并在结果弹框中说明数量。

### v2.3.3（2026-11-14）
- 监控页重构：操作按钮回到顶部工具栏（修复按钮被错误布局到左侧中间的问题）；列表支持拖动分隔条调整大小、横竖滚动条；
- 主播列表新增勾选列（全选/清空/批量设置按勾选生效），批量操作更方便；
- 搜索框点击可聚焦（修复占位文字挡住输入框导致点击无反应）；
- 设置页「分段时长」「最短保留时长」单位改为分钟（内部仍按秒存储，兼容旧配置）；
- 保存设置后弹框提示成功/失败；
- 勾选框改为自绘风格（不再用系统原生 Checkbutton）；
- 未解析出房间号时显示「待解析」，主播名与房间号不再重复。

### v2.3.2（2026-11-14）
- 新增单实例锁：重复启动（GUI/--cli）会被拒绝（退出码 3），避免同一主播被重复录制；进程退出自动释放。

### v2.3.1（2026-11-14）
- 解析缓存落盘（data/resolve_cache.json）：重启后不再重新冷启动（实测 12.5x 加速）；--check 显示缓存条目。

### v2.3.0（2026-11-14）
- 新增命令行批量导入：python run.py --import-file 主播列表.xlsx 支持 --import-group 分组
- 批量导入不再逐条刷日志；打包版 exe 已验证可直接导入 Excel。

### v2.2.1（2026-11-14）
- 修复 recorder 的 is_live_fn 语义 bug（断流曾被误判为下播）；不改变已验证行为。

### v2.2.0（2026-11-14）
- 清晰度：默认最高档（原画优先，可选自动最高 best）；断流自动降档（origin→hd→sd，下播恢复）；
- GUI 设置新增「清晰度」分组，列表/详情显示实际清晰度与「已降档」；状态栏显示过短删除计数；
- 全量 10 套测试通过。

### v2.1.1（2026-11-13）
- 协议层：无直播间账号负缓存（重复查询 0 请求）；版本号统一 2.1.1。

### v2.1（2026-11-13）
- 性能：gzip + keep-alive（1000 次轮询流量 -82%、连接 1035→12）；调度器重写（限速/自适应轮询/熔断/并发闸门）；
- 导入：GUI 支持 Excel/CSV 批量导入 + 自动去重；1000+ 主播不限并发（0=不限）；
- 清晰度：协议层支持按元数据选最高档（默认原画优先）；
- **修复**：卡死看门狗误杀超过 30 秒的健康录制（_total_size 恒为 0 的严重 bug），并补回归测试；
- 新增 1 小时 mock 浸泡测试报告（docs/soak_report.md）。

### v2.0（2026-11-13）
- 清新风格 UI（风格 A）、批量操作、最短保留时长自动删除、ffmpeg 自动检测、运行记录与诊断包、免 Python exe。
