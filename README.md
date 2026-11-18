# 抖音直播录制工具（本地自用版）

一个用 **Python 标准库 + ffmpeg** 实现的抖音直播自动录制工具：监控主播是否开播，开播后自动直录 FLV/TS，下播自动停止并转成 MP4。

目标场景：**个人本地长期挂机、监控 1000+ 直播间、批量录制/整理**。无登录、无授权校验、无广告，所有数据都在本机。

> 仅用于个人学习与本地备份，请遵守抖音平台规则与主播版权。

---

## 功能特性

### 监控与录制

- 开播自动录制、下播自动停止；支持原画/高清/标清，默认原画优先并自动回退可用档位；
- **零转码**：ffmpeg `-c copy` 直录 FLV/TS，CPU 占用低；结束后可 remux 成 MP4；
- **断流自愈**：ffmpeg 内部重连 + 上层重新取流双保险，重连间隔指数退避；
- **卡死看门狗**：录制文件连续多次不增长自动重启 ffmpeg；
- **清晰度自动降档**：一场内断流/卡死达到阈值后自动降一档重开；
- **分段录制**：按你设置的分段时长切段；可选「分段结束立即转 MP4」（v2.5.4 新增，默认开启）；
- **过短录制自动删除**：时长低于阈值（默认 30 秒）自动清理，避免碎片；
- **单次录制时长上限**、单文件大小上限、断流重试上限均可配。

### 磁盘与空间保护

- **保留空间立即生效**：设置里的「最小剩余空间」保存后立刻应用到所有在录任务；空间低于阈值立即停止全部录制并暂停开录，恢复后自动继续；
- **转 MP4 前空间预检**：剩余空间不足时不再生成半截 MP4，而是按设置**删除无法转换的文件**，不留临时错误文件；
- **转 MP4 并发限制**（默认 2）：避免几十路同时 remux 写爆磁盘；
- **分段即时转**：每段写完立刻转 MP4 并删源，峰值占用大幅降低；
- 支持按保留天数 / 每目录文件数自动清理旧录像。

### 主播与数据管理

- 主播列表**不分页**，全部主播一个滚动列表；表头「全选」勾选全部（筛选时勾选当前筛选结果）；
- **批量导入**：粘贴链接/分享文案，或导入 Excel/CSV；**Excel 会读取全部工作表（sheet）**，每个 sheet 独立识别表头（无表头的名单也能读），自动去重，导入后显示成功/重复/失败数量；
- **导出主播列表**为 xlsx/csv（含分组、画质、开关、备注等）；
- 批量设置：自动录制、画质、分组、参与监控；批量开始/停止录制、立即检查；
- **无效直播间自动停用**：确认条目无效（只有昵称/房间号错）连续 N 次后自动停用，不再反复查询；右键可「重新启用并检查」；
- **主播活跃度**：记录每个主播最近一次开播时间，列表显示「最近开播」；录制记录页可打开活跃度窗口，按最久未开播排序、统计从未/超 7 天/超 30 天，并导出 CSV。

### 界面与体验

- 中文 GUI（青屿风格）：监控 / 录制记录 / 运行日志 / 设置 / 关于 五个页面；
- 主播详情抽屉显示实时状态、房间号、最近开播、时长、大小、清晰度、排队原因；
- **实时直播预览**：ffmpeg 解码管道实时出画面（约 1~3 秒延迟），建议同时开 1~3 个窗口；
- 列表刷新时保持选中行与滚动位置，详情不会莫名跳回「未选择主播」；
- 关闭窗口 / 停止监控时等待录制收尾和 MP4 转换（默认最多等 600 秒，可配 0=无限）。

### 数据与诊断

- 按天日志 `logs/run-YYYY-MM-DD.log`；SQLite 运行记录 `data/history.db`；
- **导出诊断包**：配置（已脱敏）、系统信息、ffmpeg 探测、日志、运行统计打包 zip，一键反馈问题；
- **补转历史 MP4**：把目录里遗留的 FLV/TS 批量转成 MP4；
- 内置 mock 直播源，离线也能跑通录制/GUI/CLI 流程。

### 1000+ 直播间长期挂机

- 自适应轮询：直播中 30s / 近期开播 90s / 长期离线 300s；
- 全局限速与固定工作线程池，失败指数退避 + 全局熔断；
- 默认不限制并发录制（`max_concurrent=0`），由带宽/进程/磁盘闸门自动排队；
- 协议层 gzip + keep-alive，1000+ 主播轮询流量和连接数大幅下降；
- 1000 主播 / 5 小时浸泡测试：峰值 114 路并发、RSS 约 82MB，详见 [docs/soak_unlimited_5h_report.md](docs/soak_unlimited_5h_report.md)。

---

## 两种用法

### 1. 免安装（推荐日常使用）

Windows x64 发布包（`DouyinRecorder_win64.zip`）可从 **GitHub Releases** 下载，或按本文「打包」一节在本地生成：

- 解压后双击 **`DouyinRecorder.exe`** 即可运行；
- 另有 `DouyinRecorder-CLI.exe` 带控制台的命令行入口，适合计划任务。

程序不需要安装 Python 或 ffmpeg，Win10/11 x64 双击即用。

### 2. 源码运行

要求 Windows 10/11、Python 3.10+（实测 3.13），**无第三方 Python 依赖**；ffmpeg 放在 `bin/ffmpeg.exe` 或在设置里指定。

```powershell
python run.py          # 启动 GUI
python run.py --check  # 环境自检（Python/ffmpeg/目录/协议层）
```

也可以双击根目录的 `启动.bat`。

---

## 快速上手

### 添加主播

点「添加主播」，支持以下任意形式（可粘贴整条分享文案）：

1. 纯数字房间号：`76274427953`
2. 直播间链接：`https://live.douyin.com/76274427953`
3. 手机分享短链/文案：`https://v.douyin.com/iQLgKSj/ ...`
4. 主播主页链接：`https://www.douyin.com/user/MS4wLjABAAAA...`

名称可留空，程序会联网补全；画质默认「原画」，网络不稳时可改「高清/标清」。

### 开始监控

点「开始监控」后按自适应间隔轮询所有启用主播；开播且开启自动录制的房间自动开始录制。

常用操作：

- 右键主播：立即检查 / 重新启用并检查 / 打开输出目录 / 打开直播间 / 编辑 / 删除；
- 「批量录制」：对勾选主播开始/停止录制、立即检查；
- 「批量设置」：修改自动录制、画质、分组、参与监控；
- 搜索框 + 状态筛选：只看直播中/录制中/未开播/错误等；
- 表头「全选」：选中当前列表全部主播（列表已不分页）。

### 导入 / 导出主播

- 「＋ 导入直播间」支持粘贴文本或选择 Excel/CSV 文件；**多 sheet 的 Excel 会全部读取**，文件提示里显示「N 个 sheet · M 行」；导入完成会显示：成功导入 / 已存在（重复）/ 失败（无法识别或缺少链接）；
- 只有昵称、没有链接的行会以「不参与监控」导入，避免无效轮询；
- 「导出」把当前主播列表导出为 xlsx/csv，便于备份和编辑。

### 录制记录 / 活跃度 / 补转

- 「录制记录」页：按今天/7 天/30 天筛选，查看每场录制的时长、大小、状态；
- 「主播活跃度」：最近开播时间、多久没开播、从未开播，支持导出 CSV；
- 「补转历史 MP4」：把输出目录里未转换的 FLV/TS 补转成 MP4；
- 「导出诊断包」：生成脱敏 zip，反馈问题时直接发这个文件。

---

## 录制与磁盘策略（重点）

### 基本流程

```text
调度线程（自适应轮询 + 限速 + 闸门）
  ├─ 开播 + 自动录制 + 闸门允许 → 启动 RoomRecorder（ffmpeg 子进程）
  ├─ 录制中 + 查询到未开播      → 停止录制并收尾
  └─ 录制线程结束               → 回收状态、转 MP4、按保留策略清理

RoomRecorder:
  ffmpeg -c copy 直录 FLV/TS → 监控磁盘/卡死/下播/时长 → 断流重连
  收尾：转 MP4（分段模式每段写完即转），删除源文件可选保留
```

### 分段即时转 MP4（v2.5.4）

- 在「设置 → 录制 → 分段时长」设置分段（分钟，0=不分段）；
- 分段时长 > 0 时，每段录制完成立即转 MP4 并删除该段 FLV（可在同一页关闭「分段结束立即转 MP4」）；
- 产物是多个按序号命名的 MP4（如 `主播_2026-11-15_193000_000.mp4`），不再整场合并；正在写的最后一段不会被提前处理；
- 收尾时会转完最后一段，并清理 0 字节空分段。

### 空间不足时

每段转换前检查：`剩余空间 >= 该段大小 + 300MB`。

- 不够：删除该段源文件 → **立即停止本场录制** → 通知监控停止全部录制并暂停开录；
- 转换失败（尤其 `No space left on device`）：删除半成品 MP4 和源文件，不保留临时错误文件；
- 非分段模式收尾空间不足：同样删除本场源文件（`keep_source=true` 时保留）；
- 空间恢复到「最小剩余空间」以上后自动解除暂停。

### 建议配置

- 「最小剩余空间」建议 40GB 左右；
- 分段时长建议 30 分钟；
- 开启「分段结束立即转 MP4」，峰值磁盘占用最小；
- 若磁盘较小，可配合「保留天数 / 每目录最大文件数」自动清理。

---

## 命令行

```powershell
python run.py --check                 # 环境自检
python run.py --cli                   # 无窗口常驻监控，Ctrl+C 退出
python run.py --once                  # 只查询一次所有主播状态
python run.py --list                  # 列出主播
python run.py --add 76274427953 --name 某主播
python run.py --remove <id或房间号>
python run.py --history --days 7      # 最近 7 天录制记录与统计
python run.py --export-diagnostics 诊断.zip
python run.py --activity-report 活跃度.csv
python run.py --convert-leftovers     # 补转历史 FLV/TS
python run.py --export-watchlist 主播列表.xlsx
python run.py --import-file 主播列表.xlsx --import-group 重点
python run.py --mock --cli            # 用内置 mock 源离线演示
```

通用参数：`--config <路径>`、`--watchlist <路径>`、`--log-level DEBUG|INFO|WARNING|ERROR`。

打包版对应 `DouyinRecorder-CLI.exe <参数>`。

---

## 配置说明

首次运行自动生成 `config.json`；GUI「设置」页保存后立即生效。常用项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `poll_interval_live` | 30 | 直播中复查间隔（秒） |
| `poll_interval_warm` | 90 | 近期开播主播复查间隔（秒） |
| `poll_interval_cold` | 300 | 长期离线主播复查间隔（秒） |
| `poll_rate_limit` | 6 | 全局查询限速（次/秒），1000+ 可到 6~10 |
| `max_poll_workers` | 12 | 查询线程数，1000+ 可 16~24 |
| `max_concurrent` | 0 | 同时录制上限，0=不限（受带宽/进程/磁盘闸门约束） |
| `max_total_mbps` | 0 | 录制总带宽预算（Mbps），0=不限 |
| `max_ffmpeg_processes` | 300 | ffmpeg 进程上限，0=不限 |
| `quality` | `origin` | 默认画质：origin/best/hd/sd |
| `quality_degrade_enabled` | true | 断流自动降档 |
| `remux_mp4` | true | 录制结束/分段结束转 MP4 |
| `keep_source` | false | 转 MP4 后保留 FLV/TS |
| `remux_concurrency` | 2 | 同时转 MP4 的数量（1~8） |
| `segment_duration` | 0 | 分段时长（秒），0=不分段 |
| `segment_remux_now` | true | 分段结束立即转 MP4（分段>0 生效） |
| `segment_size_mb` | 0 | 单文件大小上限（MB），0=不限 |
| `min_keep_seconds` | 30 | 最短保留时长（秒），低于则删除本场，0=关 |
| `min_free_gb` | 2.0 | 最小剩余空间（GB），低于则停录 |
| `keep_days` | 0 | 录像保留天数，0=不自动清理 |
| `keep_max_files` | 0 | 每目录最多文件数，0=不限 |
| `record_timeout_min` | 0 | 单次录制最长分钟，0=不限 |
| `shutdown_wait_seconds` | 600 | 停止/关闭时等待收尾和转 MP4 的上限，0=不限 |
| `invalid_auto_disable_after` | 3 | 连续 N 次确认无效后自动停用，0=不停用 |
| `stall_check_seconds` | 30 | 卡死检查间隔，0=关闭 |
| `stall_kill_after` | 2 | 连续无增长次数达到后重启 ffmpeg |
| `retry_max` | 5 | 断流最大重连次数 |
| `retry_interval` / `retry_interval_max` | 5 / 120 | 重连间隔（秒）与上限 |
| `output_dir` | `records` | 录制根目录 |
| `output_template` | `{anchor}/{date}` | 子目录模板 |
| `filename_template` | `{anchor}_{date}_{time}` | 文件名模板 |
| `container` | `flv` | 录制容器 flv/ts |
| `ffmpeg_path` | 空 | 留空自动找 `bin/ffmpeg.exe` / PATH |
| `notify.*` | 关闭 | webhook / 声音通知，见下 |
| `log_keep_days` | 30 | 日志保留天数 |

完整默认值与注释见 `src/config.py`。

### 输出模板占位符

`{anchor}`/`{name}` 主播名，`{room}`/`{web_rid}` 房间号，`{date}`（2026-11-15）、`{date8}`、`{time}`、`{datetime}`、`{year}`/`{month}`/`{day}`、`{title}` 直播标题。

默认输出：`records/主播名/2026-11-15/主播名_2026-11-15_193000.mp4`。

### 通知

支持企业微信、钉钉、飞书群机器人 webhook（按域名自动识别格式），可选 Windows 开播提示音。

```json
"notify": {
  "enabled": true,
  "webhooks": ["https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"],
  "sound": true,
  "events": ["start", "live", "stopped", "error"]
}
```

通知后台发送、失败只记日志，不影响录制。

---

## 打包（生成 exe / zip）

构建机要求 Windows x64 + Python 3.10+。首次准备 PyInstaller（装到项目内 `.pylibs`，不污染全局环境）：

```powershell
cd D:\Code\DS-Workspace\douyin-recorder
python -m pip install --target .pylibs pyinstaller pyinstaller-hooks-contrib altgraph packaging pefile pywin32-ctypes
```

一键打包：

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

产物：

- `dist/DouyinRecorder/`：绿色版目录（双击 `DouyinRecorder.exe`）；
- `dist/DouyinRecorder_win64.zip`：给别人的压缩包。

可选参数：`-SkipZip`（只出目录）、`-NoSmokeTest`（跳过 `--check`）、`-DistDir <目录>`（程序正在运行时打包到别处）、`-Python <路径>`。
`bin/ffmpeg.exe` 必须存在，构建时会一起打进包里。

如希望把运行包一起提交到仓库，可把 `dist/DouyinRecorder` 与 zip 同步到 `release/`；注意这样仓库体积会明显增大，长期更推荐把大文件放 GitHub Releases 或使用 Git LFS。

---

## 项目结构

```text
douyin-recorder/
├─ run.py                   入口（GUI/CLI 统一）
├─ 启动.bat                 Windows 双击启动
├─ config.json              配置（首次运行生成）
├─ watchlist.json           主播列表（首次添加生成）
├─ bin/ffmpeg.exe           自带 ffmpeg（不入 Git）
├─ assets/                  示例文件
├─ records/                 默认录制目录
├─ logs/                    运行日志
├─ data/history.db          运行记录（SQLite）
├─ release/                 本地发布副本（app/ + zip，可选，不入 Git）
├─ build/                   PyInstaller 打包脚本/配置/图标
├─ docs/                    设计/协议/打包/测试报告等文档
├─ tests/                   自动化测试
├─ src/
│  ├─ gui.py                GUI（监控/录制记录/日志/设置/关于）
│  ├─ manager.py            监控调度（轮询/开录/闸门/通知）
│  ├─ recorder.py           单房间录制引擎（重连/分段/即时转 MP4/磁盘保护）
│  ├─ checker.py            直播状态查询
│  ├─ dy_api.py             抖音取流协议层
│  ├─ storage.py            主播列表持久化（原子写）
│  ├─ config.py             配置默认值/校验
│  ├─ model.py              数据模型/文件名模板
│  ├─ disk.py               磁盘空间与保留策略
│  ├─ ffmpeg_util.py        ffmpeg 查找/命令/remux
│  ├─ remux_legacy.py       补转历史 MP4
│  ├─ history.py            运行记录（SQLite）
│  ├─ activity.py           主播活跃度统计
│  ├─ import_util.py        批量导入解析/去重
│  ├─ export_util.py        导出 xlsx/csv
│  ├─ notify.py             通知（webhook/声音）
│  ├─ mock_api.py           离线 mock 直播源
│  ├─ instance_guard.py     单实例限制
│  ├─ ui_theme.py           主题与控件
│  └─ ...
└─ analysis/                本地逆向分析产物（不入 Git）
```

---

## 开发与测试

测试全部基于标准库，使用 `src/mock_api.py` 启动本机 HTTP-FLV 测试源，不依赖真实抖音流：

```powershell
python -m compileall -q src
python tests\test_recorder.py       # 录制引擎（基础/重连/分段/下播/MP4）
python tests\test_segment_remux.py  # 分段即时转 MP4 + 空间不足删除
python tests\test_disk_policy.py    # 保留空间立即生效/诊断包
python tests\test_manager.py        # 调度/自动开录/并发闸门
python tests\test_gui.py            # GUI 冒烟
python tests\test_history.py        # 运行记录/统计
python tests\test_import.py         # Excel/CSV 导入
python tests\test_multisheet_import.py  # 多 sheet Excel 导入
python tests\test_export.py         # 导出
python tests\test_real.py           # 真实抖音流联调（需网络，默认 30 秒）
```

当前共 26 个测试文件，覆盖录制、调度、GUI、多 sheet 导入导出、磁盘策略、诊断包、活跃度、单实例、清晰度降档等；改动后建议全跑一遍。

---

## 常见问题

**Q：提示找不到 ffmpeg / 无法录制？**

把 `ffmpeg.exe` 放到项目 `bin/ffmpeg.exe`，或在「设置 → ffmpeg 路径」填绝对路径；运行 `python run.py --check` 看最终解析结果。

**Q：能查到直播间但拉不到流 / 403 / 空数据？**

拉流地址带签名且有时效，本工具每次开播/重连都会重新取流；部分房间有风控或地区限制，可在设置里配置代理/cookie；原画多人同时录更容易断流，可改高清或降低带宽预算。

**Q：磁盘满了怎么办？**

v2.5.4 起空间不足会立即停录并删除无法转换的文件；把「最小剩余空间」设为 40GB 左右、开启分段即时转 MP4，会显著降低峰值占用。日常可用「保留天数/每目录最大文件数」自动清理。

**Q：为什么分段录制后是多个 MP4，而不是一个？**

这是「分段结束立即转 MP4」的预期行为：每段单独转，避免整场结束才转、临时占用大量空间。需要整段合并可关闭「分段结束立即转 MP4」，或自行用 ffmpeg concat。

**Q：停止监控/关闭窗口时为什么还在转？**

程序会等待录制收尾和 MP4 转换完成，默认最多等 600 秒（`shutdown_wait_seconds`，0=无限）；分段即时转模式下通常只需收尾最后一段。

**Q：诊断包会泄露隐私吗？**

导出时会自动打码 cookie/token/webhook、Windows 用户名；watchlist 只统计数量，不包含主播名/链接。

**Q：能同时预览多个直播吗？**

可以，实时预览每个窗口一个 ffmpeg 解码进程；建议同时 1~3 个，太多会占 CPU/带宽。

**Q：单实例限制？**

同一数据目录只允许运行一个实例（防止重复录制/写坏列表）；多开请使用不同 `--config` / `--watchlist` 目录。

---

## 已知限制

- 只做录制与转封装，不做转码/剪辑/上传；
- 抖音接口可能变化，取流失败请先跑 `--check` / `--once` 排查；
- 高并发原画录制对带宽和磁盘 IO 要求高（1080p 原画约 6.4Mbps ≈ 2.9GB/小时/路）；
- 打包版未签名，首次运行 SmartScreen 需「更多信息 → 仍要运行」，杀软可能误报，加白名单即可；
- 仅提供 Windows x64 打包版，GUI 需要桌面会话（服务器可用 CLI）；
- 运行包（app/zip）默认不作为源码提交；如果要把 exe 也放进 Git，仓库体积会增大，长期建议使用 GitHub Releases 或 Git LFS。

---

## 相关文档

| 文档 | 说明 |
| --- | --- |
| [docs/packaging.md](docs/packaging.md) | 打包说明（构建/体积/SmartScreen/替换 ffmpeg） |
| [docs/douyin_api.md](docs/douyin_api.md) | 抖音直播取流协议手册 |
| [docs/design_v2.md](docs/design_v2.md) | v2 设计（UI/性能/清晰度） |
| [docs/acceptance_v21.md](docs/acceptance_v21.md) | 验收记录与功能证据 |
| [docs/soak_report.md](docs/soak_report.md) | 1000 主播浸泡测试报告 |
| [docs/soak_unlimited_5h_report.md](docs/soak_unlimited_5h_report.md) | 不限并发 5 小时浸泡测试 |

---

## 更新日志

### v2.5.7（2026-11-18）
- **磁盘保留值过高时自动下调**：容量调优改用「磁盘总容量」判定——保留值 > 10GB 且 ≥ 总容量 5% 时自动降为总容量的 2%（约 2~10GB）；磁盘看门狗每轮再校正一次，避免录满后长时间停止录制；
- 新增 `--cleanup`：补转输出目录残留 flv/ts，并按 ffmpeg 解析时长删除低于 `min_keep_seconds` 的短视频（清理旧版本遗留的 flv 与短片段）。

### v2.5.6（2026-11-17）
- 修复右键「开始/停止录制」只作用于勾选框、而非右键选中行的问题；手动录制排队/失败原因明确展示；
- 录制端每次重连**重新获取抖音流地址**（带签名地址有时效），解决「启动后 30 秒未收到数据」空转；
- 清晰度自动降档不再把「启动后无数据」计入断流，避免原画被误降为 hd/sd；
- 磁盘恢复增加滞回，避免在保留值附近反复停录/开录；
- 转 MP4 前空间校验改为 **2×size**（源与目标同时存在），remux/concat 所有失败分支统一清理半成品 MP4；
- 新增启动**容量自动调优**（按 CPU/网卡带宽/磁盘剩余回填并发、带宽、磁盘保留值；`max_concurrent` 保持不限，保证所有开播主播都能录制）。

### v2.5.5（2026-11-15）
- Excel 批量导入支持**读取多个工作表（sheet）**：按工作簿顺序读取全部 sheet，每个 sheet 独立识别表头；
- 无表头（只有昵称+链接）的 sheet 也能正确导入；导入提示显示「N 个 sheet · M 行」；
- 表头识别改为精确匹配，避免昵称含「主播」等字被误判成表头而漏行；
- 新增 tests/test_multisheet_import.py。

### v2.5.4（2026-11-15）
- 分段录制每段写完立即转 MP4 并删源（默认开启，可关）；
- 空间不足立即停止本场录制，删除无法转换的源文件/半成品/0 字节空分段；
- 录制端发现磁盘致命问题会通知 manager 停止全部录制并暂停开录。

### v2.5.3（2026-11-15）
- 保留空间设置保存后立即生效，低于阈值立即停录并暂停；
- 转 MP4 空间预检、失败删除半成品；转 MP4 并发限制；
- 修复导出诊断包选择路径后没有生成文件。

### v2.5.2（2026-11-15）
- 无效直播间连续 N 次确认后自动停用，右键可重新启用并检查；
- 未开播/网络错误不会被误停用。

### v2.5.1（2026-11-15）
- 新增主播活跃度：最近开播时间、未开播天数、导出 CSV；
- 列表显示「最近开播」，支持从录像文件时间回填。

### v2.5.0（2026-11-15）
- 主播列表去掉分页，单一滚动列表；全选勾选全部；默认排序保持原顺序。

更早版本记录见 Git 提交历史。

---

## 声明

本项目为个人使用的本地录制工具，不提供任何破解、授权绕过或平台限制规避功能；请遵守抖音平台规则和主播版权，合理使用。