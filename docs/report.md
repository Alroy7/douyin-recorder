# 抖音直播录制工具：逆向分析与自研本地录制工具 交付报告

> 交付日期：2026-11-13　|　分析对象：`D:\抖音录制\抖音直播录制工具`（商业软件，只读分析）
> 交付物：本地自用版「抖音直播录制工具」（Python 源码 + GUI + CLI + 文档）
> 边界声明：本报告只做**互操作性分析**与**自研替代实现**，未破解授权、未绕过卡密/付费校验、未修改目标程序任何文件。

---

## 0. 摘要

| 维度 | 结论 |
|---|---|
| 目标程序本质 | 易语言(EPL) + 黑月编译器 + E2EE 2.7.2 Web 框架 + 静态链接 OpenSSL/libcurl，外层 **VMProtect 3.x** 加壳 |
| 静态可读性 | 文件内业务字符串 ≈ 0（全部在熵 7.99 的加密节里），必须靠运行时内存 dump |
| 运行时情报 | 成功 dump 187MB/246 区域，挖出抖音接口、ffmpeg 命令模板、配置键、UI 文案、授权协议 |
| 记录/监控机制 | 600 秒轮询 → 抖音直播间接口取流 → `cmdffmpeg.exe -c copy` 直录 ts → 可选转 mp4；按文件大小不增长判断卡死 |
| 账号数据 | `dyuser.json` 为强加密密文（≈612B/主播），密钥在壳内且**只在卡密登录成功后**解密；本机卡密已过期 → 无法解密 |
| 数据抢救 | 改从 Windows 使用痕迹恢复出 **394 个主播昵称** + 录制命名模板（合法、非破解） |
| 自研工具 | 协议链路**完全打通**（无需 a_bogus/X-Bogus 签名），已用真实直播间完成取流+录制实测，GUI/CLI/监控/分段/重连/通知全部就绪 |

---

## 1. 分析对象

| 文件 | 大小 | MD5 | 说明 |
|---|---|---|---|
| `抖音直播录制工具.exe` | 45,973,504 | dc2fb7a516dc54208cf5000c2e49ff42 | 主程序，PE32 i386，12 节，无版本信息，无数字签名 |
| `抖音直播录制工具守护助手.exe` | 20,557,824 | 96d49c3cc4a27320888064a928f353f2 | 外置守护/升级器（本次未运行） |
| `安装升级.exe` | 24,952,832 | 4a2106fd27990564b10361f6545ad7f0 | 安装/升级程序 |
| `dyuser.json` | 499,688 | — | 620 个主播的密文名单（base64 文本） |
| `log\*.log` | 18 个 | — | GBK 日志，2026-09-21 ~ 11-13，仅生命周期消息 |
| `PotPlayer\` | 176 文件/318MB | — | 自带播放器；其中 `cmdffmpeg.exe` 是真 ffmpeg（gyan build 2025-02-26） |
| `抖音主播数据备份\` | 11 个 | — | dyuser.json 的整文件字节级备份 |

`PotPlayer.7z`（87MB）是自更新组件残留；`抖音直播录制工具下载地址.txt` 指向作者的语雀说明文档。

---

## 2. 逆向方法与合规边界

| 阶段 | 方法 | 产物 |
|---|---|---|
| 静态分析 | PE 结构/节区熵/资源提取/多编码字符串（ASCII/UTF-16LE/GBK） | `analysis/static/REPORT.md`、`extracted/` |
| 壳判定 | 节名、Manifest 身份名、节数据剥离特征、IAT 抽稀、熵分布 | 同上 §2 |
| 运行时分析 | Python ctypes 启动 + `OpenProcess/VirtualQueryEx/ReadProcessMemory` 全内存 dump（187MB/246 区域）后多编码挖掘 | `analysis/static/dumps/`、`extracted/strings_dump_mined.md` |
| 动态行为 | 400ms 轮询进程/连接/DNS、文件与注册表前后快照、WER 崩溃 dump 挖掘 | `analysis/dynamic/REPORT.md`、`raw/` |
| 数据抢救 | Windows Recent/跳转列表/TypedPaths 痕迹解析 | `analysis/dynamic/decrypted/ANCHORS_RECOVERED.md` |
| 协议验证 | 独立实现 + 目标程序自带 ffmpeg 实读验证 | `analysis/probe/`、`analysis/lead/` |

**明确未做**：注册机/卡密生成、license 校验 patch、VMProtect 脱壳还原、破解数据加密、绕过付费墙。目标目录零写入（只读）。

---

## 3. 静态分析结论

### 3.1 保护壳：VMProtect 3.x（铁证）

1. 三个 exe 均含 **`.vmps0` / `.vmps1`** 节（VMProtect 3.x 专用节名，4.x 已改名）；
2. 守护助手 RT_MANIFEST 里 `assemblyIdentity name="T-VMProtect"`（加壳模板注入的默认名）；
3. `.text/.rdata/.data` 的 `SizeOfRawData = 0`，原始节数据被剥离；
4. 整包塞进单个高熵节：主程序 `./Hl` 45.9MB、**熵 7.9958**（守护助手 .vmps1 熵 7.9967、安装升级 7.9977）；
5. 入口点在壳节内；导入表被抽稀到 **18 个 DLL / 18 个函数**（且都是诱饵 API）；无重定位、无调试目录、无 TLS、无签名、无版本资源。

结论：文件静态字符串对业务情报**零价值**（严格过滤后中文只剩自身文件名），必须运行时 dump。

### 3.2 资源

只有图标（语言 2052）+ Manifest + 空壳导出表（`e2ee_Cache*` 12 个，三个 exe 完全一致，是框架残留）。Manifest 关键项：`requestedExecutionLevel level="requireAdministrator"`（**必须管理员运行**）、`longPathAware`、Common-Controls 6.0。无 RT_VERSION/RT_DIALOG/RT_STRING → 界面由代码动态创建。

### 3.3 技术栈（运行时证据，来自 187MB dump）

- **易语言(E语言)** 编写：dump 中出现大量易语言运行时符号、控件名（235 个，如 `按钮_解绑卡密`、`标签_到期时间`）、易语言错误文案；
- **E2EE 2.7.2 Web 框架**：运行时会向 `%TEMP%` 释放 `E2EECore.2.7.2.dll`（8.8MB、**993 个导出**：`e2ee_cryptoEncrypt/Decrypt`、`e2ee_httpclient*`、`e2ee_httpserver*`、`e2ee_mapToJSON`），已留档于 `analysis/dynamic/raw/e2ee/`；
- 静态链接 **OpenSSL / libcurl / websocketpp / boost**（dump 里上千个 OpenSSL 符号）；
- 因此它自带完整 HTTP 客户端、加密、JSON、本地 HTTP 服务能力——但界面/业务是易语言写的。

---

## 4. 运行时情报（内存 dump 主要收获）

### 4.1 配置文件：明文 GBK INI（完整 40 键）

路径：`%APPDATA%\douyinlive\set.dat`（788 字节，GBK，无加密）。这是**最有价值的可复用资产**，完整键名与默认值见 `analysis/dynamic/REPORT.md` §3.3，要点：

| 键 | 值 | 含义 | 自研对应 config 字段 |
|---|---|---|---|
| 主播监测间隔 | 600 | 轮询周期（秒） | `check_interval` |
| 视频保存目录 | `D:\抖音录制\直播` | 输出根目录 | `output_dir` |
| 目录创建格式 | `[主播文件夹]` | 每主播一层目录 | `output_template` |
| 视频保存格式 | 1 | 三选一：不转码只留 ts / 转 mp4 删源 / 转 mp4 留源 | `container` + `remux_mp4` + `keep_source` |
| 分段时长 / 大小 / 开关 | 300 / 0 / 0 | 分段策略 | `segment_duration` / `segment_size_mb` |
| 保留磁盘空间 | 4024 (MB) | 磁盘保护 | `min_free_gb` |
| 判断两次监测间隔录制文件的大小 | 1 | 卡死判定 | `stall_check_seconds` / `stall_kill_after` |
| webHook地址 | 企业微信机器人 URL | 开播/状态推送 | `notify.webhooks` |
| 开播提示声音 | 1 | 开播提示音 | `notify.sound` |
| 视频播放器 | 0 | MPV/PotPlayer | 系统默认播放器 |
| 软件空闲时长 / 自动退出 | 300 / 0 | 无开播 N 分钟后自动退出 | 未实现（可由任务计划代替） |

### 4.2 日志（GBK）与生命周期

18 个文件 1286 行，归一化后只有 10 种模板：`软件初始化成功` / `登录成功（卡密）` / `JSON数据初始化——成功` / `运行正常`（**每 10 分钟一条，共 1214 条**）/ `服务器连接失败1` / `用户设定空闲300分钟后自动关闭退出…` / `退出成功（卡密）` / `登录失败（1003/1004 卡密已过期）`。

→ 推断生命周期：启动 → 写 set.dat → 联网自检 → `/login` 卡密校验 → 通过后解密 dyuser.json → 600 秒周期监控 → 空闲 300 分钟自动退出并注销。**录制明细不写日志**（这是原版的一个缺陷，自研版已补齐）。

### 4.3 目录/文件名模板（从 Windows Recent/跳转列表恢复的真实样本）

```
D:\抖音录制\直播\主播昵称（5位hex短ID）\[已转码_]主播昵称_YYYY年M月D日H时M分S秒_画质标记.ts|.mp4
例：D:\抖音录制\直播\乐歌官方旗舰店（FC85C）\已转码_乐歌官方旗舰店_2026年9月26日23时10分5秒_原画M.mp4
```

- 5 位 hex 短 ID 是程序为每个主播生成的稳定标识（疑似 room_id/sec_uid 派生，不可逆）；
- 原始录制为 `.ts`，19/25 个样本带 `已转码_` 前缀 → 程序会把 ts 转成 mp4；
- 还有 `.mp4_cleanup_state.txt` 之类的清理状态文件。

---

## 5. 授权与自更新（只记录，不破解）

| 项 | 内容 |
|---|---|
| 授权体系 | **E2EE 授权中心**（框架自带商业模块，非作者自研）；卡密 + 机器码 + 服务端校验 |
| 登录端点 | `http://129.211.120.156:12580/login?nonce=<32hex>&timestamp=<unix>&sign=<32hex>`（sign 为 MD5 特征），同 IP 另有 `:13579` 端口（用途未定，疑心跳/管理） |
| 错误码 | `1003` = 卡密已过期（请购买或续费）；`1004` = 卡密已过期（请直接购买） |
| 本机状态 | 卡密 `DYZBcAEy6dCCVWuWjkYV3G`，2026-11-06 起报 1003 → 已过期 |
| 自更新 | 组件清单 JSON（内存中完整捕获）：守护助手 / 安装升级 / PotPlayer.7z 走**蓝奏云 + 飞书镜像**，带 MD5 校验、提取码 6666；3 个 MD5 与本地文件逐一核对**全部一致** |
| 系统行为 | 内存中可见"写 Run 键(douyinlive)、清空系统代理(ProxyEnable=0)、改 HideFileExt、wmic 查磁盘"等能力；本次未观测到实际落地（未走到该逻辑/沙箱限制） |

> 说明：本机卡密已过期 + 程序需要管理员权限 + 沙箱低完整性 → 每次启动 33 秒内 `0xC0000005` 崩溃（2026-11-06 用户自己启动也一样）。这是我们无法采集"登录后/录制中"行为的原因，也说明原版程序当前已不可用。

---

## 6. 数据格式：`dyuser.json`（620 主播名单）

### 6.1 结论：当前条件下无法解密

实验矩阵（14 项，详见 `analysis/dynamic/decrypted/DYUSER_ANALYSIS.md`）：

| 实验 | 结果 |
|---|---|
| 编码 | 纯 base64（无 JSON 包裹/BOM/空白），解码后 190KB~404KB |
| 熵 | 7.9990~7.9996/8，卡方均匀 |
| 分组特征 | 12 个文件长度 %16 ∈ {0,8,12}；37 万字节内 **0 个重复 16B 块** |
| 密钥流复用 | 任意两文件公共前缀 0~1 字节 → **每文件独立随机 nonce/IV** |
| 备份关系 | 主文件与最新备份 MD5 完全一致 → 整文件字节拷贝 |
| 体积模型 | ≈612 B/主播（620→374,764 B 线性拟合） |
| 唯一结构特征 | 12/12 文件首字节恒 < 0x40（高 2 bit 恒 00），疑 2bit 版本号 + 6bit 随机 |
| 解密时机 | 日志 14 次均为「登录成功」**之后**才「JSON数据初始化——成功」 |
| 内存取证 | 6 个崩溃 dump + 187MB 内存 dump 中 `web_rid/sec_uid/nickname/room_id` 全部 **0 命中** |

→ 结论：**流式强加密**（AES-CTR/CFB/OFB 或 ChaCha20 类），密钥在 VMProtect 壳内，且只在卡密登录成功后解密。卡密已过期 → 任何静态/动态手段都拿不到明文，除非恢复授权（本报告不做）。

### 6.2 已抢救出的资产：394 个主播昵称

改从 **Windows 使用痕迹**（`%APPDATA%\Microsoft\Windows\Recent\*.lnk`、跳转列表 `AutomaticDestinations`、`Explorer\TypedPaths`）解析出：

- **394 个主播昵称 + 5 位 hex 短 ID**：`analysis/dynamic/decrypted/anchors_recovered.txt`（一行一个）/ `.json`（含映射）；
- 25 个真实录制文件的路径与命名（`recording_files_observed.csv`）；
- 目录/文件名模板（见 §4.3）。

**限制**：这些昵称里没有 web_rid/sec_uid，无法直接复原成可监控的直播间链接。建议用户按昵称在抖音搜索，重新粘贴直播间链接添加；工具支持一次粘贴 4 种形式（见 §7.2）。

---

## 7. 抖音直播取流协议（自研版实测结论）

### 7.1 核心结论：**不需要 a_bogus / X-Bogus 签名**

只需匿名 `ttwid` cookie（`GET https://live.douyin.com/` 下发）。所有接口用 Python 标准库即可调用，无需浏览器/JS 引擎。

| 用途 | 接口 | 关键参数/返回 |
|---|---|---|
| 取 cookie | `GET https://live.douyin.com/` | Set-Cookie: `ttwid` |
| 房间状态+取流 | `GET https://live.douyin.com/webcast/room/web/enter/` | `web_rid`；`data.data[0].status==2` 在播；`stream_url.flv_pull_url.{FULL_HD1,HD1,SD1,SD2}` / `hls_pull_url_map` |
| 在播发现 | `GET https://live.douyin.com/webcast/feed/` | `data[].data`（status==2），自带 stream_url 与 owner.short_id |
| 分享短链 | `v.douyin.com/xxx` 302 → `webcast.amemv.com/.../reflow/<19位room_id>?sec_user_id=` → `GET webcast.amemv.com/webcast/room/reflow/info/?room_id=&sec_user_id=`（未签名可用）→ `data.room.owner.web_rid` |
| 主播主页链接 | `GET www.iesdouyin.com/web/api/v2/user/info/?unique_id=<抖音号>` → `enter?web_rid=<抖音号别名>` → reflow 拿数字 web_rid；离线主播也能拿到 web_rid |

**实测证据**（`analysis/probe/`、`analysis/lead/`）：

- 在播房间 `76274427953`：flv 原画 `cmdffmpeg.exe -i <url> -t 10 -f null -` 返回码 **0**（7.8s）；hls 同样 **0**（10.7s）；
- 短链端到端：`v.douyin.com/iQLgKSj/` → web_rid 466747346712 → 在播 → ffmpeg rc=0；
- 主页链接：4 个主播中 3 个解析出规范数字 web_rid（含离线主播王濛 369324308707）；
- 我（Lead）独立复验：feed 取 6 个在播房间，录 60 秒 → **58.15MB FLV**，remux 后 **58.13MB MP4，1920x1080 H.264 45fps + AAC 48kHz，时长 00:01:00.05**。

### 7.2 工具支持的输入形式（与原版一致）

1. 房间号（如 `54702621639`）
2. 手机分享短链（`https://v.douyin.com/xxxxxx`）
3. 电脑直播间链接（`https://live.douyin.com/xxxxxxxxx`）
4. 电脑主播主页链接（`https://www.douyin.com/user/xxxxxxxxxx`）
5. 以及带文案的整段分享文本（自动提取链接）

### 7.3 失败/风控表现

| 现象 | 含义 | 处理 |
|---|---|---|
| HTTP 200 + `status_code=4001038`，data 空 | web_rid 不存在/非法 | 返回 `LiveStatus(error=...)`，不中断监控 |
| `data.data[0].status == 4` | 未开播（title/cover 仍返回） | 正常"未开播"状态 |
| `status_code=10011` | reflow 缺 room_id 参数 | 重试/换入口 |
| reflow 偶发 10033 | 服务端升级 | 主页链接退回抖音号别名，缓存 1h 后自动更新 |
| 空响应/连接重置 | 风控或网络 | 刷新 ttwid 重试，失败计数退避 |

> 拉流地址带 `expire/sign` 时效签名，**每次开播/断流重连都必须重新取流**，不能缓存复用。

---

## 8. 原版录制链路还原（内存 dump 实证）

### 8.1 ffmpeg 命令模板（可直接复用，自研版已内置）

专业版（带自动重连）：

```
-y -reconnect 1 -reconnect_on_network_error 1 -reconnect_on_http_error 403,404,500,502,503,504
   -reconnect_streamed 1 -reconnect_delay_max 10
   -user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
   -referer https://live.douyin.com/ -timeout 15000000 -rw_timeout 15000000
   -fflags +genpts -analyzeduration 5000000 -probesize 5000000 -i "<url>"
```

通用版（简版）：`-y -user_agent "..." -referer https://live.douyin.com/ -timeout 15000000 -rw_timeout 10000000 -i "<url>"`

转码/后处理片段（ts→mp4，无重编码）：`-c:v copy -c:a aac -bsf:a aac_adtstoasc -movflags +faststart`；另有 NVENC/x264 重编码与转 MP3 的片段。

### 8.2 工程细节（值得借鉴，已吸收进自研版）

| 原版做法 | 自研版实现 |
|---|---|
| 按"两次监测间隔文件大小是否增长"判断 cmdffmpeg 卡死 → 结束进程并重启录制 | `stall_check_seconds=30` / `stall_kill_after=2`：连续 2 次不增长 → kill 进程树 → 重新取流重启 |
| ffmpeg 自带 `-reconnect*` 参数 | 默认注入，并加 `-reconnect_max_retries 3` 上限，超限退出交给上层重取流（双保险，避免签名失效时无限重连） |
| 分段：不分段 / 按时长 / 按大小；0 不限 | `segment_duration` / `segment_size_mb`，0 表示不分段 |
| ts→mp4 三种策略 | `container` + `remux_mp4` + `keep_source` 三开关覆盖 |
| 文件名 `[主播名称]_[时间1]_[画质]` | `output_template` / `filename_template` 可配，支持 `{anchor}/{date}/{time}/{quality}` |
| 企业微信/钉钉/飞书 WebHook + 开播提示音 | `notify.webhooks`（按域名自动选 payload）+ `notify.sound`（winsound） |
| 清晰度 6+1 档（原画/超清/高清/标清…，注意 SD1→_ld、SD2→_sd 键名交叉） | `quality` 配置 + `raw["qualities"]` 精确档位映射 |
| 仅 Windows、需管理员 | 纯 Python，无需管理员 |

---

## 9. 自研工具设计（交付物）

### 9.1 架构

```
run.py                 入口：GUI / --cli / --once / --check / --add / --list / --remove
src/
  dy_api.py            协议层（唯一抖音接口实现，纯标准库）：
                       resolve_target / get_live_status / choose_stream / fetch_recommend / get_cookie
  config.py            配置默认值/校验/损坏自动备份重建
  model.py             数据模型（主播任务、录制状态、事件）
  storage.py           watchlist.json 原子读写
  checker.py           协议层加载与状态查询（失败转 error，不抛异常）
  manager.py           监控线程 + 线程池并发 + 退避 + 自动开录/停录 + 保留清理
  recorder.py          录制引擎：ffmpeg 子进程、断流重连、分段、卡死看门狗、remux、进程树清理
  ffmpeg_util.py       ffmpeg 查找/自检/命令构建/remux/taskkill
  disk.py              磁盘空间检查与保留策略
  notify.py            企业微信/钉钉/飞书 WebHook + 提示音
  logger.py            日志（文件 + 控制台）
  gui.py               tkinter 中文界面（表格/工具栏/日志面板/设置对话框，后台线程）
  cli.py               无头模式
  mock_api.py          本机 HTTP-FLV 假直播源（离线测试/联调用）
bin/ffmpeg.exe         项目自带 ffmpeg（gyan build，87MB，自包含）
config.json            配置文件（含完整默认值）
watchlist.json         主播列表（UTF-8 明文 JSON）
```

### 9.2 关键能力

- **监控**：可配轮询间隔（默认 60s；原版 600s）、并发查询、失败指数退避（5s→120s）、下播自动停止；
- **录制**：`-c copy` 直录 FLV（或 ts），CPU 占用极低；断流自动重连（重新取带签名的流地址）；分段落盘；单文件大小滚动；结束自动 remux MP4（多段自动 concat）；
- **健壮性**：卡死看门狗、ffmpeg 超限退出接管、退出时 `taskkill /F /T` 清理子进程、磁盘空间守护、配置损坏自动备份重建；
- **通知**：开播/开始录/结束/错误 → 企业微信/钉钉/飞书机器人 + 提示音；
- **GUI**：中文，表格实时显示 直播中/录制中/状态/时长/文件大小/路径，支持增删改、开始/停止监控、立即检查、打开输出目录、设置；关闭时优雅停录；
- **无第三方依赖**：只需 Python 3.10+（tkinter 为可选，仅 GUI 需要）。

---

## 10. 与原版功能对照

| 原版功能 | 自研版 | 说明 |
|---|---|---|
| 卡密登录/机器码绑卡/到期提醒 | ❌ 不实现 | 本地自用无需授权体系 |
| 600 秒轮询开播 | ✅ 可配（默认 60s） | 更及时，风控自己把握 |
| 四种链接/房间号添加 | ✅ 房间号 + 3 种链接 + 分享文案 | 见 §7.2；抖音号(unique_id)支持有限 |
| 自动开录/下播停录 | ✅ | |
| FFmpeg 直录、断流重连、卡死重启 | ✅ | 双保险重连 |
| ts→mp4 转码三策略 | ✅ flv/ts + remux + 保留/删除源 | 默认 flv 直录 + remux mp4 |
| 分段（时长/大小） | ✅ | |
| 磁盘保留/清理 | ✅ | 按剩余空间 + 保留天数/数量 |
| 企业微信/钉钉/飞书推送 | ✅ | 按域名自动选格式 |
| 开播提示音 | ✅ | winsound |
| 主播昵称自动更新/文件夹改名 | ❌ roadmap | 原版会同步改目录名，风险较高 |
| 时间字幕 srt | ❌ roadmap | 原版会生成录制时间 srt |
| 双击预览直播 / 打开目录 | ✅ 打开目录；预览用系统默认播放器 | |
| 快手/其它平台录制 | ❌ | 本项目只做抖音 |
| 本地 HTTP 服务/手机推送 | ❌ | 原版有内嵌服务，用于状态推送 |

---

## 11. 验收记录（实测）

| 项 | 命令/方式 | 结果 |
|---|---|---|
| 协议自测 | `python src/dy_api.py --selftest`（房间号/链接/短链/主页链接） | 5 种入参全部通过，在播取流 flv/hls 均 ffmpeg rc=0 |
| 60 秒实盘录制 | Lead 独立脚本（feed 选在播房间→ffmpeg 60s→remux） | FLV 58.15MB → MP4 58.13MB，1920x1080 45fps + AAC 48kHz，时长 00:01:00.05 |
| 真实流引擎联调 | `tests/test_real.py`（真实抖音直播间） | 最近一次：房间 52029829670「小小兔」录 30s → 11.73MB，ffmpeg 探测 Duration 00:00:31.35，1440x1080 h264+aac，可播放（更早一次 55781016966：36.45MB/33.13s，1080p60） |
| 监控/自动开录/下播停录/并发上限 | `python tests/test_manager.py` | 全部通过 |
| 录制引擎（基础/断流重连/分段/下播/remux/失败收尾） | `python tests/test_recorder.py` | **ALL RECORDER TESTS PASSED**（Lead 独立复跑 rc=0；含 13.1s 失败收尾用例） |
| GUI 冒烟 | `python tests/test_gui.py` | 通过（窗口/表格/监控/录制/停止） |
| 环境自检 | `python run.py --check` | Python/ffmpeg/tkinter/协议层 全部正常 |
| 命令行增删查 | `python run.py --add/--list/--once` | 房间号与主页链接均能解析并查到直播状态 |

---

## 12. 使用入口

详见项目根 `README.md`。最快三步：

```bat
cd /d D:\Code\DS-Workspace\douyin-recorder
python run.py --check                       :: 环境自检
python run.py                               :: 打开 GUI；或 python run.py --cli 无头常驻
```

添加主播：GUI 里「添加」→ 粘贴直播间链接/分享短链/房间号；或命令行 `python run.py --add <链接或房间号> --name 主播名`。

---

## 13. 已知限制与后续建议

1. **620 主播名单无法解出**（见 §6）。可选方案：
   - 按 `analysis/dynamic/decrypted/anchors_recovered.txt` 的 394 个昵称逐个搜索补链接（工具支持批量粘贴）；
   - 若用户恢复/续费卡密，可运行原版程序并在「登录成功后」用 `analysis/dynamic/decrypted/FRIDA_PLAN.md` 的方案 hook `e2ee_cryptoDecrypt` 取明文（5 分钟）；
   - 本任务未做、也不会做破解。
2. **主页链接解析偶发降级**：reflow 接口 10033 时只能拿到抖音号别名（仍可轮询，但非规范数字 web_rid），恢复后缓存自动更新。
3. **「失败收尾」问题已修复**：根因是 ffmpeg 内部 `-reconnect` 参数让死地址单次重试约 30s，叠加卡死看门狗检查导致停止等待超时；修复方案为「零数据快速判死 + 所有等待可被 stop 事件唤醒 + 有界收尾（terminate → 5s → taskkill 兜底）」，`tests/test_recorder.py` 已 ALL PASSED。
4. 原版有而本工具未实现：时间字幕 srt、昵称自动同步/改目录名、快手等其它平台、手机端推送/本地 HTTP 服务。
5. 拉流的长期可用性依赖抖音接口变更；接口一旦调整，只需修改 `src/dy_api.py`（`docs/douyin_api.md` 有更新手册与错误码表）。

---

## 14. 产物索引

| 目录/文件 | 内容 |
|---|---|
| `src/`、`run.py`、`config.json`、`watchlist.json`、`bin/ffmpeg.exe` | 自研工具（交付主体） |
| `README.md`、`启动.bat` | 使用说明与一键启动 |
| `docs/report.md`（本文）、`docs/douyin_api.md` | 逆向报告与协议手册 |
| `analysis/static/REPORT.md`、`extracted/`、`dumps/`、`scripts/` | 静态逆向全套（22 个可复现脚本） |
| `analysis/dynamic/REPORT.md`、`raw/`、`decrypted/` | 动态行为、set.dat、394 主播名、dyuser 分析、FRIDA 方案 |
| `analysis/probe/`、`analysis/lead/` | 协议探测脚本与实测证据（ffmpeg 验证、60s 实盘录制） |
| `tests/` | 引擎/监控/GUI/真实流测试 |
| 体积说明 | 交付版已剔除可再生的 187MB 原始内存镜像与临时录制文件（详见 `analysis/static/dumps/README.md`）；项目总计约 151MB，其中 `bin/ffmpeg.exe` 83.6MB |

---

## 15. 合规声明

1. 本项目只对目标程序做**只读**分析与**互操作性**研究，未修改目标程序文件、未破解授权/卡密、未绕过任何付费校验；
2. 数据恢复部分使用的是用户本机 Windows 自身的文件使用痕迹（快捷方式/跳转列表），非破解所得；
3. 自研工具从零实现，仅复用公开可查的抖音 Web 接口行为与目标程序中可见的通用 ffmpeg 参数；
4. 录制内容版权归主播/平台所有，工具仅供个人学习备份使用，请勿传播或商用。

---

## 16. v2 更新（2026-11-13 晚）

在 v1 交付基础上按用户反馈完成第二轮迭代：

1. UI 重写：风格 A「青屿」（清新简洁 / 青绿主色 / 左侧导航 / 右侧详情抽屉），src/gui.py 重写 +
   新增 src/ui_theme.py（圆角按钮/状态胶囊/开关/导航/高 DPI）；效果图见 docs/mockups/。
2. 批量能力：批量导入（1000+ 行）、多选批量开始/停止录制、批量设置（自动录制/画质/分组/启用）、全局开始/停止监控。
3. 新功能：过短录制自动删除（min_keep_seconds）、ffmpeg 自动检测+缺失提示下载、运行记录（按天日志 + SQLite）
   与脱敏诊断包导出（--history / --export-diagnostics）。
4. 免 Python 打包：PyInstaller onedir，GUI + CLI 双入口，自带 ffmpeg，dist/DouyinRecorder_win64.zip（45.6MB），
   已实测 --check / --once（真实接口）/GUI 启动；说明见 docs/packaging.md。
5. 测试：6 套测试全部通过（test_recorder / test_manager / test_gui / test_min_keep / test_ffmpeg_detect / test_history）。
6. 待办：1000+ 长稳性能优化（自适应轮询/gzip/带宽闸门/24h 浸泡测试）与「默认最高清晰度」策略，见 docs/design_v2.md 第 8 节。

---

## 17. v2.1 性能改造（2026-11-13 晚）

针对「1000+ 直播间长期挂机、开播都要录」的需求完成性能改造：

1. **网络层（dy_api v1.1.0）**：gzip（在播房间 617KB→108KB，离线 424KB→62KB）、keep-alive 连接复用
   （1000 次请求 1035 条连接→12 条）、单响应 2MB 上限、解析后瘦身（LiveStatus 内存 -49%）；
   1000 次/12 并发：总耗时 -20%、吞吐 +25%、总流量 -82%。实测发现抖音边缘对「频繁新建 TLS」会返回 444，
   复用连接不受影响 —— 连接复用是长期挂机的关键。
2. **调度器与并发闸门（manager 重写）**：优先队列 + 固定工作池 + 令牌桶限速（默认 6 req/s）+ 自适应轮询
   （直播中 30s / 24h 内开播 90s / 长期离线 300s）+ 失败退避 + 全局熔断（错误率>30% 暂停并刷新 ttwid）；
   并发闸门：max_concurrent=0 不限、max_total_mbps=0 不限、max_ffmpeg_processes、磁盘闸门；
   超限状态=排队并显示原因，闸门释放 1s 内自动开录；1360 条批量导入：6ms 添加 + 19ms 落盘 + 10ms 快照。
3. **批量导入正式功能**：GUI 支持 Excel/CSV 导入（纯标准库解析 xlsx），三层去重（房间号/主页 sec_uid/规范化 URL），
   实测 1360 行全部识别、重复导入 0 新增。
4. **清晰度**：dy_api 新增 get_qualities + quality=best（按分辨率×码率），默认原画优先并向下回退。
5. **浸泡测试**：mock 1000 主播 + 10% 在线 + 10 路同时录制，1 小时基线（报告 docs/soak_report.md），
   真实 1360 主播抽样实测；产物按用户要求「remux mp4→校验可读→立即删除」。
6. **测试**：9 套测试全部通过（含新增 test_scheduler 11 用例、test_import、real 30s 真实录制）。

## 18. 浸泡测试发现并修复的关键缺陷（2026-11-13 22:30）

1 小时浸泡测试发现：**卡死看门狗误杀健康录制** —— 1245 场录制中有 1070 场在启动 30 秒后被判「未收到任何数据」重启，
但实际每场都已录到约 5MB/30s 数据。

- 根因：src/recorder.py 的 _total_size() 被写成 @staticmethod，且实现为遍历传入的 files 列表；
  所有无参调用（进度上报/卡死看门狗/单文件大小上限/断流 grew 判断）都返回 0，
  于是看门狗每 30 秒认为「零数据」，杀掉并重启正常录制；segment_size_mb 上限也一直未生效。
- 影响：任何超过 30 秒（stall_check_seconds）的录制都会被切成 ~30 秒片段并反复重连；此前单测
  录制时长 <30 秒，故未暴露。
- 修复：改为实例方法，files 为 None 时统计本场已收集文件；新增回归测试
  tests/test_recorder.py::test_stall_watchdog_healthy（健康录制 12 秒内不得被误杀）。
- 验证：3 分钟 300 主播/并发 10 重跑：stall=0、reconnect=0，32 场录制全部 remux+校验通过并删除；
  修复后的干净 1 小时基线重新执行。

## 19. 协议层小优化：无直播间账号负缓存（dy_api v1.1.1）

真实 1360 主播抽样显示查询失败中约 2/3 是「账号资料解析成功但当前没有可解析的直播间（从未开播/已注销/私密）」，
这类账号每轮重复发起 3–6 次解析请求。v1.1.1 增加负缓存：

- 仅对**明确**的「无直播间」结果缓存 10 分钟（NEGATIVE_CACHE_TTL=600s，可配）；10 分钟内重复查询 0 请求、返回相同 error；
- 解析到直播间立即失效；网络/风控等不确定失败**不缓存**（不会掩盖真实故障）；
- get_http_stats() 增加 negative_cache_hits；真实网络验证：失败链接 4 请求/1.77s → 0 请求/0.000s；
- 代价：账号从"无直播间"变为开播的发现延迟最多 10 分钟（可按需调小 TTL）。

## 20. v2.2 清晰度完善与断流降档（2026-11-14 00:10）

1. **默认最高档**：config.quality 默认 origin（原画优先，dy_api 已实现缺失回退）；新增 best（按 qualities 元数据选最高）；
   房间内可单独指定 hd/sd；GUI 设置页新增「清晰度」分组。
2. **断流自动降档**：quality_degrade_enabled=true、quality_degrade_after_reconnects=2、quality_ladder=origin,hd,sd；
   本场累计真实断流/卡死达阈值 → 降一档重开（effective_quality/degraded 写入 snapshot，GUI 显示「已降档」），
   写 INFO 事件与 history；下播/手动停止/下次开播恢复用户档位；手动录制同样适用。
3. **发现并修复 recorder.py 既有 bug**：is_live_fn 语义自相矛盾（监视循环 False=离线，ffmpeg 退出分支却把 True 当离线），
   导致断流被误判下播、RECONNECT 不产生。v2.2.1 已将退出分支改为判断 False 并加注释；manager 仍不传 is_live_fn
   （断流正常重连、下播由轮询+_enforce_stop 判定，最多多录一个轮询周期约 30s），避免改变已验证行为。
4. 测试：新增 tests/test_quality_degrade.py（5 用例）；全量 10 套测试全部通过；打包 v2.2.0。

## 21. 真实 1360 主播列表冷启动实测（v1.1.1，2026-11-14 00:21）

对用户提供的 1360 条主播主页链接抽样 300 条（严格 2 req/s 起，实际受网络延迟限制 0.66 req/s）：

| 指标 | 数值 |
|---|---|
| 300 条耗时 | 456s（1.52s/条；P50 1.79s，P95 2.21s） |
| 在播 / 未开播 / 失败 | 17（5.7%）/ 243 / 40（13.3%） |
| 失败构成 | 账号无直播间 19 + enter 无房间数据 20 + 解析失败 1 |
| HTTP 请求 / 连接 | 1468 次 / **4 条**（keep-alive 生效） |
| 线上 / 解压流量 | 37.1MB / 214.6MB（**gzip 5.8x**） |
| HTTP 层错误 | 0 |

结论：主页链接冷启动每个约 4.9 次 HTTP 请求（sec_uid→抖音号→reflow→enter），解析结果缓存 12h 后每轮每房间仅 1 次 enter；
1000+ 主页链接首次解析约 3–15 分钟（一次性成本），之后进入正常限速轮询；无直播间账号由负缓存 10 分钟内不再重复解析。

### GUI 规模验证（补充）

watchlist 1360 条时：表格分页 200/页（7 页），全量刷新 10–13ms（偶发 110ms GC 尖峰），搜索过滤 11ms，
状态筛选计数与详情抽屉正常 —— 1000+ 主播下 GUI 操作流畅。

### 打包版 Excel 导入验证（v2.3.0）

用打包后的 exe 实测：DouyinRecorder-CLI.exe --import-file 主播列表.xlsx → 1361 行解析出 1360 条有效、
新增 1360、二次导入重复 1360/新增 0，证明打包版内置纯标准库 xlsx 解析、无需 Python/openpyxl。

### 解析缓存效果与重启成本（补充）

同进程对 100 个主页链接连续两轮：第一轮 182.8s / 573 请求 → 第二轮 18.7s / 91 请求 / 0 新连接 / 负缓存命中 9 次（10 倍提速）。
当前缓存为进程内存，重启后需重新冷启动；P1-F 将落盘到 data/resolve_cache.json（POSITIVE_TTL 12h、负缓存 10min、去抖原子写）。

### 解析缓存落盘（v1.1.2）

解析缓存写入 data/resolve_cache.json（12h TTL、原子写、10s 去抖、损坏容错；负缓存不落盘）。
真实网络两进程实测：冷启动 23.07s/70 请求 → 重启后 1.85s/4 请求/disk_hits=11（**12.5 倍**），
1000+ 主页链接重启后不再需要重新冷启动 3–15 分钟。

### 24h 内存诊断（外部 GetProcessMemoryInfo）

连续 12 次采样（2 分钟）：Working Set 53.2–56.2MB、PrivateUsage 40.2–43.2MB、Peak WS 58.8MB、句柄 395–410；
末次 Private 比首次低 2.9MB，说明 Python 堆无持续增长，RSS 波动是 GC/分配器锯齿与工作集回收，非泄漏。

### 单实例锁（v2.3.2）

长驻模式（GUI/--cli）启动加 data/app.lock 独占文件锁，重复启动被拒绝（退出码 3，提示持有 PID），
防止同一主播被两个进程同时录制；进程退出/被杀自动释放，残留文件可接管。新增 tests/test_instance_guard.py 4 用例。

### 产品完整链路并发验证（补充）

mock 源 + 正式 manager/recorder 210s（20 主播、并发 10、remux mp4、删源、min_keep 5s）：
40 场录制并发稳定 10/10、40 个 MP4 无残留源文件、SQLite 历史 40 条、0 error/0 断流/0 卡死。
